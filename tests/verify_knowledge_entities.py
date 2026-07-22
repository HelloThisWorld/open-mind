"""Canonical Entities: deterministic keys, identity uniqueness, workspace
scoping, closed vocabulary, evidence requirement for manual creation,
aliases with reported collisions, bindings."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind.domain.errors import InvalidRequest  # noqa: E402
from openmind.knowledge import identity, store  # noqa: E402
from openmind.knowledge.errors import (AliasCollision,  # noqa: E402
                                       EntityNotFound, GraphConflict,
                                       GraphEvidenceInvalid,
                                       UnknownVocabularyValue)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
other_pid = runtime.workspaces.create("kg-other")["id"]
evidence_id = find_evidence(pid, "REQ-NC-017")

# -- deterministic canonical keys -------------------------------------------
check("identifier key preserves identifier case",
      identity.identifier_entity_key("requirement", "REQ-NC-017")
      == "requirement:REQ-NC-017")
check("derived key is normalized and marked derived",
      identity.derived_entity_key("workflow", "Nightly  Batch Run")
      == "workflow:derived:nightly-batch-run")
check("derived key is deterministic",
      identity.derived_entity_key("workflow", "Nightly  Batch Run")
      == identity.derived_entity_key("workflow", "Nightly Batch  Run"))
check("interface key upper-cases the method",
      identity.interface_entity_key("post", "/name-check")
      == "interface:POST:/name-check")
check("symbol key preserves symbol case",
      identity.symbol_entity_key("a_1", "org.example.NameCheckService#run()")
      == "code-symbol:asset:a_1:org.example.NameCheckService#run()")
check("looks_like_identifier accepts REQ-NC-017",
      identity.looks_like_identifier("REQ-NC-017"))
check("looks_like_identifier rejects a prose title",
      not identity.looks_like_identifier("The service shall respond"))

# -- manual creation ---------------------------------------------------------
try:
    knowledge.create_entity(pid, entity_type="requirement",
                            canonical_key="requirement:REQ-NC-017",
                            display_name="REQ-NC-017", evidence=[],
                            actor="tester", note="no evidence")
    no_evidence_rejected = False
except GraphEvidenceInvalid:
    no_evidence_rejected = True
check("manual entity without evidence is rejected", no_evidence_rejected)

try:
    knowledge.create_entity(pid, entity_type="not-a-type",
                            canonical_key="x:y", display_name="x",
                            evidence=[{"evidence_id": evidence_id}],
                            actor="tester", note="bad type")
    bad_type_rejected = False
except UnknownVocabularyValue:
    bad_type_rejected = True
check("invalid entity type is rejected at the write boundary",
      bad_type_rejected)

created = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-017",
    display_name="REQ-NC-017",
    evidence=[{"evidence_id": evidence_id,
               "quote": "shall answer within 2 seconds"}],
    actor="tester", note="manual create")
entity = created["entity"]
check("manual entity created with origin=manual",
      entity["origin"] == "manual"
      and entity["canonical_key"] == "requirement:REQ-NC-017")
check("manual entity creation minted a knowledge revision",
      created["knowledge_revision"] >= 1)
check("evidence anchored as a binding",
      any(b["ref_kind"] == "evidence" and b["ref_id"] == evidence_id
          for b in store.list_bindings(pid, entity["id"])))

try:
    knowledge.create_entity(
        pid, entity_type="requirement",
        canonical_key="requirement:REQ-NC-017", display_name="dup",
        evidence=[{"evidence_id": evidence_id}], actor="tester", note="dup")
    duplicate_rejected = False
except GraphConflict:
    duplicate_rejected = True
check("duplicate canonical key is a reported conflict, not a silent row",
      duplicate_rejected)
check("identity uniqueness held (one entity for the key)",
      len([e for e in store.list_entities(pid, lifecycle_status=None,
                                          limit=100)
           if e["canonical_key"] == "requirement:REQ-NC-017"]) == 1)

# fabricated evidence quote is rejected
try:
    knowledge.create_entity(
        pid, entity_type="constraint", canonical_key="constraint:C-1",
        display_name="C-1",
        evidence=[{"evidence_id": evidence_id,
                   "quote": "this text does not exist in the evidence"}],
        actor="tester", note="fabricated quote")
    fabricated_rejected = False
except GraphEvidenceInvalid:
    fabricated_rejected = True
check("fabricated evidence quote is rejected", fabricated_rejected)

# evidence from another workspace is rejected
try:
    knowledge.create_entity(
        other_pid, entity_type="requirement",
        canonical_key="requirement:REQ-X", display_name="REQ-X",
        evidence=[{"evidence_id": evidence_id}], actor="tester",
        note="cross-workspace evidence")
    cross_evidence_rejected = False
except GraphEvidenceInvalid:
    cross_evidence_rejected = True
check("evidence of another workspace is rejected", cross_evidence_rejected)

# -- workspace scoping -------------------------------------------------------
check("entity does not resolve through another workspace",
      store.get_entity(other_pid, entity["id"]) is None)
try:
    knowledge.get_entity(other_pid, entity["id"])
    cross_blocked = False
except EntityNotFound:
    cross_blocked = True
check("service read through another workspace is typed not-found",
      cross_blocked)

# -- aliases -----------------------------------------------------------------
added = knowledge.add_alias(pid, entity_id=entity["id"],
                            alias="Name Check Requirement",
                            alias_type="name", actor="tester", note="alias")
check("alias added with normalized form",
      added["alias"]["normalized_alias"] == "name check requirement")
check("alias is indexed for exact lookup",
      [e["id"] for e in store.find_entity_by_alias(
          pid, "name check requirement")] == [entity["id"]])

second = knowledge.create_entity(
    pid, entity_type="business-rule", canonical_key="business-rule:BR-1",
    display_name="BR-1", evidence=[{"evidence_id": evidence_id}],
    actor="tester", note="second entity")
try:
    knowledge.add_alias(pid, entity_id=second["entity"]["id"],
                        alias="NAME CHECK requirement", alias_type="name",
                        actor="tester", note="collide")
    collision_reported = False
except AliasCollision as exc:
    collision_reported = bool(exc.holders)
check("normalized alias collision across entities is reported, never "
      "silently attached", collision_reported)
check("colliding alias did not attach",
      not any(a["alias"] == "NAME CHECK requirement"
              for a in store.list_aliases(pid, second["entity"]["id"])))

removed = knowledge.remove_alias(pid, entity_id=entity["id"],
                                 alias="Name Check Requirement",
                                 actor="tester", note="remove")
check("alias removal reports the count", removed["removed"] == 1)
check("removed alias stays auditable with status=removed",
      any(a["status"] == "removed" for a in store.list_aliases(
          pid, entity["id"], status=None)))
check("removed alias no longer resolves",
      store.find_entity_by_alias(pid, "name check requirement") == [])

# same alias on the SAME entity deduplicates instead of colliding
knowledge.add_alias(pid, entity_id=entity["id"], alias="ReqNC17",
                    alias_type="acronym", actor="tester", note="a")
again = knowledge.add_alias(pid, entity_id=entity["id"], alias="reqnc17",
                            alias_type="acronym", actor="tester", note="b")
check("re-adding an alias to the same entity deduplicates",
      again.get("deduplicated") is True)

# note bound enforced
try:
    knowledge.add_alias(pid, entity_id=entity["id"], alias="overlong",
                        alias_type="name", actor="t", note="x" * 3000)
    note_bounded = False
except InvalidRequest:
    note_bounded = True
check("governance note is bounded", note_bounded)

finish()
