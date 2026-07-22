"""Knowledge Revision ledger: monotonic per-workspace numbering, one
transaction = one revision, failed transaction = no revision, concurrency
uniqueness, accurate change counts, current revision on reads."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import threading  # noqa: E402

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind.knowledge import store  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

check("fresh workspace is at knowledge revision 0",
      store.current_revision_number(pid) == 0)

first = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-017",
    display_name="REQ-NC-017", evidence=[{"evidence_id": evidence_id}],
    actor="tester", note="first write")
check("first graph write creates revision 1",
      first["knowledge_revision"] == 1)

second = knowledge.create_entity(
    pid, entity_type="requirement", canonical_key="requirement:REQ-NC-018",
    display_name="REQ-NC-018", evidence=[{"evidence_id": evidence_id}],
    actor="tester", note="second write")
check("next write creates revision 2", second["knowledge_revision"] == 2)

ledger = knowledge.list_knowledge_revisions(pid)["revisions"]
check("ledger lists both revisions newest-first",
      [r["revision_number"] for r in ledger] == [2, 1])
check("revision records its action and actor",
      ledger[0]["action"] == "manual-entity-create"
      and ledger[0]["actor"] == "tester")
rev1 = knowledge.get_knowledge_revision(pid, 1)
check("revision 1 counts one entity, one binding, one decision",
      rev1["object_counts"].get("entities") == 1
      and rev1["object_counts"].get("bindings") == 1
      and rev1["object_counts"].get("decisions") == 1)
check("revision 2 has parent revision 1",
      knowledge.get_knowledge_revision(pid, 2)["parent_revision_number"]
      == 1)

# -- failed transaction creates no revision and no partial rows -------------
before = store.current_revision_number(pid)
try:
    with store.graph_transaction(pid, action="manual-entity-create",
                                 actor="tester") as tx:
        tx.insert_entity(entity_type="constraint",
                         canonical_key="constraint:HALF",
                         display_name="half", origin="manual")
        raise RuntimeError("simulated mid-transaction failure")
except RuntimeError:
    pass
check("failed transaction creates no knowledge revision",
      store.current_revision_number(pid) == before)
check("failed transaction leaves no partial entity",
      store.find_entity_by_key(pid, "constraint", "constraint:HALF") is None)

# -- an empty transaction body writes nothing -------------------------------
with store.graph_transaction(pid, action="graph-sync") as tx:
    pass
check("a change-free transaction mints no revision",
      store.current_revision_number(pid) == before)

# -- concurrency: unique, monotonic, gap-free numbers -----------------------
results = []
errors = []


def writer(index: int) -> None:
    try:
        result = knowledge.create_entity(
            pid, entity_type="workflow",
            canonical_key=f"workflow:derived:concurrent-{index}",
            display_name=f"concurrent-{index}",
            evidence=[{"evidence_id": evidence_id}],
            actor=f"thread-{index}", note="concurrent write")
        results.append(result["knowledge_revision"])
    except Exception as exc:   # pragma: no cover - failure output
        errors.append(repr(exc))


threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent writers all succeeded", not errors)
check("concurrent revision numbers are unique",
      len(set(results)) == len(results) == 8)
check("concurrent revision numbers are gap-free and monotonic",
      sorted(results) == list(range(before + 1, before + 9)))

# -- reads carry the current revision ---------------------------------------
current = store.current_revision_number(pid)
check("stats reports the current knowledge revision",
      knowledge.get_stats(pid)["knowledge_revision"] == current)
check("entity read reports the current knowledge revision",
      knowledge.get_entity(pid, first["entity"]["id"])
      ["knowledge_revision"] == current)
check("search reports the current knowledge revision",
      knowledge.search_entities(pid, "REQ-NC-017")["knowledge_revision"]
      == current)

# -- another workspace numbers independently --------------------------------
other = make_minimal_workspace(runtime, name="kg-rev-other")
other_evidence = find_evidence(other, "REQ-NC-017")
other_first = knowledge.create_entity(
    other, entity_type="requirement",
    canonical_key="requirement:REQ-NC-017", display_name="REQ-NC-017",
    evidence=[{"evidence_id": other_evidence}], actor="tester", note="w")
check("revision numbering is per-workspace",
      other_first["knowledge_revision"] == 1)

finish()
