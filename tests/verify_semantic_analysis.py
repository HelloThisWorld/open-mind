"""Semantic analysis pipeline — zero-call planning, zero-call ingestion,
current-revisions-only scope, bounded context, transactional persistence,
target isolation, restart/resume, relation and conflict bounds.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    DESIGN_MD, REQUIREMENTS_MD, check, finish, find_evidence, make_workspace,
    mock_profile, requirement_response)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind import db  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import store  # noqa: E402
from openmind.semantic.pairs import (  # noqa: E402
    MAX_CONFLICT_PAIRS, MAX_RELATION_PAIRS, conflict_pairs, relation_pairs)
from openmind.semantic.providers.mock_provider import (  # noqa: E402
    RECORDED_REQUESTS, reset_recorder)

runtime = get_runtime()
semantic = runtime.semantic

# ---------------------------------------------------------------------------
# 1. Ordinary ingestion performs ZERO provider calls
# ---------------------------------------------------------------------------
reset_recorder()
pid = make_workspace(runtime, "analysis-ws",
                     documents={"requirements.md": REQUIREMENTS_MD,
                                "design.md": DESIGN_MD})
check("document ingestion made zero provider calls",
      len(RECORDED_REQUESTS) == 0)

req_evidence = find_evidence(pid, "shall respond to a status query")
profile = mock_profile("mock-analysis", responses={
    "requirement-extraction": requirement_response(req_evidence),
    "document-classification": {"candidates": [{
        "candidateType": "requirements", "stableKey": "",
        "title": "Neutral Component Requirements",
        "statement": "A requirements document.",
        "attributes": {},
        "evidence": [{"evidenceId": req_evidence, "quote": "REQ-NC-001"}],
        "confidenceHint": "high", "reason": "identifiers present"}]},
})
semantic.set_policy(pid, provider_profile="mock-analysis")

# ---------------------------------------------------------------------------
# 2. Planning: deterministic, bounded, zero calls
# ---------------------------------------------------------------------------
plan = semantic.plan_analysis(pid, task_types=["document-classification",
                                               "requirement-extraction"])
check("planning made zero provider calls",
      len(RECORDED_REQUESTS) == 0 and plan["provider_calls_made"] == 0)
check("plan reports targets/revisions/estimates",
      plan["target_count"] > 0 and plan["revision_count"] == 2
      and plan["estimated_input_tokens"] > 0)
check("token figures are labelled estimated",
      "estimated" in plan["token_estimate_basis"])
check("the external plan carries no raw target list",
      "targets" not in plan)

removed = runtime.documents.list_documents(pid)["documents"]
plan_docs = semantic.plan_analysis(pid, task_types=["requirement-extraction"],
                                   scope={"kind": "code"})
check("scope=code excludes document assets, with reasons",
      plan_docs["target_count"] == 0 and plan_docs["excluded_count"] > 0)

# ---------------------------------------------------------------------------
# 3. First analysis: candidates persisted, targets checkpointed
# ---------------------------------------------------------------------------
result = semantic.start_analysis(
    pid, task_types=["document-classification", "requirement-extraction"],
    wait=True, timeout=180)
run = result["run"]
check("first analysis reaches DONE", run["status"] == "done")
counts = run["targets"]
check("every target is checkpointed as done",
      set(counts) == {"done"} and counts["done"] == sum(counts.values()))
check("provider request count matches non-cached targets",
      run["summary"]["counters"]["provider_requests"] == counts["done"])

candidates = semantic.list_candidates(pid)
req_cands = [c for c in candidates["candidates"]
             if c["candidate_type"] == "requirement"]
check("verified requirement candidates persisted",
      len(req_cands) >= 1 and req_cands[0]["evidence_status"] == "verified")
check("every persisted candidate carries provenance",
      all(c["task_version"] and c["prompt_version"] and c["analyzer_version"]
          and c["model_name"] for c in candidates["candidates"]))
check("every persisted candidate is labelled status=candidate",
      all(c["status"] == "candidate" for c in candidates["candidates"]))
detail = semantic.get_candidate(pid, req_cands[0]["id"])
check("candidate detail joins its verified evidence quotes",
      detail["evidence"] and detail["evidence"][0]["quote_hash"])
check("dedup: identical proposals from sibling targets stored once",
      len(req_cands) == 1)

# analysis used only ACTIVE assets' CURRENT revisions
analyzed_revisions = {t["revision_id"]
                      for t in store.list_targets(run["id"])
                      if t["revision_id"]}
current = {a["current_revision_id"] for a in db.list_assets(pid, limit=100)}
check("analysis targets only current revisions",
      analyzed_revisions <= current)

# bounded neighbor context: request packets stayed bounded
biggest = max(len(str(r["input_packet"])) for r in RECORDED_REQUESTS)
check("no request packet approaches whole-repository size "
      f"(largest: {biggest} chars)", biggest < 60_000)
check("packets carry only untrusted entries + structural context",
      all(set(r["input_packet"]) == {"task", "allowedEvidenceIds",
                                     "context", "untrustedContent"}
          for r in RECORDED_REQUESTS))

# ---------------------------------------------------------------------------
# 4. Invalid provider output: recorded as a rejected diagnostic, isolated
# ---------------------------------------------------------------------------
bad_profile = mock_profile("mock-bad", responses={
    "requirement-extraction": {"candidates": [{
        "candidateType": "requirement", "stableKey": "REQ-NC-009",
        "title": "Fabricated", "statement": "The system shall teleport.",
        "attributes": {},
        "evidence": [{"evidenceId": req_evidence,
                      "quote": "the system shall teleport"}],
        "confidenceHint": "high", "reason": "invented"}]},
    "document-classification": {"candidates": []},
})
semantic.set_policy(pid, provider_profile="mock-bad")
before_candidates = semantic.list_candidates(pid)["total"]
result = semantic.start_analysis(pid,
                                 task_types=["requirement-extraction"],
                                 force=True, wait=True, timeout=180)
run2 = result["run"]
check("a run whose candidates all fail verification still completes",
      run2["status"] == "done")
check("fabricated-quote candidates were rejected, none persisted",
      semantic.list_candidates(pid)["total"] == before_candidates)
check("rejections are counted honestly",
      run2["summary"]["counters"]["candidates_rejected"] >= 1)
targets2 = store.list_targets(run2["id"])
check("rejection diagnostics are bounded on the target rows",
      any("fabricated" in (t["error"] or "") for t in targets2)
      and all(len(t["error"] or "") <= 500 for t in targets2))

# ---------------------------------------------------------------------------
# 5. Partial failure isolation + resume skips completed targets
# ---------------------------------------------------------------------------
flaky = mock_profile("mock-flaky", responses={
    "requirement-extraction": requirement_response(req_evidence),
    "document-classification": {"candidates": []},
}, fail={"kind": "timeout", "times": 2})
semantic.set_policy(pid, provider_profile="mock-flaky")
reset_recorder()
result = semantic.start_analysis(
    pid, task_types=["document-classification", "requirement-extraction"],
    force=True, wait=True, timeout=180)
run3 = result["run"]
counts3 = run3["targets"]
check("scripted timeouts fail exactly their targets",
      counts3.get("failed", 0) == 2 and counts3.get("done", 0) > 0)
check("a run with failed targets is PARTIAL, never silently done",
      run3["status"] == "partial")
done_before = counts3.get("done", 0)
calls_before_resume = len(RECORDED_REQUESTS)

resumed = semantic.resume_analysis(pid, run3["id"], wait=True, timeout=180)
run3b = resumed["run"]
check("resume completes the previously failed targets",
      run3b["status"] == "done"
      and run3b["targets"].get("failed", 0) == 0)
new_calls = len(RECORDED_REQUESTS) - calls_before_resume
check("resume re-ran ONLY the failed targets (completed ones skipped)",
      new_calls == 2)
check("resume created no duplicate candidates",
      len([c for c in semantic.list_candidates(pid)["candidates"]
           if c["candidate_type"] == "requirement"
           and c["lifecycle_status"] == "active"]) == 1)

# ---------------------------------------------------------------------------
# 6. Relation + conflict pair generation is bounded and signal-driven
# ---------------------------------------------------------------------------
pairs = relation_pairs(pid)
check("relation pairs come from deterministic signals only",
      all(p["signal"] in ("identifier", "identifier-mention", "code-symbol")
          for p in pairs["pairs"]))
check(f"relation pairs bounded at {MAX_RELATION_PAIRS}",
      len(pairs["pairs"]) <= MAX_RELATION_PAIRS)
conf_pairs = conflict_pairs(pid)
check(f"conflict pairs bounded at {MAX_CONFLICT_PAIRS} (no cross-product)",
      len(conf_pairs["pairs"]) <= MAX_CONFLICT_PAIRS)

# seed two same-key candidates with different statements -> conflict pair
store.insert_candidates(pid, [{
    "candidate_kind": "engineering-concept", "candidate_type": "requirement",
    "revision_id": sorted(current)[0], "stable_key": "REQ-NC-001",
    "title": "Status", "statement": "The system shall respond within 5 "
                                    "seconds.",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": [{"evidence_id": req_evidence, "quote": "status query",
                  "quote_hash": "qh"}]}])
conf_pairs2 = conflict_pairs(pid)
check("same identifier + different statements produces a conflict pair",
      any(p["signal"] == "identifier" and p["sharedKey"] == "REQ-NC-001"
          for p in conf_pairs2["pairs"]))

finish()
