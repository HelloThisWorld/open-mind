"""Graph read surface: the node abstraction, bounded expansion, bounded
shortest-path discovery and bounded subgraph export.

Traversal here is deterministic BFS in plain Python over indexed SQL reads —
no recursive SQL, no unbounded loops, every walk capped by hard limits. The
source-plane node kinds (asset / revision / segment / evidence) are
PROJECTED into the read shape from their canonical Phase 2 rows; nothing is
copied into graph tables to render it.

Path discovery is generic graph reachability. It is deliberately NOT
labelled Requirement Traceability — the formal traceability engine is
Phase 6.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import db
from . import store
from .errors import GraphLimitExceeded, GraphNodeNotFound
from .vocabularies import (GraphLifecycleStatus, GraphNodeKind,
                           RelationState, RelationType, require)

#: Hard ceilings (spec §28). Defaults below are lower.
MAX_EXPANSION_DEPTH = 4
MAX_EXPANSION_NODES = 1000
MAX_EXPANSION_EDGES = 3000
DEFAULT_EXPANSION_DEPTH = 2
DEFAULT_EXPANSION_NODES = 200
DEFAULT_EXPANSION_EDGES = 600

MAX_PATH_DEPTH = 6
MAX_PATH_VISITED = 2000
MAX_EQUAL_PATHS = 5
MAX_EVIDENCE_SUMMARY_CHARS = 200

_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})


# ---------------------------------------------------------------------------
# Node read shapes
# ---------------------------------------------------------------------------
def entity_node(workspace_id: str, entity: Dict[str, Any],
                *, include_details: bool = True) -> Dict[str, Any]:
    """The stable camelCase read shape of one Entity node."""
    node = {
        "id": entity["id"],
        "nodeKind": GraphNodeKind.ENTITY,
        "entityType": entity["entity_type"],
        "canonicalKey": entity["canonical_key"],
        "displayName": entity["display_name"],
        "lifecycleStatus": entity["lifecycle_status"],
        "authorityStatus": entity["authority_status"],
        "origin": entity["origin"],
        "knowledgeRevision": store.current_revision_number(workspace_id),
    }
    if entity.get("merged_into_entity_id"):
        node["mergedIntoEntityId"] = entity["merged_into_entity_id"]
    if entity.get("superseded_by_entity_id"):
        node["supersededByEntityId"] = entity["superseded_by_entity_id"]
    if include_details:
        node["bindings"] = [
            {"id": b["id"], "refKind": b["ref_kind"], "refId": b["ref_id"],
             "refKey": b["ref_key"], "bindingRole": b["binding_role"],
             "status": b["status"]}
            for b in store.list_bindings(workspace_id, entity["id"])]
        node["aliases"] = [
            {"alias": a["alias"], "aliasType": a["alias_type"],
             "status": a["status"]}
            for a in store.list_aliases(workspace_id, entity["id"])]
        node["claimCount"] = store.count_claims(
            workspace_id, entity_id=entity["id"],
            lifecycle_status=GraphLifecycleStatus.ACTIVE)
        node["relationCount"] = len(store.list_relations(
            workspace_id, entity_id=entity["id"],
            lifecycle_status=GraphLifecycleStatus.ACTIVE, limit=10_000))
    return node


def claim_node(workspace_id: str, claim: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": claim["id"],
        "nodeKind": GraphNodeKind.CLAIM,
        "entityId": claim["entity_id"],
        "claimType": claim["claim_type"],
        "statement": claim["statement"],
        "lifecycleStatus": claim["lifecycle_status"],
        "authorityStatus": claim["authority_status"],
        "origin": claim["origin"],
        "evidence": claim.get("evidence") or
                    store.claim_evidence(claim["id"]),
        "knowledgeRevision": store.current_revision_number(workspace_id),
    }


def get_node(workspace_id: str, node_id: str) -> Dict[str, Any]:
    """Resolve any supported node id to its read shape. Source-plane ids are
    projected from their canonical rows, workspace-scoped exactly like the
    Phase 2 reads."""
    node_id = str(node_id or "").strip()
    entity = store.get_entity(workspace_id, node_id)
    if entity:
        return entity_node(workspace_id, entity)
    claim = store.get_claim(workspace_id, node_id)
    if claim:
        return claim_node(workspace_id, claim)
    asset = db.get_asset(workspace_id, node_id)
    if asset:
        return {
            "id": asset["id"], "nodeKind": GraphNodeKind.ASSET,
            "logicalKey": asset["logical_key"],
            "assetType": asset["asset_type"], "state": asset["state"],
            "currentRevisionId": asset["current_revision_id"],
            "boundEntities": [b["entity_id"] for b in
                              store.find_bindings_by_ref(workspace_id,
                                                         "asset",
                                                         asset["id"])],
            "knowledgeRevision":
                store.current_revision_number(workspace_id),
        }
    revision = db.get_revision(workspace_id, node_id)
    if revision:
        return {
            "id": revision["id"], "nodeKind": GraphNodeKind.REVISION,
            "assetId": revision["asset_id"],
            "sequence": revision["sequence"],
            "status": revision["status"],
            "contentHash": revision["content_hash"],
            "boundEntities": [b["entity_id"] for b in
                              store.find_bindings_by_ref(workspace_id,
                                                         "revision",
                                                         revision["id"])],
            "knowledgeRevision":
                store.current_revision_number(workspace_id),
        }
    segment = db.get_segment(workspace_id, node_id)
    if segment:
        return {
            "id": segment["id"], "nodeKind": GraphNodeKind.SEGMENT,
            "revisionId": segment["revision_id"],
            "segmentType": segment["segment_type"],
            "symbol": segment["symbol"],
            "segmentKey": segment["segment_key"],
            "boundEntities": [b["entity_id"] for b in
                              store.find_bindings_by_ref(workspace_id,
                                                         "segment",
                                                         segment["id"])],
            "knowledgeRevision":
                store.current_revision_number(workspace_id),
        }
    evidence = db.get_evidence(workspace_id, node_id)
    if evidence:
        return {
            "id": evidence["id"], "nodeKind": GraphNodeKind.EVIDENCE,
            "revisionId": evidence["revision_id"],
            "segmentId": evidence["segment_id"],
            "locator": evidence["locator"],
            "knowledgeRevision":
                store.current_revision_number(workspace_id),
        }
    raise GraphNodeNotFound(node_id, workspace_id=workspace_id)


def _edge_shape(rel: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rel["id"],
        "sourceEntityId": rel["source_entity_id"],
        "targetEntityId": rel["target_entity_id"],
        "relationType": rel["relation_type"],
        "relationState": rel["relation_state"],
        "confidence": rel["confidence"],
        "lifecycleStatus": rel["lifecycle_status"],
        "origin": rel["origin"],
    }


# ---------------------------------------------------------------------------
# Adjacency
# ---------------------------------------------------------------------------
def _neighbors(workspace_id: str, entity_id: str, *, direction: str,
               relation_types: Optional[Sequence[str]],
               include_stale: bool,
               lifecycle_cache: Optional[Dict[str, str]] = None
               ) -> List[Dict[str, Any]]:
    """This entity's relations in deterministic order. Rejected relations
    are always excluded (governance history, not graph edges); an active
    traversal additionally skips edges whose OTHER endpoint is no longer an
    active entity — a withdrawn node must not silently carry a path."""
    lifecycle = None if include_stale else GraphLifecycleStatus.ACTIVE
    out: List[Dict[str, Any]] = []
    if direction in ("outgoing", "both"):
        out.extend(store.list_relations(
            workspace_id, source_entity_id=entity_id,
            lifecycle_status=lifecycle, limit=MAX_EXPANSION_EDGES))
    if direction in ("incoming", "both"):
        out.extend(store.list_relations(
            workspace_id, target_entity_id=entity_id,
            lifecycle_status=lifecycle, limit=MAX_EXPANSION_EDGES))
    cache: Dict[str, str] = lifecycle_cache if lifecycle_cache is not None \
        else {}

    def entity_lifecycle(other_id: str) -> str:
        if other_id not in cache:
            other = store.get_entity(workspace_id, other_id)
            cache[other_id] = (other or {}).get("lifecycle_status", "")
        return cache[other_id]

    wanted = set(relation_types or [])
    result = []
    seen: Set[str] = set()
    for rel in out:
        if rel["id"] in seen:
            continue
        seen.add(rel["id"])
        if rel["relation_state"] == RelationState.REJECTED:
            continue
        if wanted and rel["relation_type"] not in wanted:
            continue
        if not include_stale:
            if rel["lifecycle_status"] != GraphLifecycleStatus.ACTIVE:
                continue
            other = (rel["target_entity_id"]
                     if rel["source_entity_id"] == entity_id
                     else rel["source_entity_id"])
            if entity_lifecycle(other) != GraphLifecycleStatus.ACTIVE:
                continue
        result.append(rel)
    result.sort(key=lambda r: (r["relation_type"], r["source_entity_id"],
                               r["target_entity_id"], r["id"]))
    return result


def _validate_relation_types(relation_types: Optional[Sequence[str]]
                             ) -> List[str]:
    clean: List[str] = []
    for value in relation_types or []:
        clean.append(require(RelationType, value, field="relation type"))
    return clean


def _require_entity_node(workspace_id: str, node_id: str) -> Dict[str, Any]:
    entity = store.get_entity(workspace_id, node_id)
    if not entity:
        raise GraphNodeNotFound(node_id, workspace_id=workspace_id)
    return entity


# ---------------------------------------------------------------------------
# Bounded expansion
# ---------------------------------------------------------------------------
def expand_node(workspace_id: str, node_id: str, *,
                direction: str = "both",
                relation_types: Optional[Sequence[str]] = None,
                depth: int = DEFAULT_EXPANSION_DEPTH,
                node_limit: int = DEFAULT_EXPANSION_NODES,
                edge_limit: int = DEFAULT_EXPANSION_EDGES,
                include_stale: bool = False) -> Dict[str, Any]:
    """Deterministic bounded BFS from one Entity node."""
    direction = str(direction or "both").strip().lower()
    if direction not in _DIRECTIONS:
        raise GraphLimitExceeded(
            f"unknown direction: {direction!r}",
            details={"allowed": sorted(_DIRECTIONS)})
    types = _validate_relation_types(relation_types)
    depth = max(1, min(int(depth), MAX_EXPANSION_DEPTH))
    node_limit = max(1, min(int(node_limit), MAX_EXPANSION_NODES))
    edge_limit = max(1, min(int(edge_limit), MAX_EXPANSION_EDGES))

    start = _require_entity_node(workspace_id, node_id)
    truncated = False
    visited: Dict[str, int] = {start["id"]: 0}
    edges: Dict[str, Dict[str, Any]] = {}
    frontier: List[str] = [start["id"]]

    for level in range(depth):
        next_frontier: List[str] = []
        for entity_id in frontier:
            for rel in _neighbors(workspace_id, entity_id,
                                  direction=direction,
                                  relation_types=types,
                                  include_stale=include_stale):
                if len(edges) >= edge_limit and rel["id"] not in edges:
                    truncated = True
                    continue
                edges[rel["id"]] = rel
                other = (rel["target_entity_id"]
                         if rel["source_entity_id"] == entity_id
                         else rel["source_entity_id"])
                if other in visited:
                    continue
                if len(visited) >= node_limit:
                    truncated = True
                    continue
                visited[other] = level + 1
                next_frontier.append(other)
        frontier = sorted(next_frontier)
        if not frontier:
            break

    nodes = []
    for entity_id in sorted(visited, key=lambda e: (visited[e], e)):
        entity = store.get_entity(workspace_id, entity_id)
        if entity:
            shape = entity_node(workspace_id, entity, include_details=False)
            shape["depth"] = visited[entity_id]
            nodes.append(shape)
    edge_list = [_edge_shape(edges[k]) for k in sorted(edges)]
    return {
        "workspace_id": workspace_id,
        "start": start["id"],
        "direction": direction,
        "depth": depth,
        "nodes": nodes,
        "edges": edge_list,
        "truncated": truncated,
        "limits": {"depth": depth, "nodes": node_limit, "edges": edge_limit,
                   "hard_depth": MAX_EXPANSION_DEPTH,
                   "hard_nodes": MAX_EXPANSION_NODES,
                   "hard_edges": MAX_EXPANSION_EDGES},
        "knowledge_revision": store.current_revision_number(workspace_id),
    }


# ---------------------------------------------------------------------------
# Bounded shortest-path discovery
# ---------------------------------------------------------------------------
def _evidence_summary(relation_id: str) -> List[Dict[str, Any]]:
    return [{"evidence_id": ev["evidence_id"],
             "quote": (ev.get("quote") or "")[:MAX_EVIDENCE_SUMMARY_CHARS]}
            for ev in store.relation_evidence(relation_id)[:5]]


def find_path(workspace_id: str, source_id: str, target_id: str, *,
              relation_types: Optional[Sequence[str]] = None,
              direction: str = "both",
              max_depth: int = MAX_PATH_DEPTH,
              include_stale: bool = False) -> Dict[str, Any]:
    """Deterministic BFS shortest paths (unweighted). Honest outcomes:
    ``found`` / ``no-path`` / ``truncated`` — a missing edge is never
    invented."""
    direction = str(direction or "both").strip().lower()
    if direction not in _DIRECTIONS:
        raise GraphLimitExceeded(
            f"unknown direction: {direction!r}",
            details={"allowed": sorted(_DIRECTIONS)})
    types = _validate_relation_types(relation_types)
    max_depth = max(1, min(int(max_depth), MAX_PATH_DEPTH))
    source = _require_entity_node(workspace_id, source_id)
    target = _require_entity_node(workspace_id, target_id)

    base = {
        "workspace_id": workspace_id,
        "source": source["id"], "target": target["id"],
        "direction": direction, "max_depth": max_depth,
        "knowledge_revision": store.current_revision_number(workspace_id),
    }
    if source["id"] == target["id"]:
        return {**base, "outcome": "found", "paths": [
            {"length": 0, "entities": [source["id"]], "edges": []}],
            "truncated": False}

    #: parents[node] = list of (prev_node, relation) on ANY shortest path.
    parents: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    depth_of: Dict[str, int] = {source["id"]: 0}
    frontier = [source["id"]]
    truncated = False
    found_depth: Optional[int] = None

    for level in range(max_depth):
        if found_depth is not None:
            break
        next_frontier: List[str] = []
        for entity_id in sorted(frontier):
            if len(depth_of) >= MAX_PATH_VISITED:
                truncated = True
                break
            for rel in _neighbors(workspace_id, entity_id,
                                  direction=direction,
                                  relation_types=types,
                                  include_stale=include_stale):
                other = (rel["target_entity_id"]
                         if rel["source_entity_id"] == entity_id
                         else rel["source_entity_id"])
                if other == entity_id:
                    continue
                known = depth_of.get(other)
                if known is None:
                    if len(depth_of) >= MAX_PATH_VISITED:
                        truncated = True
                        continue
                    depth_of[other] = level + 1
                    parents[other] = [(entity_id, rel)]
                    next_frontier.append(other)
                elif known == level + 1:
                    parents[other].append((entity_id, rel))
                if other == target["id"]:
                    found_depth = level + 1
        frontier = next_frontier
        if not frontier:
            break

    if found_depth is None:
        return {**base, "outcome": "truncated" if truncated else "no-path",
                "paths": [], "truncated": truncated}

    # Reconstruct up to MAX_EQUAL_PATHS shortest paths, deterministically.
    paths: List[Dict[str, Any]] = []

    def walk(node: str, entities: List[str],
             edges: List[Dict[str, Any]]) -> None:
        """Backtrack from *node* to the source; *entities* collects the
        nodes strictly after the source, target-first."""
        if len(paths) >= MAX_EQUAL_PATHS:
            return
        if node == source["id"]:
            path_entities = [source["id"]] + list(reversed(entities))
            path_edges = list(reversed(edges))
            paths.append({
                "length": len(path_edges),
                "entities": path_entities,
                "edges": [{**_edge_shape(r),
                           "evidence": _evidence_summary(r["id"])}
                          for r in path_edges],
            })
            return
        for prev, rel in sorted(parents.get(node, ()),
                                key=lambda pr: (pr[0], pr[1]["id"])):
            walk(prev, entities + [node], edges + [rel])

    walk(target["id"], [], [])
    # Present entity display data for the nodes on the returned paths.
    node_ids = sorted({e for p in paths for e in p["entities"]})
    nodes = []
    for entity_id in node_ids:
        entity = store.get_entity(workspace_id, entity_id)
        if entity:
            nodes.append(entity_node(workspace_id, entity,
                                     include_details=False))
    return {**base, "outcome": "found", "paths": paths, "nodes": nodes,
            "truncated": truncated}


# ---------------------------------------------------------------------------
# Bounded subgraph export
# ---------------------------------------------------------------------------
def get_subgraph(workspace_id: str, node_ids: Sequence[str], *,
                 depth: int = 1,
                 node_limit: int = DEFAULT_EXPANSION_NODES,
                 edge_limit: int = DEFAULT_EXPANSION_EDGES,
                 include_stale: bool = False) -> Dict[str, Any]:
    """The bounded union of expansions around several seed nodes, plus every
    edge BETWEEN collected nodes."""
    seeds = [str(n).strip() for n in node_ids if str(n or "").strip()]
    if not seeds:
        raise GraphLimitExceeded("at least one seed node id is required")
    if len(seeds) > 20:
        raise GraphLimitExceeded("at most 20 seed nodes",
                                 details={"got": len(seeds)})
    collected: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    truncated = False
    for seed in sorted(seeds):
        expansion = expand_node(workspace_id, seed, depth=depth,
                                node_limit=node_limit,
                                edge_limit=edge_limit,
                                include_stale=include_stale)
        truncated = truncated or expansion["truncated"]
        for node in expansion["nodes"]:
            if len(collected) >= node_limit and node["id"] not in collected:
                truncated = True
                continue
            collected.setdefault(node["id"], node)
        for edge in expansion["edges"]:
            if len(edges) >= edge_limit and edge["id"] not in edges:
                truncated = True
                continue
            edges.setdefault(edge["id"], edge)
    # Keep only edges whose BOTH endpoints made it into the node set.
    kept = [e for _, e in sorted(edges.items())
            if e["sourceEntityId"] in collected
            and e["targetEntityId"] in collected]
    return {
        "workspace_id": workspace_id,
        "seeds": sorted(seeds),
        "nodes": [collected[k] for k in sorted(collected)],
        "edges": kept,
        "truncated": truncated,
        "knowledge_revision": store.current_revision_number(workspace_id),
    }


__all__ = [
    "get_node", "entity_node", "claim_node", "expand_node", "find_path",
    "get_subgraph",
    "MAX_EXPANSION_DEPTH", "MAX_EXPANSION_NODES", "MAX_EXPANSION_EDGES",
    "MAX_PATH_DEPTH",
]
