"""Conflict Candidate promotion: unreviewed / rejected / stale / partially
verified candidates blocked; unresolved canonical references block;
a confirmed active verified candidate promotes transactionally and
idempotently, minting one Knowledge Revision, a governance Decision and a
promotion record; the candidate row survives."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (find_evidence,  # noqa: E402
                                make_confirmed_candidate)
from _traceability_helpers import (check, finish,  # noqa: E402
                                   insert_conflict_candidate, make_fixture)

from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import store as semantic_store  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "promo-fix")
pid = fx.pid
trace = fx.trace
evidence_id = fx.evidence_id
QUOTE = "shall answer within 2 seconds"


def candidate(**kw):
    return insert_conflict_candidate(pid, evidence_id=evidence_id,
                                     quote=QUOTE, **kw)


# -- blocked: unreviewed ------------------------------------------------------
unreviewed = candidate()
plan = trace.plan_conflict_promotion(pid, unreviewed)
check("unreviewed candidate blocked",
      plan["expected_action"] == "blocked"
      and any("review_status" in r for r in plan["blocking_reasons"]))
try:
    trace.promote_conflict_candidate(pid, unreviewed, actor="r", note="n")
    check("unreviewed promotion raises typed", False)
except Exception:
    check("unreviewed promotion raises typed", True)

# -- blocked: rejected --------------------------------------------------------
rejected = candidate()
runtime.semantic.review_conflict_candidate(pid, rejected,
                                           decision="reject",
                                           reviewer="r")
plan = trace.plan_conflict_promotion(pid, rejected)
check("rejected candidate blocked", plan["expected_action"] == "blocked")

# -- blocked: stale -----------------------------------------------------------
stale = candidate()
runtime.semantic.review_conflict_candidate(pid, stale, decision="confirm",
                                           reviewer="r")
conn, lock = __import__("openmind.db", fromlist=["shared_connection"]) \
    .shared_connection()
with lock:
    conn.execute("UPDATE semantic_conflict_candidates SET "
                 "lifecycle_status='stale' WHERE id=?", (stale,))
    conn.commit()
plan = trace.plan_conflict_promotion(pid, stale)
check("stale candidate blocked",
      plan["expected_action"] == "blocked"
      and any("lifecycle_status" in r for r in plan["blocking_reasons"]))

# -- blocked: partially verified ---------------------------------------------
partial = candidate(evidence_status="partially-verified")
runtime.semantic.review_conflict_candidate(pid, partial,
                                           decision="confirm",
                                           reviewer="r")
plan = trace.plan_conflict_promotion(pid, partial)
check("partially verified candidate blocked",
      plan["expected_action"] == "blocked"
      and any("evidence_status" in r for r in plan["blocking_reasons"]))

# -- blocked: unsupported category -------------------------------------------
possibly = candidate(category="possibly-conflicting")
runtime.semantic.review_conflict_candidate(pid, possibly,
                                           decision="confirm",
                                           reviewer="r")
plan = trace.plan_conflict_promotion(pid, possibly)
check("possibly-conflicting category blocked",
      plan["expected_action"] == "blocked"
      and any("category" in r for r in plan["blocking_reasons"]))

# -- blocked: unresolved canonical references --------------------------------
sc_unpromoted = make_confirmed_candidate(runtime, pid, confirm=True)
referencing = candidate(left_candidate_id=sc_unpromoted)
runtime.semantic.review_conflict_candidate(pid, referencing,
                                           decision="confirm",
                                           reviewer="r")
plan = trace.plan_conflict_promotion(pid, referencing)
check("an unpromoted referenced candidate blocks promotion",
      plan["expected_action"] == "blocked"
      and any("not promoted" in r for r in plan["blocking_reasons"]))

# promote the referenced candidate -> now eligible with resolved objects
runtime.knowledge.promote_candidate(pid, sc_unpromoted, actor="r",
                                    note="promote the left side")
plan = trace.plan_conflict_promotion(pid, referencing)
check("promotion becomes eligible once references resolve",
      plan["eligible"] and plan["expected_action"] == "create-conflict")
check("the plan names the resolved canonical objects",
      any(o["role"] == "left" for o in plan["proposed_objects"]))

# -- the promotion itself -----------------------------------------------------
revision_before = runtime.knowledge.get_current_revision(
    pid)["knowledge_revision"]
promoted = trace.promote_conflict_candidate(
    pid, referencing, actor="reviewer",
    note="evidence and canonical references verified")
check("promotion succeeded", promoted["status"] == "promoted")
conflict = promoted["conflict"]
check("promoted conflict is open with semantic-promotion origin",
      conflict["status"] == "open"
      and conflict["origin"] == "semantic-promotion"
      and conflict["promoted_from_conflict_candidate_id"] == referencing)
check("exactly one Knowledge Revision minted",
      promoted["knowledge_revision"] == revision_before + 1)
check("promoted conflict carries verified evidence joins",
      len(conflict["evidence"]) >= 1
      and conflict["evidence"][0]["quote"] == QUOTE)
check("promoted conflict references resolved canonical objects",
      any(o["role"] == "left" for o in conflict["objects"]))

# provenance: promotion record + governance decision
promotions = runtime.knowledge.list_promotions(pid)["promotions"]
check("promotion recorded in the knowledge_promotions ledger",
      any(p["candidate_kind"] == "conflict-candidate"
          and p["candidate_id"] == referencing
          and p["status"] == "promoted" for p in promotions))
decisions = runtime.knowledge.list_decisions(
    pid, decision_type="conflict-promote")["decisions"]
check("promotion recorded a governance Decision with the actor",
      any(d["target_id"] == referencing and d["actor"] == "reviewer"
          for d in decisions))

# candidate survives, separate from the conflict
survivor = semantic_store.get_conflict(pid, referencing)
check("the candidate row survives promotion, still a candidate",
      survivor is not None and survivor["status"] == "candidate")

# -- idempotency --------------------------------------------------------------
again = trace.promote_conflict_candidate(pid, referencing, actor="r",
                                         note="again")
check("promotion is idempotent",
      again["status"] == "already-promoted"
      and again["conflict"]["id"] == conflict["id"])
check("idempotent retry minted no new revision",
      runtime.knowledge.get_current_revision(pid)["knowledge_revision"]
      == revision_before + 1)
plan = trace.plan_conflict_promotion(pid, referencing)
check("the plan reports already-promoted",
      plan["expected_action"] == "already-promoted")

# -- transactionality ---------------------------------------------------------
# A candidate citing evidence with a fabricated quote fails verification
# INSIDE the transaction: nothing is written, no revision minted.
bad_quote = insert_conflict_candidate(
    pid, evidence_id=evidence_id, quote="this text is not in the evidence")
runtime.semantic.review_conflict_candidate(pid, bad_quote,
                                           decision="confirm",
                                           reviewer="r")
revision_now = runtime.knowledge.get_current_revision(
    pid)["knowledge_revision"]
try:
    trace.promote_conflict_candidate(pid, bad_quote, actor="r", note="n")
    check("fabricated quote blocks promotion", False)
except Exception:
    check("fabricated quote blocks promotion", True)
check("failed promotion wrote nothing (no conflict, no revision)",
      runtime.knowledge.get_current_revision(pid)["knowledge_revision"]
      == revision_now
      and __import__("openmind.traceability.store",
                     fromlist=["find_conflict_by_candidate"])
      .find_conflict_by_candidate(pid, bad_quote) is None)

finish()
