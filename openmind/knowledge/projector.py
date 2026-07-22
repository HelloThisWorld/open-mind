"""Deterministic graph projection: model-free seeding and incremental sync.

Everything here derives from recorded deterministic facts — Asset types,
Segment structure, the structure map's name-based call edges and template
facet captures. No semantic provider is imported, no model is called, no
network is touched, and nothing this module writes is a semantic Claim.

PROJECTION RULES (each tested in verify_knowledge_projection)
-------------------------------------------------------------
Assets (active, per :class:`~openmind.domain.types.AssetType`):
    source-code / test-source  -> code-component
    configuration              -> configuration
    database-schema            -> data-model (+ database-object per SQL object)
    build-definition           -> build-definition
    parsed document            -> document       (a parse record is the fact)
Segments (of each active asset's CURRENT revision):
    Java type/method/constructor -> code-symbol
    OpenAPI api-operation        -> interface
    OpenAPI schema-definition    -> data-model
    SQL sql-object               -> database-object
    message-topic facet capture  -> message-topic
Containment (structurally explicit -> relation_state=explicit):
    code-component -> code-symbol
    document       -> interface / data-model      (OpenAPI blocks)
    data-model     -> database-object             (SQL objects in a schema)
Calls (structure map, name-based -> relation_state=inferred):
    code-component -> code-component, confidence medium (unambiguous) or
    low (ambiguous). Ambiguity is PRESERVED — never upgraded.

Deliberate NON-projections: a generic document never becomes a Requirement
or Design; a test-source method never becomes a Test Case; a facet fact
without an exact ``topic`` capture never becomes a message-topic; no
publishes/consumes relation is created from facet captures (the capture
does not record direction deterministically).

INCREMENTAL SYNC
----------------
The desired deterministic state is computed in full (a read-only pass), then
DIFFED against the stored deterministic graph rows; only the difference is
written, inside one graph transaction. An unchanged source hash writes
nothing and mints no Knowledge Revision.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from .. import db, mapio
from . import GRAPH_PROJECTOR_VERSION, identity, store
from .vocabularies import (AliasStatus, AliasType, BindingRefKind,
                           BindingRole, BindingStatus, EntityType,
                           GraphLifecycleStatus, GraphOrigin, RelationState,
                           RelationType, RevisionAction)

#: Bounds — a projection pass must stay predictable on large workspaces.
MAX_ASSETS = 50_000
MAX_SEGMENTS_PER_REVISION = 2_000
MAX_CALL_EDGES = 20_000
MAX_TOPIC_FACTS = 2_000

_ASSET_ENTITY_TYPES = {
    "source-code": EntityType.CODE_COMPONENT,
    "test-source": EntityType.CODE_COMPONENT,
    "configuration": EntityType.CONFIGURATION,
    "database-schema": EntityType.DATA_MODEL,
    "build-definition": EntityType.BUILD_DEFINITION,
}

_CODE_SEGMENT_TYPES = frozenset({"type", "method", "constructor"})


def _desired_entity(entity_type: str, canonical_key: str, display_name: str,
                    description: str = "") -> Dict[str, Any]:
    return {"entity_type": entity_type, "canonical_key": canonical_key,
            "display_name": display_name[:200], "description":
            description[:500], "bindings": [], "aliases": []}


def compute_desired_state(workspace_id: str) -> Dict[str, Any]:
    """The full deterministic target state, as pure data.

    Returns ``{entities: {key: {...}}, relations: {(src_key, tgt_key, type):
    {...}}, truncated: {...}}`` where keys are canonical keys.
    """
    entities: Dict[str, Dict[str, Any]] = {}
    relations: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    truncated: Dict[str, bool] = {}

    assets = db.list_assets(workspace_id, state="active", limit=MAX_ASSETS)
    truncated["assets"] = len(assets) >= MAX_ASSETS
    key_to_asset: Dict[str, Dict[str, Any]] = {}
    #: asset_id -> the file-segment evidence id of its current revision
    #: (used as deterministic call-edge provenance).
    file_evidence: Dict[str, str] = {}

    for asset in assets:
        asset_id = asset["id"]
        logical_key = asset["logical_key"]
        key_to_asset[logical_key] = asset
        revision_id = asset.get("current_revision_id") or ""
        is_document = db.is_document_asset(workspace_id, asset_id)

        # A parse record is the recorded fact that makes an Asset a document;
        # an unparsed text file projects nothing (never a guessed Requirement
        # or Design). Everything else maps by deterministic Asset type.
        entity_type = (EntityType.DOCUMENT if is_document
                       else _ASSET_ENTITY_TYPES.get(asset["asset_type"]))
        if not entity_type:
            continue

        akey = identity.asset_entity_key(entity_type, asset_id)
        record = _desired_entity(entity_type, akey,
                                 asset.get("title") or logical_key)
        record["bindings"].append(
            {"ref_kind": BindingRefKind.ASSET, "ref_id": asset_id,
             "ref_key": logical_key,
             "binding_role": BindingRole.PRIMARY_SOURCE})
        if revision_id:
            record["bindings"].append(
                {"ref_kind": BindingRefKind.REVISION, "ref_id": revision_id,
                 "ref_key": logical_key,
                 "binding_role": BindingRole.PRIMARY_SOURCE})
        record["aliases"].append({"alias": logical_key,
                                  "alias_type": AliasType.PATH})
        entities[akey] = record
        asset["_entity_key"] = akey

        if not revision_id:
            continue
        segments = db.list_segments(workspace_id, revision_id,
                                    limit=MAX_SEGMENTS_PER_REVISION)
        if len(segments) >= MAX_SEGMENTS_PER_REVISION:
            truncated["segments"] = True
        evidence_by_segment = db.evidence_ids_for_revision(workspace_id,
                                                           revision_id)
        for segment in segments:
            stype = segment["segment_type"]
            symbol = segment.get("symbol") or ""
            seg_id = segment["id"]
            evidence_id = evidence_by_segment.get(seg_id, "")
            # The asset's call-edge evidence anchor: the file segment's
            # evidence where one exists (generic sources), else the first
            # segment evidence (Java revisions carry typed segments only).
            if evidence_id and (stype == "file"
                                or not file_evidence.get(asset_id)):
                file_evidence[asset_id] = evidence_id

            skey: Optional[str] = None
            if stype in _CODE_SEGMENT_TYPES and symbol:
                skey = identity.symbol_entity_key(asset_id, symbol)
                child = _desired_entity(EntityType.CODE_SYMBOL, skey, symbol)
                child["aliases"].append({"alias": symbol,
                                         "alias_type": AliasType.SYMBOL})
            elif stype == "api-operation" and symbol:
                # The parser records the exact method + path in the block
                # metadata; the segment symbol is only a slug. Fall back to
                # the slug when a (future) parser omits them.
                meta = segment.get("metadata") or {}
                method = str(meta.get("method") or "").strip()
                path = str(meta.get("path") or "").strip()
                if method and path:
                    skey = identity.interface_entity_key(method, path)
                    display = f"{method.upper()} {path}"
                else:
                    skey = identity.entity_key(EntityType.INTERFACE,
                                               "asset", asset_id, symbol)
                    display = symbol
                child = _desired_entity(EntityType.INTERFACE, skey, display)
                child["aliases"].append({"alias": display,
                                         "alias_type": AliasType.IDENTIFIER})
            elif stype == "schema-definition" and symbol:
                skey = identity.entity_key(EntityType.DATA_MODEL, "asset",
                                           asset_id, symbol)
                child = _desired_entity(EntityType.DATA_MODEL, skey, symbol)
                child["aliases"].append({"alias": symbol,
                                         "alias_type": AliasType.NAME})
            elif stype == "sql-object" and symbol:
                skey = f"database-object:asset:{asset_id}:{symbol}"
                child = _desired_entity(EntityType.DATABASE_OBJECT, skey,
                                        symbol)
                child["aliases"].append({"alias": symbol,
                                         "alias_type": AliasType.IDENTIFIER})
            else:
                continue
            child["bindings"].append(
                {"ref_kind": BindingRefKind.SEGMENT, "ref_id": seg_id,
                 "ref_key": segment.get("segment_key", ""),
                 "binding_role": BindingRole.DEFINITION_SOURCE,
                 "evidence_id": evidence_id})
            child["bindings"].append(
                {"ref_kind": BindingRefKind.REVISION, "ref_id": revision_id,
                 "ref_key": logical_key,
                 "binding_role": BindingRole.PRIMARY_SOURCE})
            entities[skey] = child
            relations[(akey, skey, RelationType.CONTAINS)] = {
                "relation_state": RelationState.EXPLICIT,
                "confidence": "high",
                "evidence_id": evidence_id,
                "metadata": {"containment": stype},
            }

    # -- call edges (name-based, ambiguity preserved) ------------------------
    structure = mapio.load_structure(workspace_id)
    edges = ((structure.get("call_graph") or {}).get("edges")
             or [])[:MAX_CALL_EDGES]
    truncated["call_edges"] = len(
        (structure.get("call_graph") or {}).get("edges") or []) > MAX_CALL_EDGES
    for edge in edges:
        src = key_to_asset.get(str(edge.get("from") or ""))
        dst = key_to_asset.get(str(edge.get("to") or ""))
        if not src or not dst:
            continue            # an endpoint is not an active asset: no edge
        src_key = src.get("_entity_key")
        dst_key = dst.get("_entity_key")
        if not src_key or not dst_key or src_key == dst_key:
            continue
        if entities.get(src_key, {}).get("entity_type") != \
                EntityType.CODE_COMPONENT or \
                entities.get(dst_key, {}).get("entity_type") != \
                EntityType.CODE_COMPONENT:
            continue
        evidence_id = file_evidence.get(src["id"], "")
        if not evidence_id:
            continue            # no recoverable source evidence: no edge
        ambiguous = bool(edge.get("ambiguous"))
        rkey = (src_key, dst_key, RelationType.CALLS)
        existing = relations.get(rkey)
        if existing:
            # Several symbols may link the same file pair; the edge stays
            # ambiguous unless EVERY contributing symbol is unambiguous.
            symbols = existing["metadata"].setdefault("symbols", [])
            if edge.get("symbol") and edge["symbol"] not in symbols:
                symbols.append(edge["symbol"])
            if ambiguous:
                existing["metadata"]["ambiguous"] = True
                existing["confidence"] = "low"
            continue
        relations[rkey] = {
            "relation_state": RelationState.INFERRED,
            "confidence": "low" if ambiguous else "medium",
            "evidence_id": evidence_id,
            "metadata": {"symbols": [edge.get("symbol") or ""],
                         "ambiguous": ambiguous, "analyzer": "structure"},
        }

    # -- message-topic facet captures ---------------------------------------
    facts = (mapio.load_facts(workspace_id) or {}).get("facts") or []
    topic_facts = [f for f in facts
                   if str((f.get("values") or {}).get("topic") or "").strip()]
    truncated["topic_facts"] = len(topic_facts) > MAX_TOPIC_FACTS
    for fact in topic_facts[:MAX_TOPIC_FACTS]:
        topic = str(fact["values"]["topic"]).strip()
        asset = key_to_asset.get(str(fact.get("file") or ""))
        tkey = identity.message_topic_entity_key(topic)
        record = entities.get(tkey)
        if not record:
            record = _desired_entity(EntityType.MESSAGE_TOPIC, tkey, topic)
            record["aliases"].append({"alias": topic,
                                      "alias_type": AliasType.IDENTIFIER})
            entities[tkey] = record
        if asset:
            binding = {"ref_kind": BindingRefKind.ASSET,
                       "ref_id": asset["id"],
                       "ref_key": asset["logical_key"],
                       "binding_role": BindingRole.SUPPORTING}
            if binding not in record["bindings"]:
                record["bindings"].append(binding)

    return {"entities": entities, "relations": relations,
            "truncated": {k: v for k, v in truncated.items() if v}}


def compute_source_hash(workspace_id: str,
                        desired: Optional[Dict[str, Any]] = None) -> str:
    """Deterministic hash of the projection INPUT state. Derived from the
    desired output (which is a pure function of the inputs), so any relevant
    source change — asset set, revisions, segments, call edges, facets —
    changes it, and irrelevant changes do not."""
    desired = desired or compute_desired_state(workspace_id)
    payload = json.dumps({
        "version": GRAPH_PROJECTOR_VERSION,
        "entities": {k: {kk: vv for kk, vv in v.items()}
                     for k, v in sorted(desired["entities"].items())},
        "relations": {"|".join(k): v for k, v in
                      sorted(desired["relations"].items())},
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _diff(workspace_id: str, desired: Dict[str, Any]) -> Dict[str, Any]:
    """What sync would write: pure data, no writes."""
    existing_entities = {
        e["canonical_key"]: e for e in store.list_entities(
            workspace_id, origin=GraphOrigin.DETERMINISTIC, limit=200_000)}
    bindings = store.list_workspace_bindings(workspace_id,
                                             status=BindingStatus.ACTIVE)
    bindings_by_entity: Dict[str, List[Dict[str, Any]]] = {}
    for b in bindings:
        bindings_by_entity.setdefault(b["entity_id"], []).append(b)

    create_entities: List[str] = []
    revive_entities: List[str] = []
    stale_entities: List[str] = []
    binding_inserts: List[Tuple[str, Dict[str, Any]]] = []   # (key, binding)
    binding_stales: List[Dict[str, Any]] = []
    for key, want in desired["entities"].items():
        have = existing_entities.get(key)
        if not have:
            create_entities.append(key)
            continue
        if have["lifecycle_status"] == GraphLifecycleStatus.STALE:
            revive_entities.append(key)
        want_bindings = {(b["ref_kind"], b["ref_id"], b["binding_role"])
                         for b in want["bindings"]}
        have_bindings = {(b["ref_kind"], b["ref_id"], b["binding_role"]): b
                         for b in bindings_by_entity.get(have["id"], [])}
        for b in want["bindings"]:
            if (b["ref_kind"], b["ref_id"], b["binding_role"]) \
                    not in have_bindings:
                binding_inserts.append((key, b))
        for sig, b in have_bindings.items():
            if b["origin"] == GraphOrigin.DETERMINISTIC and \
                    sig not in want_bindings:
                binding_stales.append(b)
    for key, have in existing_entities.items():
        if key not in desired["entities"] and \
                have["lifecycle_status"] == GraphLifecycleStatus.ACTIVE:
            stale_entities.append(key)

    existing_relations: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    id_to_key = {e["id"]: k for k, e in existing_entities.items()}
    for rel in store.list_relations(workspace_id,
                                    origin=GraphOrigin.DETERMINISTIC,
                                    limit=200_000):
        skey = id_to_key.get(rel["source_entity_id"])
        tkey = id_to_key.get(rel["target_entity_id"])
        if skey and tkey:
            existing_relations[(skey, tkey, rel["relation_type"])] = rel
    create_relations = [k for k in desired["relations"]
                        if k not in existing_relations
                        or existing_relations[k]["lifecycle_status"] !=
                        GraphLifecycleStatus.ACTIVE]
    revive_relations = [k for k in desired["relations"]
                        if k in existing_relations
                        and existing_relations[k]["lifecycle_status"] ==
                        GraphLifecycleStatus.STALE]
    create_relations = [k for k in create_relations
                        if k not in revive_relations]
    stale_relations = [k for k, rel in existing_relations.items()
                       if k not in desired["relations"]
                       and rel["lifecycle_status"] ==
                       GraphLifecycleStatus.ACTIVE]
    return {
        "existing_entities": existing_entities,
        "existing_relations": existing_relations,
        "create_entities": sorted(create_entities),
        "revive_entities": sorted(revive_entities),
        "stale_entities": sorted(stale_entities),
        "binding_inserts": binding_inserts,
        "binding_stales": binding_stales,
        "create_relations": sorted(create_relations),
        "revive_relations": sorted(revive_relations),
        "stale_relations": sorted(stale_relations),
    }


def plan_seed(workspace_id: str) -> Dict[str, Any]:
    """Dry-run: what a seed/sync would change. Writes nothing."""
    desired = compute_desired_state(workspace_id)
    source_hash = compute_source_hash(workspace_id, desired)
    state = store.get_projection_state(workspace_id)
    diff = _diff(workspace_id, desired)
    unchanged = bool(state) and \
        state["source_knowledge_hash"] == source_hash and \
        state["projector_version"] == GRAPH_PROJECTOR_VERSION
    return {
        "workspace_id": workspace_id,
        "projector_version": GRAPH_PROJECTOR_VERSION,
        "source_hash": source_hash,
        "stored_hash": (state or {}).get("source_knowledge_hash", ""),
        "unchanged": unchanged,
        "desired_entities": len(desired["entities"]),
        "desired_relations": len(desired["relations"]),
        "would_create_entities": len(diff["create_entities"]),
        "would_revive_entities": len(diff["revive_entities"]),
        "would_stale_entities": len(diff["stale_entities"]),
        "would_create_relations": len(diff["create_relations"]),
        "would_revive_relations": len(diff["revive_relations"]),
        "would_stale_relations": len(diff["stale_relations"]),
        "would_add_bindings": len(diff["binding_inserts"]),
        "would_stale_bindings": len(diff["binding_stales"]),
        "truncated": desired["truncated"],
        "knowledge_revision": store.current_revision_number(workspace_id),
    }


def sync_graph(workspace_id: str, *, actor: str = "",
               seed: bool = False) -> Dict[str, Any]:
    """Project the deterministic state into the graph, incrementally.

    Unchanged source hash → zero writes, zero Knowledge Revisions (the
    watermark is refreshed). Otherwise exactly the diff is written in one
    graph transaction that mints one Knowledge Revision.
    """
    desired = compute_desired_state(workspace_id)
    source_hash = compute_source_hash(workspace_id, desired)
    state = store.get_projection_state(workspace_id)
    action_label = RevisionAction.GRAPH_SEED if (seed or not state) \
        else RevisionAction.GRAPH_SYNC

    if state and state["source_knowledge_hash"] == source_hash and \
            state["projector_version"] == GRAPH_PROJECTOR_VERSION:
        return {"workspace_id": workspace_id, "action": "noop",
                "changed": False, "source_hash": source_hash,
                "projector_version": GRAPH_PROJECTOR_VERSION,
                "knowledge_revision":
                    store.current_revision_number(workspace_id),
                "truncated": desired["truncated"]}

    diff = _diff(workspace_id, desired)
    changed = any((diff["create_entities"], diff["revive_entities"],
                   diff["stale_entities"], diff["binding_inserts"],
                   diff["binding_stales"], diff["create_relations"],
                   diff["revive_relations"], diff["stale_relations"]))
    if not changed:
        # Source hash moved (e.g. first run over an empty workspace) but the
        # graph diff is empty: refresh the watermark without a revision.
        store.touch_projection_state(
            workspace_id, projector_version=GRAPH_PROJECTOR_VERSION,
            source_knowledge_hash=source_hash)
        return {"workspace_id": workspace_id, "action": "noop",
                "changed": False, "source_hash": source_hash,
                "projector_version": GRAPH_PROJECTOR_VERSION,
                "knowledge_revision":
                    store.current_revision_number(workspace_id),
                "truncated": desired["truncated"]}

    alias_collisions: List[Dict[str, Any]] = []
    counts = {"entities_created": 0, "entities_revived": 0,
              "entities_staled": 0, "relations_created": 0,
              "relations_revived": 0, "relations_staled": 0,
              "bindings_added": 0, "bindings_staled": 0, "aliases_added": 0}

    with store.graph_transaction(workspace_id, action=action_label,
                                 actor=actor,
                                 summary="deterministic graph projection"
                                 ) as tx:
        key_to_id: Dict[str, str] = {
            k: e["id"] for k, e in diff["existing_entities"].items()}

        def _add_aliases(entity_id: str, key: str,
                         wanted: List[Dict[str, Any]]) -> None:
            existing = {a["normalized_alias"] for a in store.list_aliases(
                workspace_id, entity_id, status=None)}
            for alias in wanted:
                normalized = identity.normalize_alias(alias["alias"])
                if not normalized or normalized in existing:
                    continue
                holders = store.find_alias_holders(workspace_id, normalized,
                                                   exclude_entity_id=entity_id)
                if holders:
                    alias_collisions.append(
                        {"alias": alias["alias"], "entity_key": key,
                         "held_by": [h["holder_canonical_key"]
                                     for h in holders]})
                    continue    # reported, never silently attached
                tx.insert_alias(entity_id=entity_id, alias=alias["alias"],
                                normalized_alias=normalized,
                                alias_type=alias["alias_type"],
                                origin=GraphOrigin.DETERMINISTIC)
                counts["aliases_added"] += 1
                existing.add(normalized)

        for key in diff["create_entities"]:
            want = desired["entities"][key]
            entity = tx.insert_entity(
                entity_type=want["entity_type"], canonical_key=key,
                display_name=want["display_name"],
                description=want["description"],
                origin=GraphOrigin.DETERMINISTIC)
            key_to_id[key] = entity["id"]
            counts["entities_created"] += 1
            for binding in want["bindings"]:
                tx.insert_binding(entity_id=entity["id"],
                                  ref_kind=binding["ref_kind"],
                                  ref_id=binding["ref_id"],
                                  ref_key=binding.get("ref_key", ""),
                                  binding_role=binding["binding_role"],
                                  origin=GraphOrigin.DETERMINISTIC,
                                  evidence_id=binding.get("evidence_id", ""))
                counts["bindings_added"] += 1
            _add_aliases(entity["id"], key, want["aliases"])

        for key in diff["revive_entities"]:
            entity_id = key_to_id[key]
            tx.update_entity(entity_id,
                             lifecycle_status=GraphLifecycleStatus.ACTIVE,
                             stale_at=None)
            counts["entities_revived"] += 1

        for key, binding in diff["binding_inserts"]:
            entity_id = key_to_id.get(key)
            if not entity_id:
                continue
            tx.insert_binding(entity_id=entity_id,
                              ref_kind=binding["ref_kind"],
                              ref_id=binding["ref_id"],
                              ref_key=binding.get("ref_key", ""),
                              binding_role=binding["binding_role"],
                              origin=GraphOrigin.DETERMINISTIC,
                              evidence_id=binding.get("evidence_id", ""))
            counts["bindings_added"] += 1

        for binding in diff["binding_stales"]:
            tx.update_binding(binding["id"],
                              status=BindingStatus.STALE, stale_at=tx.ts)
            counts["bindings_staled"] += 1

        for key in diff["stale_entities"]:
            entity_id = key_to_id[key]
            tx.update_entity(entity_id,
                             lifecycle_status=GraphLifecycleStatus.STALE,
                             stale_at=tx.ts)
            counts["entities_staled"] += 1

        for rkey in diff["create_relations"]:
            src_key, tgt_key, rtype = rkey
            want = desired["relations"][rkey]
            src_id = key_to_id.get(src_key)
            tgt_id = key_to_id.get(tgt_key)
            if not src_id or not tgt_id or src_id == tgt_id:
                continue
            evidence = []
            if want.get("evidence_id"):
                evidence = [{"evidence_id": want["evidence_id"],
                             "role": "primary", "quote": "",
                             "quote_hash": identity.quote_hash("")}]
            tx.insert_relation(
                source_entity_id=src_id, target_entity_id=tgt_id,
                relation_type=rtype,
                relation_state=want["relation_state"],
                confidence=want["confidence"],
                origin=GraphOrigin.DETERMINISTIC,
                metadata=want.get("metadata") or {},
                evidence=evidence)
            counts["relations_created"] += 1

        for rkey in diff["revive_relations"]:
            rel = diff["existing_relations"][rkey]
            # Restore the epistemic state that was parked when it went stale.
            prior = (rel.get("metadata") or {}).get(
                "prior_state", RelationState.INFERRED)
            tx.update_relation(rel["id"],
                               lifecycle_status=GraphLifecycleStatus.ACTIVE,
                               relation_state=prior, stale_at=None)
            counts["relations_revived"] += 1

        for rkey in diff["stale_relations"]:
            rel = diff["existing_relations"][rkey]
            meta = dict(rel.get("metadata") or {})
            meta["prior_state"] = rel["relation_state"]
            tx.update_relation(rel["id"],
                               lifecycle_status=GraphLifecycleStatus.STALE,
                               relation_state=RelationState.STALE,
                               metadata=meta, stale_at=tx.ts)
            counts["relations_staled"] += 1

        tx.set_projection_state(projector_version=GRAPH_PROJECTOR_VERSION,
                                source_knowledge_hash=source_hash)
        revision_number = tx.revision_number

    return {
        "workspace_id": workspace_id,
        "action": "seeded" if action_label == RevisionAction.GRAPH_SEED
                  else "synced",
        "changed": True,
        "source_hash": source_hash,
        "projector_version": GRAPH_PROJECTOR_VERSION,
        **counts,
        "alias_collisions": alias_collisions,
        "truncated": desired["truncated"],
        "knowledge_revision": revision_number,
    }


def seed_graph(workspace_id: str, *, actor: str = "") -> Dict[str, Any]:
    """First projection (or a full re-check): same incremental writer."""
    return sync_graph(workspace_id, actor=actor, seed=True)


__all__ = ["plan_seed", "seed_graph", "sync_graph", "compute_desired_state",
           "compute_source_hash"]
