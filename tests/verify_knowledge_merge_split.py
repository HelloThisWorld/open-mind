"""Entity merge and split: transactional, auditable, nothing deleted,
duplicates deduplicated, self-relations eliminated, invalid input aborts
whole."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind.knowledge import store  # noqa: E402
from openmind.knowledge.errors import GraphConflict  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")
evidence_two = find_evidence(pid, "retried three times")


def entity(key, entity_type="requirement"):
    return knowledge.create_entity(
        pid, entity_type=entity_type, canonical_key=f"{entity_type}:{key}",
        display_name=key, evidence=[{"evidence_id": evidence_id}],
        actor="t", note="fixture")["entity"]


# -- merge scenario ----------------------------------------------------------
source = entity("REQ-DUP-A")
target = entity("REQ-NC-017")
third = entity("REQ-NC-018")

knowledge.add_alias(pid, entity_id=source["id"], alias="duplicate-req",
                    alias_type="name", actor="t", note="a")
# an alias the TARGET also holds -> superseded on move, not duplicated
knowledge.add_alias(pid, entity_id=source["id"], alias="shared-name",
                    alias_type="name", actor="t", note="a")
# a colliding alias held by a THIRD entity -> stays on source, reported
knowledge.add_alias(pid, entity_id=third["id"], alias="third-name",
                    alias_type="name", actor="t", note="a")

source_claim = knowledge.create_claim(
    pid, entity_id=source["id"], claim_type="normative-statement",
    statement="A statement unique to the duplicate.",
    evidence=[{"evidence_id": evidence_id}], actor="t", note="c")["claim"]
shared_statement = "The name check service shall answer within 2 seconds."
knowledge.create_claim(pid, entity_id=source["id"],
                       claim_type="normative-statement",
                       statement=shared_statement,
                       evidence=[{"evidence_id": evidence_id}],
                       actor="t", note="c")
target_shared = knowledge.create_claim(
    pid, entity_id=target["id"], claim_type="normative-statement",
    statement=shared_statement,
    evidence=[{"evidence_id": evidence_id}], actor="t", note="c")["claim"]

# relations: source->third (rewires), source->target (becomes self, removed),
# and a duplicate-to-be: source->third refines already exists on target too
knowledge.create_relation(pid, source_entity_id=source["id"],
                          target_entity_id=third["id"],
                          relation_type="refines",
                          relation_state="confirmed",
                          evidence=[{"evidence_id": evidence_id}],
                          actor="t", note="r")
knowledge.create_relation(pid, source_entity_id=source["id"],
                          target_entity_id=target["id"],
                          relation_type="possibly-related",
                          relation_state="confirmed",
                          evidence=[{"evidence_id": evidence_id}],
                          actor="t", note="r")
knowledge.create_relation(pid, source_entity_id=target["id"],
                          target_entity_id=third["id"],
                          relation_type="refines",
                          relation_state="confirmed",
                          evidence=[{"evidence_id": evidence_id}],
                          actor="t", note="r")

# alias merge collision setup: give SOURCE an alias the THIRD entity holds
conn_alias = knowledge.add_alias(pid, entity_id=source["id"],
                                 alias="THIRD-NAME", alias_type="name",
                                 actor="t", note="will collide on merge") \
    if False else None
# (adding it via service would collide immediately; write through the store
# transaction to model a legacy collision that merge must handle)
from openmind import db  # noqa: E402
conn, lock = db.shared_connection()
with lock:
    conn.execute(
        "INSERT INTO engineering_entity_aliases (id,workspace_id,entity_id,"
        "alias,normalized_alias,alias_type,origin,status,evidence_id,"
        "created_knowledge_revision,created_at) VALUES (?,?,?,?,?,'name',"
        "'manual','active','',0,'2026-01-01')",
        (db.new_id("al_"), pid, source["id"], "Third-Name", "third-name"))
    conn.commit()

revision_before = store.current_revision_number(pid)
decisions_before = len(knowledge.list_decisions(pid)["decisions"])
result = knowledge.merge_entities(pid, source_entity_id=source["id"],
                                  target_entity_id=target["id"],
                                  actor="merger", note="merge duplicates")

merged_source = store.get_entity(pid, source["id"])
check("source became merged and points at the target",
      merged_source["lifecycle_status"] == "merged"
      and merged_source["merged_into_entity_id"] == target["id"])
check("source remains addressable (never deleted)",
      knowledge.get_entity(pid, source["id"])["id"] == source["id"])

target_aliases = {a["normalized_alias"]: a for a in
                  store.list_aliases(pid, target["id"])}
check("non-colliding alias moved to the target",
      "duplicate-req" in target_aliases)
check("target-shared alias superseded instead of duplicated",
      "shared-name" in target_aliases
      and len([a for a in store.list_aliases(pid, target["id"], status=None)
               if a["normalized_alias"] == "shared-name"]) == 1)
check("externally colliding alias stayed off the target and was reported",
      "third-name" not in target_aliases
      and any(c["alias"] == "Third-Name"
              for c in result["alias_collisions"]))

target_claims = store.list_claims(pid, entity_id=target["id"],
                                  lifecycle_status=None, limit=100)
check("unique claim moved to the target",
      any(c["id"] == source_claim["id"] for c in target_claims))
check("duplicate claim deduplicated by superseding, evidence intact",
      result["claims_deduplicated"] == 1
      and any(c["superseded_by_claim_id"] == target_shared["id"]
              for c in target_claims if c["id"] != target_shared["id"]
              and c["normalized_statement_hash"]
              == target_shared["normalized_statement_hash"]))

target_relations = store.list_relations(pid, entity_id=target["id"],
                                        lifecycle_status=None, limit=100)
check("relations rewired to the target",
      any(r["source_entity_id"] == target["id"]
          and r["target_entity_id"] == third["id"]
          for r in target_relations))
check("would-be self-relation was eliminated, not kept",
      not any(r["source_entity_id"] == r["target_entity_id"]
              for r in target_relations)
      and result["self_relations_removed"] == 1)
check("duplicate relation deduplicated",
      result["relations_deduplicated"] == 1
      and len([r for r in target_relations
               if r["relation_type"] == "refines"
               and r["lifecycle_status"] == "active"
               and r["source_entity_id"] == target["id"]]) == 1)
check("merge minted exactly one revision and one decision",
      store.current_revision_number(pid) == revision_before + 1
      and len(knowledge.list_decisions(pid)["decisions"])
      == decisions_before + 1)
check("merge decision recorded with actor",
      knowledge.list_decisions(
          pid, decision_type="merge-entity")["decisions"][0]["actor"]
      == "merger")

merge_into_active = store.get_entity(pid, target["id"])
check("merge target remains active", merge_into_active["lifecycle_status"]
      == "active")

# merging into a merged entity is rejected
try:
    knowledge.merge_entities(pid, source_entity_id=third["id"],
                             target_entity_id=source["id"], actor="m",
                             note="bad")
    bad_merge = False
except GraphConflict:
    bad_merge = True
check("merging into a merged entity is rejected", bad_merge)

# -- split scenario ----------------------------------------------------------
split_source = entity("REQ-SPLIT", entity_type="workflow")
keep_claim = knowledge.create_claim(
    pid, entity_id=split_source["id"], claim_type="workflow-step",
    statement="Step one stays on the source.",
    evidence=[{"evidence_id": evidence_two}], actor="t", note="c")["claim"]
move_claim = knowledge.create_claim(
    pid, entity_id=split_source["id"], claim_type="workflow-step",
    statement="Step two moves to the new entity.",
    evidence=[{"evidence_id": evidence_two}], actor="t", note="c")["claim"]
bindings = store.list_bindings(pid, split_source["id"])
move_binding = bindings[0]
rel_to_rewrite = knowledge.create_relation(
    pid, source_entity_id=split_source["id"], target_entity_id=third["id"],
    relation_type="refines", relation_state="confirmed",
    evidence=[{"evidence_id": evidence_two}], actor="t", note="r")["relation"]

# invalid moved id aborts the whole transaction
revision_before = store.current_revision_number(pid)
try:
    knowledge.split_entity(
        pid, source_entity_id=split_source["id"],
        new_entity_type="workflow",
        new_canonical_key="workflow:derived:step-two",
        new_display_name="Step two", claim_ids=[move_claim["id"],
                                                "clm_not_yours"],
        binding_ids=[], relation_rewrites=[], actor="s", note="bad")
    invalid_blocked = False
except GraphConflict:
    invalid_blocked = True
check("an invalid moved claim id blocks the whole split", invalid_blocked)
check("no partial split happened",
      store.current_revision_number(pid) == revision_before
      and store.find_entity_by_key(pid, "workflow",
                                   "workflow:derived:step-two") is None)

split = knowledge.split_entity(
    pid, source_entity_id=split_source["id"], new_entity_type="workflow",
    new_canonical_key="workflow:derived:step-two",
    new_display_name="Step two", claim_ids=[move_claim["id"]],
    binding_ids=[move_binding["id"]],
    relation_rewrites=[{"relation_id": rel_to_rewrite["id"],
                        "end": "source"}],
    actor="splitter", note="explicit split")
new_entity = split["new_entity"]
check("explicit claim moved to the new entity",
      store.get_claim(pid, move_claim["id"])["entity_id"]
      == new_entity["id"])
check("unspecified claim stayed on the source",
      store.get_claim(pid, keep_claim["id"])["entity_id"]
      == split_source["id"])
check("explicit binding moved",
      any(b["id"] == move_binding["id"]
          for b in store.list_bindings(pid, new_entity["id"])))
check("listed relation endpoint rewired to the new entity",
      store.get_relation(pid, rel_to_rewrite["id"])["source_entity_id"]
      == new_entity["id"])
check("source entity remains active after split",
      store.get_entity(pid, split_source["id"])["lifecycle_status"]
      == "active")
check("split minted one revision and one split decision",
      split["knowledge_revision"] == revision_before + 1
      and any(d["decision_type"] == "split-entity"
              and d["actor"] == "splitter"
              for d in knowledge.list_decisions(pid)["decisions"]))

# a split must move something
try:
    knowledge.split_entity(
        pid, source_entity_id=split_source["id"],
        new_entity_type="workflow",
        new_canonical_key="workflow:derived:empty-split",
        new_display_name="empty", claim_ids=[], binding_ids=[],
        relation_rewrites=[], actor="s", note="nothing moves")
    empty_blocked = False
except Exception:
    empty_blocked = True
check("a split moving nothing is rejected", empty_blocked)

finish()
