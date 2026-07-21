"""Evidence verifier — ownership, request inclusion, quote verification,
whitespace normalization, fabrication rejection, locally derived confidence.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    REQUIREMENTS_MD, check, finish, find_evidence, make_workspace)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import verifier  # noqa: E402
from openmind.semantic.context import resolve_evidence_text  # noqa: E402

runtime = get_runtime()
pid = make_workspace(runtime, "verifier-ws")
foreign_pid = make_workspace(runtime, "verifier-foreign",
                             documents={"other.md": "# Other\n\nAlien text "
                                                    "paragraph here.\n"})

evidence_id = find_evidence(pid, "shall respond to a status query")
foreign_evidence = find_evidence(foreign_pid, "Alien text")
allowed = frozenset({evidence_id})


def candidate(**overrides):
    base = {
        "candidateType": "requirement", "stableKey": "REQ-NC-001",
        "title": "Status query", "statement":
            "The system shall respond to a status query within 2 seconds.",
        "attributes": {},
        "evidence": [{"evidenceId": evidence_id,
                      "quote": "shall respond to a status query within 2 "
                               "seconds"}],
        "confidenceHint": "low",       # the model lowballs; verification decides
        "reason": "explicit requirement",
    }
    base.update(overrides)
    return base


ALLOWED_TYPES = frozenset({"requirement"})

# ---------------------------------------------------------------------------
# 1. The happy path: verified + locally derived HIGH
# ---------------------------------------------------------------------------
verdict = verifier.verify_candidate(pid, candidate(),
                                    allowed_types=ALLOWED_TYPES,
                                    allowed_evidence_ids=allowed,
                                    resolver=resolve_evidence_text)
check("valid evidence accepted", verdict.accepted)
check("evidence status is 'verified'", verdict.evidence_status == "verified")
check("identifier + exact quote derives HIGH locally",
      verdict.confidence == "high")
check("the model's low hint did NOT become the final confidence",
      verdict.confidence != "low")
check("verified quotes carry their hash",
      verdict.verified_evidence[0]["quote_hash"])

# ---------------------------------------------------------------------------
# 2. Rejections: foreign, un-sent, fabricated, empty, unknown type, bad key
# ---------------------------------------------------------------------------
verdict = verifier.verify_candidate(
    pid, candidate(evidence=[{"evidenceId": foreign_evidence,
                              "quote": "Alien text"}]),
    allowed_types=ALLOWED_TYPES,
    allowed_evidence_ids=frozenset({foreign_evidence}),
    resolver=resolve_evidence_text)
check("evidence from a FOREIGN workspace is rejected (scoped resolver)",
      not verdict.accepted and verdict.evidence_status == "rejected")

verdict = verifier.verify_candidate(
    pid, candidate(), allowed_types=ALLOWED_TYPES,
    allowed_evidence_ids=frozenset({"e_something_else"}),
    resolver=resolve_evidence_text)
check("evidence not included in the request is rejected",
      not verdict.accepted
      and any("not part of the request" in d for d in verdict.diagnostics))

verdict = verifier.verify_candidate(
    pid, candidate(evidence=[{"evidenceId": evidence_id,
                              "quote": "the system shall levitate"}]),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("a fabricated quote is rejected",
      not verdict.accepted
      and any("fabricated" in d for d in verdict.diagnostics))

verdict = verifier.verify_candidate(
    pid, candidate(evidence=[{"evidenceId": evidence_id, "quote": "   "}]),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("an empty quote is rejected", not verdict.accepted)

verdict = verifier.verify_candidate(
    pid, candidate(evidence=[]), allowed_types=ALLOWED_TYPES,
    allowed_evidence_ids=allowed, resolver=resolve_evidence_text)
check("a candidate with no evidence at all is rejected",
      not verdict.accepted and verdict.evidence_status == "rejected")

verdict = verifier.verify_candidate(
    pid, candidate(candidateType="interface"),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("a candidate type outside the task's allowance is rejected",
      not verdict.accepted and "not allowed" in verdict.rejection_reason)

verdict = verifier.verify_candidate(
    pid, candidate(stableKey="REQ\nNC 001\x00"),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("a malformed identifier is rejected",
      not verdict.accepted and "identifier" in verdict.rejection_reason)

# ---------------------------------------------------------------------------
# 3. Whitespace normalization: reflowed quotes still verify
# ---------------------------------------------------------------------------
verdict = verifier.verify_candidate(
    pid, candidate(evidence=[{"evidenceId": evidence_id,
                              "quote": "shall   respond\n to a status\t"
                                       "query"}]),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("a whitespace-reflowed quote still verifies", verdict.accepted
      and verdict.evidence_status == "verified")
check("normalize_ws collapses all whitespace runs",
      verifier.normalize_ws("a\n\t b   c") == "a b c")

# ---------------------------------------------------------------------------
# 4. Confidence ladder: medium / low / partially-verified
# ---------------------------------------------------------------------------
retry_evidence = find_evidence(pid, "attempt failed transfers")
verdict = verifier.verify_candidate(
    pid, candidate(stableKey="",
                   statement="The retry handler attempts failed transfers "
                             "three times.",
                   evidence=[{"evidenceId": retry_evidence,
                              "quote": "must attempt failed transfers three "
                                       "times"}]),
    allowed_types=ALLOWED_TYPES,
    allowed_evidence_ids=frozenset({retry_evidence}),
    resolver=resolve_evidence_text)
check("normative language without an identifier still derives HIGH",
      verdict.confidence == "high")

archiver_evidence = find_evidence(pid, "compress completed batches")
verdict = verifier.verify_candidate(
    pid, candidate(stableKey="",
                   statement="The archiver compresses completed batches "
                             "nightly.",
                   evidence=[{"evidenceId": archiver_evidence,
                              "quote": "compress completed batches "
                                       "nightly"}]),
    allowed_types=ALLOWED_TYPES,
    allowed_evidence_ids=frozenset({archiver_evidence}),
    resolver=resolve_evidence_text)
check("verified quote without identifier or normative anchor derives "
      "MEDIUM", verdict.confidence == "medium")

verdict = verifier.verify_candidate(
    pid, candidate(evidence=[
        {"evidenceId": evidence_id,
         "quote": "shall respond to a status query"},
        {"evidenceId": evidence_id, "quote": "made up nonsense"}]),
    allowed_types=ALLOWED_TYPES, allowed_evidence_ids=allowed,
    resolver=resolve_evidence_text)
check("mixed valid+fabricated citations yield PARTIALLY-VERIFIED",
      verdict.accepted and verdict.evidence_status == "partially-verified")
check("partially-verified caps confidence at LOW",
      verdict.confidence == "low")

# ---------------------------------------------------------------------------
# 5. Relations and conflicts: capped confidence
# ---------------------------------------------------------------------------
relation = {"relationType": "refines",
            "sourceRef": {"kind": "candidate", "id": "sc_1", "key": ""},
            "targetRef": {"kind": "candidate", "id": "sc_2", "key": ""},
            "evidence": [{"evidenceId": evidence_id,
                          "quote": "shall respond to a status query"}],
            "confidenceHint": "high", "reason": "same identifier"}
verdict = verifier.verify_relation(pid, relation,
                                   allowed_evidence_ids=allowed,
                                   resolver=resolve_evidence_text,
                                   pair_signal="identifier")
check("an identifier-paired relation with verified quotes is MEDIUM at "
      "best (hint 'high' ignored)", verdict.accepted
      and verdict.confidence == "medium")
verdict = verifier.verify_relation(pid, relation,
                                   allowed_evidence_ids=allowed,
                                   resolver=resolve_evidence_text,
                                   pair_signal="retrieval")
check("a semantic-retrieval relation stays LOW",
      verdict.confidence == "low")
verdict = verifier.verify_relation(pid, dict(relation, evidence=[]),
                                   allowed_evidence_ids=allowed,
                                   resolver=resolve_evidence_text,
                                   pair_signal="identifier")
check("a relation without valid evidence is rejected", not verdict.accepted)

conflict = {"category": "document-document",
            "refs": [{"kind": "revision", "id": "r_1", "key": ""},
                     {"kind": "revision", "id": "r_2", "key": ""}],
            "explanation": "One says 2 seconds, the other three retries.",
            "evidence": [
                {"evidenceId": evidence_id,
                 "quote": "shall respond to a status query"},
                {"evidenceId": retry_evidence,
                 "quote": "must attempt failed transfers three times"}],
            "confidenceHint": "high", "reason": "same subject"}
verdict = verifier.verify_conflict(
    pid, conflict,
    allowed_evidence_ids=frozenset({evidence_id, retry_evidence}),
    resolver=resolve_evidence_text)
check("a conflict quoting BOTH sides is MEDIUM, never high",
      verdict.accepted and verdict.confidence == "medium")

finish()
