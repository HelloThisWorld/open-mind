"""Canonical Claims: evidence requirement, quote verification, dedup,
correction-by-supersede (never rewrite), explicit authority, lifecycle
queryability."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind.knowledge import store  # noqa: E402
from openmind.knowledge.errors import (GraphEvidenceInvalid,  # noqa: E402
                                       UnknownVocabularyValue)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

entity = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-017",
    display_name="REQ-NC-017", evidence=[{"evidence_id": evidence_id}],
    actor="tester", note="fixture")["entity"]

# -- evidence rules ----------------------------------------------------------
try:
    knowledge.create_claim(pid, entity_id=entity["id"],
                           claim_type="normative-statement",
                           statement="The service shall answer quickly.",
                           evidence=[], actor="tester", note="none")
    rejected = False
except GraphEvidenceInvalid:
    rejected = True
check("claim without evidence is rejected", rejected)

try:
    knowledge.create_claim(
        pid, entity_id=entity["id"], claim_type="normative-statement",
        statement="The service shall answer quickly.",
        evidence=[{"evidence_id": evidence_id,
                   "quote": "a quote that is not in the snapshot"}],
        actor="tester", note="fabricated")
    fabricated = False
except GraphEvidenceInvalid:
    fabricated = True
check("fabricated quote is rejected against the immutable snapshot",
      fabricated)

try:
    knowledge.create_claim(pid, entity_id=entity["id"],
                           claim_type="definitely-not-a-type",
                           statement="x",
                           evidence=[{"evidence_id": evidence_id}],
                           actor="tester", note="bad type")
    bad_type = False
except UnknownVocabularyValue:
    bad_type = True
check("invalid claim type rejected", bad_type)

created = knowledge.create_claim(
    pid, entity_id=entity["id"], claim_type="normative-statement",
    statement="The name check service shall answer within 2 seconds.",
    evidence=[{"evidence_id": evidence_id,
               "quote": "shall answer within 2 seconds"}],
    actor="tester", note="valid claim")
claim = created["claim"]
check("valid claim created with verified evidence join",
      len(claim["evidence"]) == 1
      and claim["evidence"][0]["evidence_id"] == evidence_id)
check("claim carries a normalized statement hash",
      len(claim["normalized_statement_hash"]) == 64)

# -- dedup -------------------------------------------------------------------
duplicate = knowledge.create_claim(
    pid, entity_id=entity["id"], claim_type="normative-statement",
    statement="The NAME CHECK service   shall answer within 2 seconds.",
    evidence=[{"evidence_id": evidence_id}], actor="tester", note="dup")
check("identical normalized claim deduplicates to the existing row",
      duplicate["deduplicated"] and duplicate["claim"]["id"] == claim["id"])
check("dedup minted no new knowledge revision",
      duplicate["knowledge_revision"] == created["knowledge_revision"])

# -- correction: new claim + supersede, never a rewrite ----------------------
corrected = knowledge.create_claim(
    pid, entity_id=entity["id"], claim_type="normative-statement",
    statement="The name check service shall answer within 3 seconds.",
    evidence=[{"evidence_id": evidence_id}], actor="tester",
    note="corrected budget")
check("changed statement creates a NEW claim",
      corrected["claim"]["id"] != claim["id"])
superseded = knowledge.supersede_object(
    pid, kind="claim", object_id=claim["id"],
    replacement_id=corrected["claim"]["id"], actor="tester",
    note="3s supersedes 2s")
old = store.get_claim(pid, claim["id"])
check("old claim is superseded, not overwritten",
      old["lifecycle_status"] == "superseded"
      and old["superseded_by_claim_id"] == corrected["claim"]["id"]
      and old["statement"].endswith("2 seconds."))
check("superseded claim remains queryable with its evidence",
      len(old["evidence"]) == 1)
check("supersede minted its own knowledge revision",
      superseded["knowledge_revision"] > corrected["knowledge_revision"])

# -- authority is explicit ---------------------------------------------------
check("new claim starts with authority unknown",
      corrected["claim"]["authority_status"] == "unknown")
marked = knowledge.set_authority(pid, kind="claim",
                                 object_id=corrected["claim"]["id"],
                                 authority="authoritative", actor="lead",
                                 note="approved by review board")
check("authority change is explicit and recorded",
      store.get_claim(pid, corrected["claim"]["id"])["authority_status"]
      == "authoritative")
authority_decisions = knowledge.list_decisions(
    pid, decision_type="mark-authoritative")["decisions"]
check("authority change recorded a decision",
      any(d["target_id"] == corrected["claim"]["id"]
          for d in authority_decisions))

# -- lifecycle queryability --------------------------------------------------
active = knowledge.list_claims(pid, entity_id=entity["id"])
check("active listing excludes the superseded claim",
      all(c["id"] != claim["id"] for c in active["claims"]))
history = knowledge.list_claims(pid, entity_id=entity["id"],
                                lifecycle_status="superseded")
check("superseded claim listed under its lifecycle",
      any(c["id"] == claim["id"] for c in history["claims"]))

withdrawn = knowledge.create_claim(
    pid, entity_id=entity["id"], claim_type="implementation-note",
    statement="A note that will be withdrawn.",
    evidence=[{"evidence_id": evidence_id}], actor="tester", note="w")
knowledge.withdraw_object(pid, kind="claim",
                          object_id=withdrawn["claim"]["id"],
                          actor="tester", note="withdrawn in test")
check("withdrawn claim excluded from active queries",
      all(c["id"] != withdrawn["claim"]["id"]
          for c in knowledge.list_claims(pid,
                                         entity_id=entity["id"])["claims"]))
check("withdrawn claim remains queryable by id",
      store.get_claim(pid, withdrawn["claim"]["id"])["lifecycle_status"]
      == "withdrawn")

finish()
