"""Conflict governance: the full lifecycle (open -> under-review ->
accepted-risk/resolved/dismissed -> reopen), notes and evidence
requirements, expiry, suppression fingerprints, no automatic claim
rewrites, complete double-ledger auditability, cross-workspace isolation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "gov-fix")
pid = fx.pid
trace = fx.trace
objects = fx.lifecycle()
req = objects["requirement"]
fx.claim(req, "constraint", "The check timeout is 2 seconds.")
claim_5s = fx.claim(req, "constraint", "The check timeout is 5 seconds.")
scan = trace.scan_conflicts(pid, actor="scanner")
conflict_id = scan["created"][0]

# -- transitions --------------------------------------------------------------
review = trace.start_conflict_review(pid, conflict_id, actor="reviewer",
                                     note="taking a look")
check("open -> under-review", review["conflict"]["status"] == "under-review")
try:
    trace.start_conflict_review(pid, conflict_id, actor="reviewer",
                                note="again")
    check("under-review -> under-review rejected", False)
except Exception:
    check("under-review -> under-review rejected", True)
try:
    trace.accept_conflict_risk(pid, conflict_id, actor="reviewer", note="")
    check("accept-risk requires a note", False)
except Exception:
    check("accept-risk requires a note", True)
try:
    trace.accept_conflict_risk(pid, conflict_id, actor="", note="x")
    check("accept-risk requires an actor", False)
except Exception:
    check("accept-risk requires an actor", True)

accepted = trace.accept_conflict_risk(
    pid, conflict_id, actor="reviewer",
    note="tolerable until the next release",
    expires_at="2020-01-01T00:00:00", follow_up="CHG-104")
check("accept-risk records expiry + follow-up",
      accepted["conflict"]["status"] == "accepted-risk"
      and accepted["conflict"]["metadata"]["accepted_risk_expires_at"]
      == "2020-01-01T00:00:00"
      and accepted["conflict"]["metadata"]["accepted_risk_follow_up"]
      == "CHG-104")

# expired accepted risk reopens on the next scan (never silently kept)
scan2 = trace.scan_conflicts(pid, actor="scanner")
check("expired accepted risk reopened by the scan",
      conflict_id in scan2["reopened"]
      and trace.get_conflict(pid, conflict_id)["status"] == "open")

# -- resolve ------------------------------------------------------------------
try:
    trace.resolve_conflict(pid, conflict_id, actor="reviewer",
                           note="fixed", resolution_type="left-correct",
                           evidence=[])
    check("resolve requires supporting evidence", False)
except Exception:
    check("resolve requires supporting evidence", True)
try:
    trace.resolve_conflict(pid, conflict_id, actor="reviewer",
                           note="fixed", resolution_type="not-a-type",
                           evidence=[{"evidence_id": fx.evidence_id}])
    check("resolve validates the resolution type", False)
except Exception:
    check("resolve validates the resolution type", True)

claims_before = {c["id"]: c["lifecycle_status"]
                 for c in runtime.knowledge.list_claims(
                     pid, entity_id=req["id"],
                     lifecycle_status=None)["claims"]}
resolved = trace.resolve_conflict(
    pid, conflict_id, actor="reviewer", note="the 2s value is correct",
    resolution_type="left-correct",
    evidence=[{"evidence_id": fx.evidence_id}])
check("resolve works with evidence + type",
      resolved["conflict"]["status"] == "resolved"
      and resolved["conflict"]["resolved_at"])
claims_after = {c["id"]: c["lifecycle_status"]
                for c in runtime.knowledge.list_claims(
                    pid, entity_id=req["id"],
                    lifecycle_status=None)["claims"]}
check("resolution does NOT rewrite canonical claims",
      claims_before == claims_after
      and claims_after[claim_5s["id"]] == "active")

# resolved-but-unchanged facts: the next scan reopens deterministically
scan3 = trace.scan_conflicts(pid, actor="scanner")
check("resolution without a graph change is reopened by the next scan",
      conflict_id in scan3["reopened"])

# -- dismiss + suppression ----------------------------------------------------
dismissed = trace.dismiss_conflict(pid, conflict_id, actor="reviewer",
                                   note="detector false positive here")
check("dismiss stores the suppression fingerprint",
      dismissed["conflict"]["status"] == "dismissed"
      and dismissed["conflict"]["metadata"]["suppression_fingerprint"])
scan4 = trace.scan_conflicts(pid, actor="scanner")
check("unchanged dismissed conflict is suppressed, not recreated",
      conflict_id in scan4["suppressed"] and not scan4["created"])
check("dismissed conflict stays dismissed",
      trace.get_conflict(pid, conflict_id)["status"] == "dismissed")

# changed underlying fact: suppression no longer applies -> NEW conflict
runtime.knowledge.supersede_object(
    pid, kind="claim", object_id=claim_5s["id"],
    replacement_id=fx.claim(req, "constraint",
                            "The check timeout is 7 seconds.")["id"],
    actor="reviewer", note="correcting the wrong value")
scan5 = trace.scan_conflicts(pid, actor="scanner")
check("changed underlying fact creates a NEW conflict "
      "(suppression no longer applies)",
      len(scan5["created"]) >= 1)
new_conflict = trace.get_conflict(pid, scan5["created"][0])
check("the old dismissed conflict is untouched",
      trace.get_conflict(pid, conflict_id)["status"] == "dismissed")
check("the new conflict shows the changed values",
      "7000" in str(new_conflict["metadata"].get("left_value"))
      or "7000" in str(new_conflict["metadata"].get("right_value")))

# -- explicit reopen ----------------------------------------------------------
reopened = trace.reopen_conflict(pid, conflict_id, actor="reviewer",
                                 note="was not a false positive")
check("explicit reopen works",
      reopened["conflict"]["status"] == "open")

# -- auditability -------------------------------------------------------------
history = trace.get_conflict(pid, conflict_id)["decisions"]
check("every action is in the conflict decision ledger",
      {d["decision"] for d in history}
      >= {"start-review", "accept-risk", "resolve", "dismiss", "reopen"})
check("every decision records actor, note and both statuses",
      all(d["actor"] and d["note"] and d["after_status"]
          for d in history))
check("every decision carries its Knowledge Revision",
      all(d["knowledge_revision"] >= 1 for d in history))
check("every conflict decision links the Phase 5 knowledge ledger",
      all(d["knowledge_decision_id"] for d in history))
kg_decisions = runtime.knowledge.list_decisions(
    pid, target_kind="conflict")["decisions"]
check("the knowledge ledger mirrors conflict governance",
      {d["decision_type"] for d in kg_decisions}
      >= {"conflict-detect", "conflict-review", "conflict-accept-risk",
          "conflict-resolve", "conflict-dismiss", "conflict-reopen"})

finish()
