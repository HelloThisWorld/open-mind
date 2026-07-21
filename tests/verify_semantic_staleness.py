"""Candidate staleness — code and document revision changes stale dependent
candidates transitively; history stays queryable; review status survives;
startup reconciliation repairs missed events.
"""
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    REQUIREMENTS_MD, check, finish, find_evidence, make_workspace,
    mock_profile, requirement_response)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind import db  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import store  # noqa: E402

runtime = get_runtime()
semantic = runtime.semantic
pid = make_workspace(runtime, "stale-ws")
evidence_id = find_evidence(pid, "shall respond to a status query")
mock_profile("mock-stale", responses={
    "requirement-extraction": requirement_response(evidence_id)})
semantic.set_policy(pid, provider_profile="mock-stale")

run = semantic.start_analysis(pid, task_types=["requirement-extraction"],
                              wait=True, timeout=180)["run"]
check("setup analysis done", run["status"] == "done")
candidates = semantic.list_candidates(pid)["candidates"]
req = [c for c in candidates if c["candidate_type"] == "requirement"][0]
semantic.review_candidate(pid, req["id"], decision="confirm",
                          note="looks right", reviewer="tester")

# dependent relation + conflict candidates referencing the requirement
rel_ids = store.insert_relations(pid, [{
    "relation_type": "possibly-related",
    "source_ref": {"kind": "candidate", "id": req["id"], "key": ""},
    "target_ref": {"kind": "symbol", "id": "s_x", "key": "QueryService"},
    "source_candidate_id": req["id"], "reason": "mentions",
    "confidence": "low", "evidence_status": "verified",
    "evidence": [{"evidence_id": evidence_id, "quote": "status query",
                  "quote_hash": "qh"}]}])
conf_ids = store.insert_conflicts(pid, [{
    "category": "document-document",
    "refs": [{"kind": "candidate", "id": req["id"], "key": ""},
             {"kind": "revision", "id": "r_other", "key": ""}],
    "left_candidate_id": req["id"], "explanation": "differs",
    "confidence": "low", "evidence_status": "verified",
    "evidence": []}])

# ---------------------------------------------------------------------------
# 1. A new DOCUMENT revision stales candidates + dependents transitively
# ---------------------------------------------------------------------------
src = pathlib.Path(tempfile.mkdtemp(prefix="om_stale_doc_"))
doc = src / "requirements.md"
doc.write_text(REQUIREMENTS_MD + "\nREQ-NC-004: New requirement line.\n",
               encoding="utf-8")
asset_id = runtime.documents.list_documents(pid)["documents"][0]["id"]
result = runtime.documents.add_document(pid, str(doc), asset_id=asset_id,
                                        wait=True, timeout=180)
check("a new document revision was committed",
      result.get("status") == "revision")

stale_req = semantic.get_candidate(pid, req["id"])
check("the document candidate became STALE",
      stale_req["lifecycle_status"] == "stale"
      and stale_req["stale_at"])
check("its CONFIRMED review status is preserved",
      stale_req["review_status"] == "confirmed")
rel = store.get_relation(pid, rel_ids[0])
check("the dependent relation candidate became STALE",
      rel["lifecycle_status"] == "stale")
conf = store.get_conflict(pid, conf_ids[0])
check("the dependent conflict candidate became STALE",
      conf["lifecycle_status"] == "stale")

# History stays queryable; active listing excludes it
listing = semantic.list_candidates(pid, lifecycle_status="stale")
check("stale candidates remain queryable",
      any(c["id"] == req["id"] for c in listing["candidates"]))
active = semantic.list_candidates(pid, lifecycle_status="active")
check("stale candidates leave the active listing",
      all(c["id"] != req["id"] for c in active["candidates"]))
check("nothing was deleted", store.get_candidate(pid, req["id"]) is not None)

# ---------------------------------------------------------------------------
# 2. A new CODE revision stales code-bound candidates
# ---------------------------------------------------------------------------
code_src = pathlib.Path(tempfile.mkdtemp(prefix="om_stale_code_"))
code_file = code_src / "service.py"
code_file.write_text("def status():\n    return 'ok'\n", encoding="utf-8")
runtime.workspaces.add_path(pid, str(code_src))
runtime.ingest.start(pid, wait=True, timeout=300)
code_asset = db.find_asset_by_logical_key(pid, "service.py")
check("code asset ingested", code_asset is not None)
code_cand_ids = store.insert_candidates(pid, [{
    "candidate_kind": "engineering-concept", "candidate_type": "interface",
    "revision_id": code_asset["current_revision_id"],
    "stable_key": "", "title": "status()",
    "statement": "The service exposes a status operation.",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": []}])
code_file.write_text("def status():\n    return 'ok'\n\ndef extra():\n"
                     "    return 1\n", encoding="utf-8")
runtime.ingest.start(pid, wait=True, timeout=300)
code_cand = store.get_candidate(pid, code_cand_ids[0])
check("a new code revision stales the code-bound candidate",
      code_cand["lifecycle_status"] == "stale")

# ---------------------------------------------------------------------------
# 3. Startup reconciliation repairs a missed staleness event
# ---------------------------------------------------------------------------
missed_ids = store.insert_candidates(pid, [{
    "candidate_kind": "engineering-concept", "candidate_type": "requirement",
    "revision_id": "r_gone_missing",       # a revision that is not current
    "stable_key": "REQ-MISSED-1", "title": "Missed",
    "statement": "A candidate whose staleness event was missed.",
    "confidence": "low", "evidence_status": "verified", "evidence": []}])
check("the seeded candidate starts active",
      store.get_candidate(pid, missed_ids[0])["lifecycle_status"]
      == "active")
from openmind import jobs as jobs_engine  # noqa: E402
jobs_engine.reconcile_semantic_staleness(pid)
check("the startup/backstop reconciliation repairs it",
      store.get_candidate(pid, missed_ids[0])["lifecycle_status"]
      == "stale")

result = semantic.reconcile_staleness(pid)
check("reconciliation is idempotent (second run changes nothing)",
      result["stale_candidates"] == 0 and result["stale_relations"] == 0
      and result["stale_conflicts"] == 0)

# candidates for the NEW current revisions stay active
fresh = semantic.list_candidates(pid, lifecycle_status="active")
check("candidates bound to current revisions stay active",
      all(c["lifecycle_status"] == "active" for c in fresh["candidates"]))

finish()
