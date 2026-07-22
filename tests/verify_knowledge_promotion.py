"""Candidate promotion: the full eligibility matrix, plan-writes-nothing,
transactional promotion with entity/claim/evidence/binding/decision/revision,
idempotency, classification and revision-status non-interference, relation
endpoint resolution, conflict candidates never promotable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (DESIGN_MD, check,  # noqa: E402
                                confirm_relation_candidate, finish,
                                find_evidence, insert_relation_candidate,
                                make_confirmed_candidate,
                                make_minimal_workspace, mock_profile,
                                requirement_response)

from openmind import db  # noqa: E402
from openmind.knowledge import store  # noqa: E402
from openmind.knowledge.errors import PromotionBlocked  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import store as semantic_store  # noqa: E402
from openmind.semantic.errors import CandidateNotFound  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)

# -- eligibility matrix ------------------------------------------------------
unreviewed = make_confirmed_candidate(runtime, pid, confirm=False)
plan = knowledge.plan_candidate_promotion(pid, unreviewed)
check("unreviewed candidate is blocked",
      not plan["eligible"] and plan["expected_action"] == "blocked"
      and any("unreviewed" in r for r in plan["blocking_reasons"]))
before_rev = store.current_revision_number(pid)
check("plan performed no graph write",
      store.current_revision_number(pid) == before_rev
      and store.count_entities(pid) == 0)
try:
    knowledge.promote_candidate(pid, unreviewed, actor="t", note="n")
    blocked_raises = False
except PromotionBlocked:
    blocked_raises = True
check("promoting a blocked candidate raises typed PromotionBlocked",
      blocked_raises)
check("blocked attempt persisted nothing",
      store.current_revision_number(pid) == before_rev
      and store.find_promotion(pid, "semantic-candidate", unreviewed)
      is None)

runtime.semantic.review_candidate(pid, unreviewed, decision="reject",
                                  reviewer="r")
plan = knowledge.plan_candidate_promotion(pid, unreviewed)
check("rejected candidate is blocked", not plan["eligible"])

# partially verified: flip evidence_status directly through the store layer
partial = make_confirmed_candidate(runtime, pid, confirm=True)
conn, lock = db.shared_connection()
with lock:
    conn.execute("UPDATE semantic_candidates SET "
                 "evidence_status='partially-verified' WHERE id=?",
                 (partial,))
    conn.commit()
plan = knowledge.plan_candidate_promotion(pid, partial)
check("partially verified candidate is blocked",
      not plan["eligible"]
      and any("partially-verified" in r for r in plan["blocking_reasons"]))
with lock:
    conn.execute("UPDATE semantic_candidates SET "
                 "evidence_status='verified' WHERE id=?", (partial,))
    conn.commit()

# stale: mark lifecycle stale (what revision movement does)
with lock:
    conn.execute("UPDATE semantic_candidates SET lifecycle_status='stale' "
                 "WHERE id=?", (partial,))
    conn.commit()
plan = knowledge.plan_candidate_promotion(pid, partial)
check("stale candidate is blocked",
      not plan["eligible"]
      and any("stale" in r for r in plan["blocking_reasons"]))
with lock:
    conn.execute("UPDATE semantic_candidates SET lifecycle_status='active' "
                 "WHERE id=?", (partial,))
    conn.commit()

# -- eligible promotion ------------------------------------------------------
plan = knowledge.plan_candidate_promotion(pid, partial)
check("verified confirmed candidate is eligible",
      plan["eligible"]
      and plan["expected_action"] == "create-entity-and-claim"
      and plan["proposed_entity"]["canonical_key"]
      == "requirement:REQ-NC-017")
check("plan proposes identifier alias and source bindings",
      plan["proposed_aliases"]
      and any(b["ref_kind"] == "revision"
              for b in plan["proposed_bindings"]))

result = knowledge.promote_candidate(pid, partial, actor="reviewer-1",
                                     note="approved for canonical",
                                     source_command="test")
check("promotion status is promoted", result["status"] == "promoted")
entity = result["entity"]
claim = result["claim"]
check("promotion created the entity with promotion provenance",
      entity["origin"] == "semantic-promotion"
      and entity["promoted_from_candidate_id"] == partial)
check("promotion created the claim with verified evidence joins",
      claim["origin"] == "semantic-promotion"
      and len(claim["evidence"]) >= 1)
check("promotion created source bindings",
      any(b["ref_kind"] == "revision"
          for b in store.list_bindings(pid, entity["id"])))
check("promotion created the identifier alias",
      any(a["alias"] == "REQ-NC-017"
          for a in store.list_aliases(pid, entity["id"])))
promotion_record = store.find_promotion(pid, "semantic-candidate", partial)
check("promotion record persisted with policy version and actor",
      promotion_record and promotion_record["status"] == "promoted"
      and promotion_record["policy_version"] == "1"
      and promotion_record["actor"] == "reviewer-1")
check("promotion recorded a human decision",
      any(d["decision_type"] == "promote-candidate"
          and d["target_id"] == partial
          for d in knowledge.list_decisions(pid)["decisions"]))
check("promotion minted exactly one knowledge revision",
      result["knowledge_revision"] == before_rev + 1)
check("source candidate remains queryable after promotion",
      runtime.semantic.get_candidate(pid, partial)["review_status"]
      == "confirmed")

# -- idempotency -------------------------------------------------------------
again = knowledge.promote_candidate(pid, partial, actor="reviewer-1",
                                    note="again")
check("re-promotion returns already-promoted with the same target",
      again["status"] == "already-promoted"
      and again["target"]["id"] == entity["id"])
check("re-promotion minted no new revision",
      again["knowledge_revision"] == result["knowledge_revision"])

# -- classification promotion does not overwrite asset type ------------------
doc_asset = db.list_assets(pid, state="active", limit=10)[0]
asset_type_before = doc_asset["asset_type"]
revision_id = doc_asset["current_revision_id"]
classification_ids = semantic_store.insert_candidates(pid, [{
    "candidate_kind": "classification", "candidate_type": "requirements",
    "revision_id": revision_id, "stable_key": "",
    "title": "requirements", "statement": "requirements",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": [{"evidence_id": find_evidence(pid, "REQ-NC-017"),
                  "quote": "", "quote_hash": "", "role": "supports"}]}])
runtime.semantic.review_candidate(pid, classification_ids[0],
                                  decision="confirm", reviewer="r")
class_result = knowledge.promote_candidate(pid, classification_ids[0],
                                           actor="r", note="classify")
check("classification promotion succeeded as a claim",
      class_result["status"] == "promoted"
      and class_result["claim"]["claim_type"] == "classification")
check("classification promotion did not overwrite the asset type",
      db.get_asset(pid, doc_asset["id"])["asset_type"]
      == asset_type_before)

# -- revision-status promotion does not alter the revision -------------------
status_before = db.get_revision(pid, revision_id)["status"]
revision_status_ids = semantic_store.insert_candidates(pid, [{
    "candidate_kind": "revision-status", "candidate_type": "approved",
    "revision_id": revision_id, "stable_key": "",
    "title": "approved", "statement": "This revision appears approved.",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": [{"evidence_id": find_evidence(pid, "REQ-NC-017"),
                  "quote": "", "quote_hash": "", "role": "supports"}]}])
runtime.semantic.review_candidate(pid, revision_status_ids[0],
                                  decision="confirm", reviewer="r")
rs_result = knowledge.promote_candidate(pid, revision_status_ids[0],
                                        actor="r", note="revision status")
check("revision-status promotion creates a revision-status claim",
      rs_result["status"] == "promoted"
      and rs_result["claim"]["claim_type"] == "revision-status")
check("revision-status promotion did not alter asset_revisions.status",
      db.get_revision(pid, revision_id)["status"] == status_before)

# -- relation promotion ------------------------------------------------------
# a second promoted concept to serve as the relation target
target_pid_evidence = find_evidence(pid, "retried three times")
second_ids = semantic_store.insert_candidates(pid, [{
    "candidate_kind": "engineering-concept", "candidate_type": "requirement",
    "revision_id": revision_id, "stable_key": "REQ-NC-018",
    "title": "REQ-NC-018 retry", "statement":
        "Failed name checks must be retried three times.",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": [{"evidence_id": target_pid_evidence,
                  "quote": "retried three times", "quote_hash": "",
                  "role": "supports"}]}])
runtime.semantic.review_candidate(pid, second_ids[0], decision="confirm",
                                  reviewer="r")
second_result = knowledge.promote_candidate(pid, second_ids[0], actor="r",
                                            note="second concept")

# unresolved endpoint blocks
unresolved = insert_relation_candidate(
    pid, source_ref={"kind": "candidate", "id": partial,
                     "key": "REQ-NC-017"},
    target_ref={"kind": "candidate", "id": "sc_nonexistent",
                "key": "NO-SUCH-KEY"},
    relation_type="refines", evidence_id=target_pid_evidence,
    quote="retried three times", source_candidate_id=partial)
confirm_relation_candidate(runtime, pid, unresolved)
plan = knowledge.plan_relation_promotion(pid, unresolved)
check("relation candidate with an unresolved endpoint is blocked",
      not plan["eligible"]
      and not plan["target_resolution"]["resolved"])

# ambiguous endpoint blocks: two active entities holding the same alias key
amb_source = knowledge.create_entity(
    pid, entity_type="interface", canonical_key="interface:GET:/dup-a",
    display_name="dup-a",
    evidence=[{"evidence_id": target_pid_evidence}], actor="t",
    note="amb a")["entity"]
amb_target = knowledge.create_entity(
    pid, entity_type="interface", canonical_key="interface:GET:/dup-b",
    display_name="dup-b",
    evidence=[{"evidence_id": target_pid_evidence}], actor="t",
    note="amb b")["entity"]
with lock:
    for holder in (amb_source["id"], amb_target["id"]):
        conn.execute(
            "INSERT INTO engineering_entity_aliases (id,workspace_id,"
            "entity_id,alias,normalized_alias,alias_type,origin,status,"
            "evidence_id,created_knowledge_revision,created_at) VALUES "
            "(?,?,?,?,?,'identifier','manual','active','',0,'2026-01-01')",
            (db.new_id("al_"), pid, holder, "DUP-KEY", "dup-key"))
    conn.commit()
ambiguous = insert_relation_candidate(
    pid, source_ref={"kind": "candidate", "id": partial,
                     "key": "REQ-NC-017"},
    target_ref={"kind": "symbol", "id": "", "key": "DUP-KEY"},
    relation_type="refines", evidence_id=target_pid_evidence,
    quote="retried three times", source_candidate_id=partial)
confirm_relation_candidate(runtime, pid, ambiguous)
plan = knowledge.plan_relation_promotion(pid, ambiguous)
check("ambiguous endpoint blocks relation promotion",
      not plan["eligible"] and plan["target_resolution"]["ambiguous"])

# eligible relation candidate promotes to a confirmed canonical relation
eligible = insert_relation_candidate(
    pid, source_ref={"kind": "candidate", "id": second_ids[0],
                     "key": "REQ-NC-018"},
    target_ref={"kind": "candidate", "id": partial, "key": "REQ-NC-017"},
    relation_type="refines", evidence_id=target_pid_evidence,
    quote="retried three times", source_candidate_id=second_ids[0],
    target_candidate_id=partial)
plan = knowledge.plan_relation_promotion(pid, eligible)
check("unconfirmed relation candidate is blocked", not plan["eligible"])
confirm_relation_candidate(runtime, pid, eligible)
plan = knowledge.plan_relation_promotion(pid, eligible)
check("confirmed relation candidate with resolvable endpoints is eligible",
      plan["eligible"]
      and plan["source_resolution"]["via"] == "promotion"
      and plan["target_resolution"]["via"] == "promotion")
rel_result = knowledge.promote_relation(pid, eligible, actor="reviewer-1",
                                        note="endpoints verified")
relation = rel_result["relation"]
check("promoted relation is confirmed with semantic-promotion origin",
      relation["relation_state"] == "confirmed"
      and relation["origin"] == "semantic-promotion"
      and relation["promoted_from_relation_candidate_id"] == eligible)
check("promoted relation carries verified evidence",
      len(relation["evidence"]) >= 1)
check("relation promotion recorded promotion + decision",
      store.find_promotion(pid, "relation-candidate", eligible) is not None
      and any(d["decision_type"] == "promote-relation"
              for d in knowledge.list_decisions(pid)["decisions"]))

rel_again = knowledge.promote_relation(pid, eligible, actor="reviewer-1",
                                       note="again")
check("relation re-promotion is idempotent",
      rel_again["status"] == "already-promoted"
      and rel_again["relation"]["id"] == relation["id"]
      and rel_again["knowledge_revision"]
      == rel_result["knowledge_revision"])

# -- conflict candidates are not promotable ----------------------------------
conflict_ids = semantic_store.insert_conflicts(pid, [{
    "category": "requirement-design", "refs": [],
    "explanation": "fixture conflict", "confidence": "low",
    "evidence_status": "verified"}])
try:
    knowledge.plan_candidate_promotion(pid, conflict_ids[0])
    conflict_blocked = False
except CandidateNotFound:
    conflict_blocked = True
try:
    knowledge.plan_relation_promotion(pid, conflict_ids[0])
    conflict_blocked_rel = False
except CandidateNotFound:
    conflict_blocked_rel = True
check("conflict candidate cannot enter either promotion path",
      conflict_blocked and conflict_blocked_rel)

# review still does not promote (compatibility)
fresh = semantic_store.insert_candidates(pid, [{
    "candidate_kind": "engineering-concept", "candidate_type": "requirement",
    "revision_id": revision_id, "stable_key": "REQ-NC-099",
    "title": "REQ-NC-099", "statement": "A statement.",
    "confidence": "medium", "evidence_status": "verified",
    "evidence": [{"evidence_id": target_pid_evidence, "quote": "",
                  "quote_hash": "", "role": "supports"}]}])
entities_before = store.count_entities(pid)
runtime.semantic.review_candidate(pid, fresh[0], decision="confirm",
                                  reviewer="r")
check("semantic review confirm still promotes nothing",
      store.count_entities(pid) == entities_before
      and store.find_promotion(pid, "semantic-candidate", fresh[0]) is None)

finish()
