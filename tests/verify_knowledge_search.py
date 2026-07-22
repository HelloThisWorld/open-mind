"""Graph search + vector projection: exact-over-similar precedence, stale
filtering, separate Entity/Claim sections, collection isolation and
lifecycle (merge removal, terminate/delete, orphan recognition)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind import vectorstore  # noqa: E402
from openmind.knowledge import store, vector_projection  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

exact = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-017",
    display_name="REQ-NC-017", evidence=[{"evidence_id": evidence_id}],
    actor="t", note="fixture",
    description="The name check service response budget.")["entity"]
knowledge.add_alias(pid, entity_id=exact["id"], alias="response budget rule",
                    alias_type="name", actor="t", note="a")
similar = knowledge.create_entity(
    pid, entity_type="requirement",
    canonical_key="requirement:REQ-NC-0170", display_name="REQ-NC-0170",
    evidence=[{"evidence_id": evidence_id}], actor="t", note="fixture",
    description="A requirement about answering name check queries fast — "
                "reads like the other one but is not it.")["entity"]
claim = knowledge.create_claim(
    pid, entity_id=exact["id"], claim_type="normative-statement",
    statement="REQ-NC-017 requires an answer within 2 seconds.",
    evidence=[{"evidence_id": evidence_id,
               "quote": "shall answer within 2 seconds"}],
    actor="t", note="claim")["claim"]

# -- exact canonical key ------------------------------------------------------
result = knowledge.search_entities(pid, "requirement:REQ-NC-017")
check("exact canonical key search ranks the key match first",
      result["entities"]
      and result["entities"][0]["id"] == exact["id"]
      and result["entities"][0]["matched_via"] == "canonical-key")

# -- exact alias --------------------------------------------------------------
result = knowledge.search_entities(pid, "response budget rule")
check("exact alias search resolves through the alias index",
      result["entities"]
      and result["entities"][0]["id"] == exact["id"]
      and result["entities"][0]["matched_via"] == "alias")

# -- exact identifier outranks similar text ----------------------------------
result = knowledge.search_entities(pid, "REQ-NC-017")
first = result["entities"][0]
check("exact identifier outranks the similar-looking entity",
      first["id"] == exact["id"]
      and (len(result["entities"]) == 1
           or result["entities"][1]["score"] < first["score"]))
check("REQ-NC-017 never matches REQ-NC-0170 as an exact token",
      all(e["canonical_key"] != "requirement:REQ-NC-0170"
          or e["matched_via"] in ("vector", "lexical")
          for e in result["entities"]))

# -- separate sections + navigability ----------------------------------------
check("entities and claims are separate result sections",
      isinstance(result["entities"], list)
      and isinstance(result["claims"], list))
check("claim hits carry evidence ids",
      result["claims"]
      and all(hit["evidence_ids"] for hit in result["claims"]))
check("entity hits carry navigable claim ids",
      any(claim["id"] in hit["claim_ids"] for hit in result["entities"]
          if hit["id"] == exact["id"]))
check("search result carries the current knowledge revision",
      result["knowledge_revision"]
      == store.current_revision_number(pid))

# -- stale filtering ----------------------------------------------------------
knowledge.withdraw_object(pid, kind="entity", object_id=similar["id"],
                          actor="t", note="w")
result = knowledge.search_entities(pid, "name check")
check("withdrawn entity excluded from default search",
      all(hit["id"] != similar["id"] for hit in result["entities"]))
result = knowledge.search_entities(pid, "name check", include_stale=True)
check("include_stale surfaces the withdrawn entity",
      any(hit["id"] == similar["id"] for hit in result["entities"]))

# -- vector projection isolation ---------------------------------------------
name = vector_projection.collection_name(pid)
check("graph collection is knowledge_<workspace-id>",
      name == f"knowledge_{pid}")
counts = vector_projection.refresh_workspace(pid)
check("entities and claims are indexed in the graph collection",
      counts["indexed"] >= 2
      and vectorstore.count_collection(name) == counts["indexed"])
collection = vectorstore.get_store(name)
stored = collection.get()
kinds = {(m or {}).get("object_kind") for m in stored["metadatas"]}
check("entity and claim projections are separate objects",
      {"entity", "claim"} <= kinds)
check("projection metadata carries workspace scoping and lifecycle",
      all((m or {}).get("workspace_id") == pid
          and (m or {}).get("lifecycle_status") == "active"
          for m in stored["metadatas"]))
check("no graph object entered the code collection",
      not set(stored["ids"])
      & set(vectorstore.get_code_store(pid).get()["ids"]))
check("no graph object entered the documents collection",
      not set(stored["ids"])
      & set(vectorstore.get_documents_store(pid).get()["ids"]))

# changed claim updates the projection
knowledge.supersede_object(
    pid, kind="claim", object_id=claim["id"],
    replacement_id=knowledge.create_claim(
        pid, entity_id=exact["id"], claim_type="normative-statement",
        statement="REQ-NC-017 requires an answer within 3 seconds.",
        evidence=[{"evidence_id": evidence_id}], actor="t",
        note="replacement")["claim"]["id"],
    actor="t", note="supersede")
stored = vectorstore.get_store(name).get()
check("superseded claim left the active projection",
      claim["id"] not in stored["ids"])

# merge removes the source's active projection
merge_source = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-MERGE",
    display_name="REQ-MERGE", evidence=[{"evidence_id": evidence_id}],
    actor="t", note="m")["entity"]
vector_projection.refresh_workspace(pid)
check("merge source is projected while active",
      merge_source["id"] in vectorstore.get_store(name).get()["ids"])
knowledge.merge_entities(pid, source_entity_id=merge_source["id"],
                         target_entity_id=exact["id"], actor="t", note="m")
check("merge removed the source's active projection",
      merge_source["id"] not in vectorstore.get_store(name).get()["ids"])

# -- collection lifecycle -----------------------------------------------------
check("knowledge_ prefix registered for orphan/delete handling",
      f"knowledge_{pid}" in vectorstore.project_collection_names(pid))
check("orphan sweep does not treat a live workspace's graph collection as "
      "an orphan",
      vectorstore.drop_orphan_collections({pid}) == [])

from openmind import jobs  # noqa: E402
jobs.terminate_project(pid)
check("terminate dropped the graph collection",
      vectorstore.count_collection(name) == 0)
check("terminate wiped the workspace graph rows",
      store.stats(pid)["entities_total"] == 0
      and store.current_revision_number(pid) == 0)

other = make_minimal_workspace(runtime, name="kg-search-del")
other_evidence = find_evidence(other, "REQ-NC-017")
knowledge.create_entity(
    other, entity_type="requirement",
    canonical_key="requirement:REQ-DEL", display_name="REQ-DEL",
    evidence=[{"evidence_id": other_evidence}], actor="t", note="d")
vector_projection.refresh_workspace(other)
other_name = vector_projection.collection_name(other)
check("second workspace projected before delete",
      vectorstore.count_collection(other_name) >= 1)
jobs.delete_project(other)
check("delete dropped the graph collection",
      vectorstore.count_collection(other_name) == 0)

# an orphaned graph collection IS recognized and swept
ghost = vectorstore.get_store("knowledge_p_ghost")
ghost.upsert(["g1"], [[0.0] * 8], ["ghost"], [{"workspace_id": "p_ghost"}])
dropped = vectorstore.drop_orphan_collections({pid})
check("startup orphan cleanup recognizes graph collections",
      "knowledge_p_ghost" in dropped)

finish()
