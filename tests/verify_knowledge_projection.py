"""Deterministic graph projection: asset/segment/containment/call/facet
rules, zero provider calls, incremental sync (unchanged = no-op; one changed
segment touches only affected records)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (JAVA_SERVICE, check,  # noqa: E402
                                entities_of_type, finish,
                                make_source_workspace)

from openmind import db, mapio  # noqa: E402
from openmind.knowledge import store  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid, src = make_source_workspace(runtime)

plan = knowledge.plan_seed(pid)
check("seed plan reports desired objects without writing",
      plan["desired_entities"] > 0
      and store.current_revision_number(pid) == 0)

result = knowledge.seed(pid, actor="projector-test")
check("seed wrote the graph and minted revision 1",
      result["action"] == "seeded" and result["knowledge_revision"] == 1)

# -- asset projection --------------------------------------------------------
components = entities_of_type(pid, "code-component")
component_names = {e["display_name"] for e in components}
check("source assets became code-components (service + test sources)",
      any("NameCheckService.java" in n for n in component_names)
      and any("NameCheckServiceTest.java" in n for n in component_names))
configurations = entities_of_type(pid, "configuration")
check("configuration asset became a configuration entity",
      any("namecheck.properties" in e["display_name"]
          for e in configurations))
documents = entities_of_type(pid, "document")
check("parsed documents became document entities",
      any("requirements" in e["display_name"].lower() for e in documents))
check("a generic document did NOT become a requirement or design entity",
      entities_of_type(pid, "requirement") == []
      and entities_of_type(pid, "design") == [])

# -- segment projection ------------------------------------------------------
symbols = entities_of_type(pid, "code-symbol")
symbol_names = {e["display_name"] for e in symbols}
check("Java type became a code-symbol",
      any(n.endswith("NameCheckService") for n in symbol_names))
check("Java method became a code-symbol",
      any("execute" in n for n in symbol_names))
check("test-source method did NOT become a test-case entity",
      entities_of_type(pid, "test-case") == []
      and any("shouldNormalize" in n for n in symbol_names))
interfaces = entities_of_type(pid, "interface")
check("OpenAPI operation became an interface entity",
      any(e["canonical_key"].startswith("interface:POST:")
          for e in interfaces))
database_objects = entities_of_type(pid, "database-object")
db_names = {e["display_name"] for e in database_objects}
check("SQL tables became database-object entities",
      "PASSENGER" in db_names and "CHECK_AUDIT" in db_names)

# -- containment -------------------------------------------------------------
by_id = {e["id"]: e for e in store.list_entities(pid, lifecycle_status=None,
                                                 limit=10_000)}
contains = store.list_relations(pid, relation_type="contains", limit=10_000)
check("containment relations are explicit",
      contains and all(r["relation_state"] == "explicit"
                       for r in contains))


def has_containment(source_type: str, target_type: str) -> bool:
    return any(by_id[r["source_entity_id"]]["entity_type"] == source_type
               and by_id[r["target_entity_id"]]["entity_type"] == target_type
               for r in contains)


check("code-component contains code-symbol", has_containment(
    "code-component", "code-symbol"))
check("document contains interface (OpenAPI)", has_containment(
    "document", "interface"))
check("document contains database-object (SQL)", has_containment(
    "document", "database-object"))

# -- call relations -----------------------------------------------------------
calls = store.list_relations(pid, relation_type="calls", limit=10_000)
check("deterministic call edges are inferred with origin deterministic",
      calls and all(r["relation_state"] == "inferred"
                    and r["origin"] == "deterministic" for r in calls))
ambiguous_calls = [r for r in calls
                   if (r.get("metadata") or {}).get("ambiguous")]
check("name-based ambiguity is preserved (ambiguous edge stays low "
      "confidence)", ambiguous_calls
      and all(r["confidence"] == "low" for r in ambiguous_calls))
check("call relations carry source evidence",
      all(store.relation_evidence(r["id"]) for r in calls))

# -- zero provider calls ------------------------------------------------------
conn, lock = db.shared_connection()
with lock:
    usage_rows = conn.execute("SELECT COUNT(*) FROM semantic_usage"
                              ).fetchone()[0]
check("projection performed zero provider calls", usage_rows == 0)

# -- unchanged sync is a no-op ------------------------------------------------
sync = knowledge.sync(pid)
check("unchanged sync writes nothing and mints no revision",
      sync["action"] == "noop" and sync["knowledge_revision"] == 1)

# -- one changed segment updates only affected records ------------------------
# NOTE the ingest-completion hook runs the graph staleness reconciliation
# (spec §22), so re-ingesting mints its own reconcile revision BEFORE the
# explicit sync; the assertions below measure the sync increment itself.
symbols_before = {e["canonical_key"]: e["id"] for e in symbols}
changed = JAVA_SERVICE.replace(
    "return raw == null ? \"\" : raw.trim().toUpperCase();",
    "return raw == null ? \"?\" : raw.trim().toUpperCase();")
(src / "src" / "NameCheckService.java").write_text(changed,
                                                   encoding="utf-8")
runtime.ingest.start(pid, wait=True, timeout=300)
before_sync = store.current_revision_number(pid)
sync = knowledge.sync(pid, actor="projector-test")
check("changed source produced exactly one incremental sync revision",
      sync["action"] == "synced"
      and sync["knowledge_revision"] == before_sync + 1)
check("incremental sync created no new entities for an in-place edit",
      sync["entities_created"] == 0)
check("incremental sync repointed only the changed asset's bindings",
      0 < sync["bindings_added"] <= 14)
symbols_after = {e["canonical_key"]: e["id"] for e in
                 entities_of_type(pid, "code-symbol")}
check("unchanged code-symbol entities kept their identity",
      symbols_before == symbols_after)
check("after sync the changed asset's symbols are active again",
      all(e["lifecycle_status"] == "active"
          for e in entities_of_type(pid, "code-symbol")))

# a removed method stales exactly its symbol (the reconcile hook or the
# sync may perform the staling; the OUTCOME is what the contract fixes)
without_method = changed.replace(
    """    private String normalize(String raw) {
        return raw == null ? \"?\" : raw.trim().toUpperCase();
    }
""", "")
(src / "src" / "NameCheckService.java").write_text(without_method,
                                                   encoding="utf-8")
runtime.ingest.start(pid, wait=True, timeout=300)
sync = knowledge.sync(pid, actor="projector-test")
stale_symbols = [e for e in entities_of_type(pid, "code-symbol")
                 if e["lifecycle_status"] == "stale"]
check("a removed method stales exactly its code-symbol entity",
      len(stale_symbols) == 1
      and "normalize" in stale_symbols[0]["display_name"])
check("all other code-symbols stayed (or returned to) active",
      len([e for e in entities_of_type(pid, "code-symbol")
           if e["lifecycle_status"] == "active"]) ==
      len(symbols_after) - 1)
check("stale entity remains queryable with authority preserved",
      stale_symbols[0]["authority_status"] == "unknown")

# -- facet topics -------------------------------------------------------------
facts = mapio.load_facts(pid) or {}
facts.setdefault("facts", []).append(
    {"facet": "kafka_topic", "values": {"topic": "name-check-events"},
     "file": "src/NameCheckService.java", "line": 10,
     "snippet": "kafkaTemplate.send(\"name-check-events\", request)"})
mapio.save_facts(pid, facts)
sync = knowledge.sync(pid, actor="projector-test")
topics = entities_of_type(pid, "message-topic")
check("an exact topic facet capture seeded a message-topic entity",
      any(e["display_name"] == "name-check-events" for e in topics))
check("no publishes/consumes relation was invented from the facet "
      "(direction is not captured)",
      store.list_relations(pid, relation_type="publishes",
                           limit=100) == []
      and store.list_relations(pid, relation_type="consumes",
                               limit=100) == [])

finish()
