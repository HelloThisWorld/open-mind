"""Graph queries: node lookup for every kind, bounded deterministic
expansion, shortest paths (found / no-path / truncated), evidence summaries,
subgraph export."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_minimal_workspace)

from openmind import db  # noqa: E402
from openmind.knowledge import graph, store  # noqa: E402
from openmind.knowledge.errors import (GraphLimitExceeded,  # noqa: E402
                                       GraphNodeNotFound)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")


def entity(key):
    return knowledge.create_entity(
        pid, entity_type="requirement", canonical_key=f"requirement:{key}",
        display_name=key, evidence=[{"evidence_id": evidence_id}],
        actor="t", note="fixture")["entity"]


def relate(source, target, relation_type="refines"):
    return knowledge.create_relation(
        pid, source_entity_id=source["id"], target_entity_id=target["id"],
        relation_type=relation_type, relation_state="confirmed",
        evidence=[{"evidence_id": evidence_id,
                   "quote": "shall answer within 2 seconds"}],
        actor="t", note="fixture")["relation"]


# a small chain plus a fork:  A -> B -> C -> D,  A -> E,  F isolated
a, b, c, d, e, f = (entity(k) for k in ("A", "B", "C", "D", "E", "F"))
relate(a, b)
relate(b, c)
relate(c, d)
relate(a, e, relation_type="affected-by")
claim = knowledge.create_claim(
    pid, entity_id=a["id"], claim_type="normative-statement",
    statement="A drives everything.",
    evidence=[{"evidence_id": evidence_id}], actor="t", note="c")["claim"]

# -- node lookup for every kind ----------------------------------------------
node = knowledge.get_node(pid, a["id"])
check("entity node has the stable camelCase shape",
      node["nodeKind"] == "entity"
      and node["canonicalKey"] == "requirement:A"
      and "lifecycleStatus" in node and "authorityStatus" in node
      and node["claimCount"] == 1 and node["relationCount"] == 2
      and isinstance(node["bindings"], list))
check("claim node resolves",
      knowledge.get_node(pid, claim["id"])["nodeKind"] == "claim")
asset = db.list_assets(pid, state="active", limit=5)[0]
check("asset node projected from the canonical row",
      knowledge.get_node(pid, asset["id"])["nodeKind"] == "asset")
revision_node = knowledge.get_node(pid, asset["current_revision_id"])
check("revision node projected", revision_node["nodeKind"] == "revision")
segment = db.list_segments(pid, asset["current_revision_id"], limit=5)[0]
check("segment node projected",
      knowledge.get_node(pid, segment["id"])["nodeKind"] == "segment")
check("evidence node projected",
      knowledge.get_node(pid, evidence_id)["nodeKind"] == "evidence")
try:
    knowledge.get_node(pid, "ent_nope")
    missing = False
except GraphNodeNotFound:
    missing = True
check("unknown node id is a typed not-found", missing)

# -- bounded expansion --------------------------------------------------------
expansion = knowledge.expand_node(pid, a["id"], depth=1)
ids_at_depth_one = {n["id"] for n in expansion["nodes"]}
check("depth-1 expansion reaches only direct neighbours",
      ids_at_depth_one == {a["id"], b["id"], e["id"]})
expansion = knowledge.expand_node(pid, a["id"], depth=3)
check("depth-3 expansion reaches the chain but not the isolate",
      {n["id"] for n in expansion["nodes"]}
      == {a["id"], b["id"], c["id"], d["id"], e["id"]})
check("expansion reports limits and revision",
      expansion["limits"]["hard_depth"] == 4
      and expansion["knowledge_revision"]
      == store.current_revision_number(pid))
second = knowledge.expand_node(pid, a["id"], depth=3)
check("expansion is deterministic (identical repeat)",
      expansion["nodes"] == second["nodes"]
      and expansion["edges"] == second["edges"])
check("depth is clamped to the hard cap",
      knowledge.expand_node(pid, a["id"], depth=99)["limits"]["depth"] == 4)
truncated = knowledge.expand_node(pid, a["id"], depth=3, node_limit=2)
check("node limit truncates and says so",
      truncated["truncated"] and len(truncated["nodes"]) <= 2)
filtered = knowledge.expand_node(pid, a["id"], depth=1,
                                 relation_types=["refines"])
check("relation-type filter narrows the expansion",
      {n["id"] for n in filtered["nodes"]} == {a["id"], b["id"]})
try:
    knowledge.expand_node(pid, a["id"], relation_types=["depends-on"])
    bad_type = False
except Exception:
    bad_type = True
check("unknown relation type in expansion is rejected", bad_type)

# -- path discovery -----------------------------------------------------------
path = knowledge.find_path(pid, a["id"], d["id"])
check("shortest path found with honest outcome",
      path["outcome"] == "found" and path["paths"][0]["length"] == 3
      and path["paths"][0]["entities"]
      == [a["id"], b["id"], c["id"], d["id"]])
check("path edges carry evidence summaries",
      all(edge["evidence"] for edge in path["paths"][0]["edges"]))
no_path = knowledge.find_path(pid, a["id"], f["id"])
check("unreachable target is an honest no-path (never invented)",
      no_path["outcome"] == "no-path" and no_path["paths"] == [])
short = knowledge.find_path(pid, a["id"], d["id"], max_depth=2)
check("a too-short depth bound reports no-path/truncated, not a fake path",
      short["outcome"] in ("no-path", "truncated") and short["paths"] == [])
directed = knowledge.find_path(pid, d["id"], a["id"],
                               direction="outgoing")
check("direction policy is honoured (no reverse path outgoing-only)",
      directed["outcome"] in ("no-path", "truncated"))
both = knowledge.find_path(pid, d["id"], a["id"], direction="both")
check("undirected policy finds the reverse traversal",
      both["outcome"] == "found")

# truncation disclosure with a tiny visited budget
original_cap = graph.MAX_PATH_VISITED
graph.MAX_PATH_VISITED = 2
try:
    tiny = knowledge.find_path(pid, a["id"], d["id"])
finally:
    graph.MAX_PATH_VISITED = original_cap
check("visited-node truncation is disclosed",
      tiny["outcome"] == "truncated" and tiny["truncated"])

# stale exclusion in traversal
knowledge.withdraw_object(pid, kind="entity", object_id=c["id"],
                          actor="t", note="w")
# withdrawing C stales the relations touching it at next reconcile; the
# traversal itself must already exclude non-active endpoints' edges
blocked = knowledge.find_path(pid, a["id"], d["id"])
check("a withdrawn intermediate breaks the active path",
      blocked["outcome"] in ("no-path", "truncated"))
with_stale = knowledge.find_path(pid, a["id"], d["id"], include_stale=True)
check("include_stale traverses the historical edge again",
      with_stale["outcome"] == "found")

# -- subgraph -----------------------------------------------------------------
sub = knowledge.get_subgraph(pid, [a["id"], d["id"]], depth=1)
check("subgraph unions the seed neighbourhoods",
      {a["id"], b["id"], e["id"], d["id"]}
      <= {n["id"] for n in sub["nodes"]})
check("subgraph keeps only edges with both endpoints present",
      all(any(n["id"] == edge["sourceEntityId"] for n in sub["nodes"])
          and any(n["id"] == edge["targetEntityId"] for n in sub["nodes"])
          for edge in sub["edges"]))
try:
    knowledge.get_subgraph(pid, [])
    empty_ok = False
except GraphLimitExceeded:
    empty_ok = True
check("subgraph requires at least one seed", empty_ok)

finish()
