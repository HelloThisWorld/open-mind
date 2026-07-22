"""Human Decision audit: every governance write records one, actor is
caller-supplied (never inferred), notes and snapshots bounded, decisions
immutable, no secret material stored."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind import db  # noqa: E402
from openmind.domain.errors import InvalidRequest  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

entity = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-017",
    display_name="REQ-NC-017", evidence=[{"evidence_id": evidence_id}],
    actor="alice-reviewer", note="created in decision test")["entity"]
claim = knowledge.create_claim(
    pid, entity_id=entity["id"], claim_type="normative-statement",
    statement="The name check service shall answer within 2 seconds.",
    evidence=[{"evidence_id": evidence_id}], actor="", note="empty actor")
knowledge.add_alias(pid, entity_id=entity["id"], alias="NC-Requirement",
                    alias_type="name", actor="alice-reviewer", note="alias")
knowledge.set_authority(pid, kind="entity", object_id=entity["id"],
                        authority="authoritative", actor="lead",
                        note="board approval")
knowledge.withdraw_object(pid, kind="claim",
                          object_id=claim["claim"]["id"],
                          actor="alice-reviewer", note="withdrawn")

decisions = knowledge.list_decisions(pid)["decisions"]
types = [d["decision_type"] for d in decisions]
check("every governance write recorded a decision",
      {"create-entity", "create-claim", "add-alias", "mark-authoritative",
       "withdraw"} <= set(types))
check("one decision per mutation (5 writes, 5 decisions)",
      len(decisions) == 5)
check("decisions link to their knowledge revision",
      all(d["knowledge_revision_id"] for d in decisions))

by_type = {d["decision_type"]: d for d in decisions}
check("actor recorded verbatim, never inferred",
      by_type["create-entity"]["actor"] == "alice-reviewer")
check("empty actor stays empty (identity is never invented)",
      by_type["create-claim"]["actor"] == "")
check("before/after snapshots are structured and bounded",
      isinstance(by_type["mark-authoritative"]["before"], dict)
      and by_type["mark-authoritative"]["before"]["authority_status"]
      == "unknown"
      and by_type["mark-authoritative"]["after"]["authority_status"]
      == "authoritative")
check("decision records the invoking command channel",
      all("source_command" in d for d in decisions))

# note bound
try:
    knowledge.set_authority(pid, kind="entity", object_id=entity["id"],
                            authority="informational", actor="x",
                            note="n" * 5000)
    bounded = False
except InvalidRequest:
    bounded = True
check("oversized note rejected before any write", bounded)

# snapshots bounded: entity snapshot carries graph fields only
snapshot = by_type["create-entity"]["after"]
check("entity snapshot is field-bounded (no free payload dump)",
      set(snapshot) <= {"id", "entity_type", "canonical_key",
                        "display_name", "lifecycle_status",
                        "authority_status", "origin",
                        "merged_into_entity_id",
                        "superseded_by_entity_id"})

# no secret material in any stored decision JSON
conn, lock = db.shared_connection()
with lock:
    rows = conn.execute("SELECT before_json, after_json, note FROM "
                        "knowledge_decisions").fetchall()
blob = json.dumps([tuple(r) for r in rows]).lower()
check("no credential material in stored decisions",
      "api_key" not in blob and "authorization" not in blob
      and "bearer " not in blob)

# immutability: a later flurry of writes leaves earlier rows byte-identical
with lock:
    before_rows = {r["id"]: tuple(r) for r in conn.execute(
        "SELECT * FROM knowledge_decisions").fetchall()}
knowledge.set_authority(pid, kind="entity", object_id=entity["id"],
                        authority="informational", actor="x", note="later")
with lock:
    after_rows = {r["id"]: tuple(r) for r in conn.execute(
        "SELECT * FROM knowledge_decisions").fetchall()}
check("earlier decisions are immutable across later writes",
      all(after_rows[i] == before_rows[i] for i in before_rows))
check("the later write added its own decision",
      len(after_rows) == len(before_rows) + 1)

# filtered listing
filtered = knowledge.list_decisions(pid, target_kind="entity",
                                    target_id=entity["id"])["decisions"]
check("decision listing filters by target",
      filtered and all(d["target_id"] == entity["id"] for d in filtered))

finish()
