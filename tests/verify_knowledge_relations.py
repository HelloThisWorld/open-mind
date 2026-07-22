"""Canonical Relations: endpoint existence and scoping, closed types,
manual-state restrictions, self-relation rejection, dedup, reject/restore,
`possibly-related` never upgraded."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind.domain.errors import InvalidRequest  # noqa: E402
from openmind.knowledge import store  # noqa: E402
from openmind.knowledge.errors import (EntityNotFound,  # noqa: E402
                                       GraphConflict,
                                       UnknownVocabularyValue)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
other_pid = make_minimal_workspace(runtime, name="kg-rel-other")
evidence_id = find_evidence(pid, "REQ-NC-017")
other_evidence = find_evidence(other_pid, "REQ-NC-017")


def make_entity(workspace, key, entity_type="requirement",
                evidence=None) -> dict:
    return knowledge.create_entity(
        workspace, entity_type=entity_type,
        canonical_key=f"{entity_type}:{key}", display_name=key,
        evidence=[{"evidence_id": evidence or evidence_id}],
        actor="tester", note="fixture")["entity"]


source = make_entity(pid, "REQ-NC-017")
target = make_entity(pid, "REQ-NC-018")
foreign = make_entity(other_pid, "REQ-FF-001", evidence=other_evidence)

# -- endpoint rules ----------------------------------------------------------
try:
    knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id="ent_missing",
        relation_type="refines", relation_state="confirmed",
        evidence=[{"evidence_id": evidence_id}], actor="t", note="n")
    missing_ok = False
except EntityNotFound:
    missing_ok = True
check("relation endpoint must exist", missing_ok)

try:
    knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id=foreign["id"],
        relation_type="refines", relation_state="confirmed",
        evidence=[{"evidence_id": evidence_id}], actor="t", note="n")
    cross_ok = False
except EntityNotFound:
    cross_ok = True
check("cross-workspace endpoint is rejected", cross_ok)

try:
    knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id=target["id"],
        relation_type="depends-on", relation_state="confirmed",
        evidence=[{"evidence_id": evidence_id}], actor="t", note="n")
    unsupported_ok = False
except UnknownVocabularyValue:
    unsupported_ok = True
check("unsupported relation type (depends-on) is rejected", unsupported_ok)

try:
    knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id=source["id"],
        relation_type="refines", relation_state="confirmed",
        evidence=[{"evidence_id": evidence_id}], actor="t", note="n")
    self_ok = False
except GraphConflict:
    self_ok = True
check("self-relation is rejected", self_ok)

try:
    knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id=target["id"],
        relation_type="refines", relation_state="inferred",
        evidence=[{"evidence_id": evidence_id}], actor="t", note="n")
    inferred_ok = False
except InvalidRequest:
    inferred_ok = True
check("manual 'inferred' state is rejected (no analyzer origin)",
      inferred_ok)

created = knowledge.create_relation(
    pid, source_entity_id=source["id"], target_entity_id=target["id"],
    relation_type="refines", relation_state="confirmed",
    evidence=[{"evidence_id": evidence_id,
               "quote": "shall answer within 2 seconds"}],
    actor="tester", note="manual relation with evidence")
relation = created["relation"]
check("manual relation created as confirmed with origin=manual",
      relation["relation_state"] == "confirmed"
      and relation["origin"] == "manual")
check("relation carries its verified evidence join",
      len(relation["evidence"]) == 1)

# -- idempotent identity -----------------------------------------------------
duplicate = knowledge.create_relation(
    pid, source_entity_id=source["id"], target_entity_id=target["id"],
    relation_type="refines", relation_state="confirmed",
    evidence=[{"evidence_id": evidence_id}], actor="tester", note="dup")
check("duplicate active relation deduplicates to the existing row",
      duplicate["deduplicated"]
      and duplicate["relation"]["id"] == relation["id"])
check("dedup minted no new knowledge revision",
      duplicate["knowledge_revision"] == created["knowledge_revision"])

# -- possibly-related is never upgraded --------------------------------------
weak = knowledge.create_relation(
    pid, source_entity_id=target["id"], target_entity_id=source["id"],
    relation_type="possibly-related", relation_state="confirmed",
    evidence=[{"evidence_id": evidence_id}], actor="tester", note="weak")
check("possibly-related stays possibly-related",
      weak["relation"]["relation_type"] == "possibly-related")
check("possibly-related and implements are distinct identity tuples",
      store.find_active_relation(pid, target["id"], source["id"],
                                 "implements") is None)

# -- reject / restore --------------------------------------------------------
rejected = knowledge.reject_relation(pid, relation_id=relation["id"],
                                     actor="reviewer", note="not real")
check("rejected relation state applied",
      store.get_relation(pid, relation["id"])["relation_state"]
      == "rejected")
check("rejected relation is retained and queryable",
      store.get_relation(pid, relation["id"]) is not None)
check("rejected relation keeps lifecycle active (governance history)",
      store.get_relation(pid, relation["id"])["lifecycle_status"]
      == "active")
listing = knowledge.list_relations(pid, relation_state="confirmed")
check("rejected relation excluded from confirmed-state listing",
      all(r["id"] != relation["id"] for r in listing["relations"]))

restored = knowledge.restore_relation(pid, relation_id=relation["id"],
                                      actor="reviewer", note="was real")
check("restore returns the prior epistemic state",
      restored["relation_state"] == "confirmed"
      and store.get_relation(pid, relation["id"])["relation_state"]
      == "confirmed")

# -- supersede / withdraw ----------------------------------------------------
replacement = knowledge.create_relation(
    pid, source_entity_id=source["id"], target_entity_id=target["id"],
    relation_type="implements", relation_state="explicit",
    evidence=[{"evidence_id": evidence_id}], actor="tester",
    note="replacement")["relation"]
knowledge.supersede_object(pid, kind="relation", object_id=relation["id"],
                           replacement_id=replacement["id"], actor="tester",
                           note="superseded in test")
old = store.get_relation(pid, relation["id"])
check("superseded relation points at its replacement and stays queryable",
      old["lifecycle_status"] == "superseded"
      and old["superseded_by_relation_id"] == replacement["id"])

knowledge.withdraw_object(pid, kind="relation",
                          object_id=weak["relation"]["id"], actor="tester",
                          note="withdrawn")
check("withdrawn relation excluded from active listing",
      all(r["id"] != weak["relation"]["id"] for r in
          knowledge.list_relations(pid)["relations"]))

finish()
