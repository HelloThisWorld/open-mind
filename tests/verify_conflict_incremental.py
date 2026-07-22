"""Conflict incrementality: a duplicate scan neither duplicates conflicts
nor mints revisions; changed values supersede the prior conflict and
preserve history; a stale detection basis stales the conflict; detector
failure produces an honest partial result."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "cinc-fix")
pid = fx.pid
trace = fx.trace
objects = fx.lifecycle()
req = objects["requirement"]
claim_2s = fx.claim(req, "constraint", "The check timeout is 2 seconds.")
claim_5s = fx.claim(req, "constraint", "The check timeout is 5 seconds.")

scan1 = trace.scan_conflicts(pid, actor="scanner")
check("first scan creates the conflict", len(scan1["created"]) == 1)
conflict_id = scan1["created"][0]

# -- duplicate scan -----------------------------------------------------------
revision_before = runtime.knowledge.get_current_revision(
    pid)["knowledge_revision"]
observed_before = trace.get_conflict(pid, conflict_id)["metadata"].get(
    "last_observed_at")
scan2 = trace.scan_conflicts(pid, actor="scanner")
check("duplicate scan creates nothing and supersedes nothing",
      not scan2["created"] and not scan2["superseded"])
check("duplicate scan observes the identical conflict",
      conflict_id in scan2["observed_unchanged"])
check("duplicate scan mints NO Knowledge Revision",
      runtime.knowledge.get_current_revision(pid)["knowledge_revision"]
      == revision_before)
check("last_observed metadata updated without a revision",
      trace.get_conflict(pid, conflict_id)["metadata"]
      .get("last_observed_revision") == revision_before)
check("still exactly one open conflict",
      trace.list_conflicts(pid, status="open")["count"] == 1)

# -- changed value supersedes -------------------------------------------------
runtime.knowledge.supersede_object(
    pid, kind="claim", object_id=claim_5s["id"],
    replacement_id=fx.claim(req, "constraint",
                            "The check timeout is 9 seconds.")["id"],
    actor="fx", note="value corrected to another wrong value")
scan3 = trace.scan_conflicts(pid, actor="scanner")
check("changed compared values supersede the old conflict",
      conflict_id in scan3["superseded"]
      and len(scan3["created"]) == 1)
old = trace.get_conflict(pid, conflict_id)
new_id = scan3["created"][0]
check("history preserved: old conflict superseded and linked",
      old["status"] == "superseded"
      and old["superseded_by_conflict_id"] == new_id)
check("the supersession is in the old conflict's decision ledger",
      any(d["decision"] == "supersede" for d in old["decisions"]))
new = trace.get_conflict(pid, new_id)
check("the new conflict carries the new values",
      "9000" in str(new["metadata"].get("left_value"))
      or "9000" in str(new["metadata"].get("right_value")))

# -- detection basis gone -> conflict goes stale ------------------------------
# Withdraw the 2s claim: only one active value remains, nothing to compare.
runtime.knowledge.withdraw_object(pid, kind="claim",
                                  object_id=claim_2s["id"], actor="fx",
                                  note="withdrawing the 2s statement")
scan4 = trace.scan_conflicts(pid, actor="scanner")
check("a conflict whose basis is gone goes stale (never auto-resolved)",
      new_id in scan4["staled"]
      and trace.get_conflict(pid, new_id)["status"] == "stale")

# re-detected identical facts reopen the stale conflict
runtime.knowledge.create_claim(
    pid, entity_id=req["id"], claim_type="constraint",
    statement="The check timeout is 2 seconds.",
    evidence=[{"evidence_id": fx.evidence_id}], actor="fx", note="back")
scan5 = trace.scan_conflicts(pid, actor="scanner")
check("re-detection after staleness reopens or recreates",
      scan5["reopened"] or scan5["created"])

# -- detector failure -> honest partial --------------------------------------
from openmind.traceability import conflicts as conflicts_module  # noqa: E402
from openmind.traceability import detectors as detectors_module  # noqa: E402


class _Boom(detectors_module._BaseDetector):
    name = "boom"
    version = "0.0.1"
    categories = {"document-document"}

    def detect(self, context):
        raise RuntimeError("simulated detector failure")


original = list(detectors_module.ALL_DETECTORS)
conflicts_before = trace.list_conflicts(pid, limit=200)["conflicts"]
detectors_module.ALL_DETECTORS.append(_Boom())
try:
    partial = trace.scan_conflicts(pid, actor="scanner")
finally:
    detectors_module.ALL_DETECTORS[:] = original
check("a detector failure yields an honest partial scan",
      partial["status"] == "partial"
      and any(e["detector"] == "boom"
              for e in partial["detector_errors"]))
conflicts_after = trace.list_conflicts(pid, limit=200)["conflicts"]
check("a failing detector corrupts no existing conflict",
      {c["id"]: c["status"] for c in conflicts_before}
      == {c["id"]: c["status"] for c in conflicts_after
          if c["id"] in {x["id"] for x in conflicts_before}})

finish()
