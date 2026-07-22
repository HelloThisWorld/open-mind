"""The graph vector projection: ``knowledge_<workspace-id>``.

A SEPARATE per-workspace collection — graph text never enters ``code_<ws>``
or ``documents_<ws>`` (their retrieval contracts are frozen), and their
chunks never enter this one. Indexed objects are ACTIVE Entities and Claims
only; Relations are not embedded (no tested retrieval need).

The projection is derived state: SQLite stays the source of truth, and a
projection refresh replaces the workspace's rows content-addressed by id.
Embeddings are the same local embedder every other collection uses — no
cloud embedding path exists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import embeddings, vectorstore
from . import store
from .vocabularies import GraphLifecycleStatus

#: Bounded text sizes for the embedded documents.
MAX_ENTITY_TEXT_CHARS = 2_000
MAX_CLAIM_TEXT_CHARS = 2_000
MAX_PRIMARY_CLAIMS_IN_ENTITY_TEXT = 3


def collection_name(workspace_id: str) -> str:
    return vectorstore.knowledge_collection_name(workspace_id)


def entity_text(workspace_id: str, entity: Dict[str, Any]) -> str:
    """The embedded text of one Entity: type, key, name, aliases, bounded
    description and its first active primary claims."""
    aliases = [a["alias"] for a in store.list_aliases(workspace_id,
                                                      entity["id"])]
    claims = store.list_claims(workspace_id, entity_id=entity["id"],
                               lifecycle_status=GraphLifecycleStatus.ACTIVE,
                               limit=MAX_PRIMARY_CLAIMS_IN_ENTITY_TEXT)
    parts = [
        f"{entity['entity_type']} {entity['canonical_key']}",
        entity["display_name"],
    ]
    if aliases:
        parts.append("aliases: " + ", ".join(aliases[:10]))
    if entity.get("description"):
        parts.append(entity["description"])
    for claim in claims:
        parts.append(claim["statement"])
    return "\n".join(p for p in parts if p)[:MAX_ENTITY_TEXT_CHARS]


def claim_text(workspace_id: str, claim: Dict[str, Any],
               entity: Optional[Dict[str, Any]] = None) -> str:
    """The embedded text of one Claim: type, statement, entity identity and
    a bounded evidence locator summary."""
    entity = entity or store.get_entity(workspace_id, claim["entity_id"])
    locators: List[str] = []
    from .. import db
    for ev in store.claim_evidence(claim["id"])[:3]:
        record = db.get_evidence(workspace_id, ev["evidence_id"])
        loc = (record or {}).get("locator") or {}
        name = loc.get("file") or loc.get("document") or ""
        if name:
            locators.append(str(name))
    parts = [
        f"{claim['claim_type']}: {claim['statement']}",
        f"entity: {(entity or {}).get('canonical_key', '')}",
    ]
    if locators:
        parts.append("sources: " + ", ".join(sorted(set(locators))))
    return "\n".join(p for p in parts if p)[:MAX_CLAIM_TEXT_CHARS]


def _metadata(workspace_id: str, *, object_kind: str,
              entity_id: str = "", claim_id: str = "",
              entity_type: str = "", claim_type: str = "",
              lifecycle_status: str = "", authority_status: str = "",
              origin: str = "", content_hash: str = "") -> Dict[str, Any]:
    return {
        "workspace_id": workspace_id, "object_kind": object_kind,
        "entity_id": entity_id, "claim_id": claim_id,
        "entity_type": entity_type, "claim_type": claim_type,
        "lifecycle_status": lifecycle_status,
        "authority_status": authority_status, "origin": origin,
        "knowledge_revision": store.current_revision_number(workspace_id),
        "content_hash": content_hash,
    }


def refresh_workspace(workspace_id: str) -> Dict[str, int]:
    """Rebuild the workspace's graph projection to match ACTIVE graph state.

    Content-addressed by row id: active objects are upserted, projections of
    no-longer-active objects are deleted (a merged source entity's active
    projection disappears here). Idempotent; failures must be caught by the
    caller — a projection failure never rolls back canonical SQLite state.
    """
    import hashlib

    entities = store.list_entities(
        workspace_id, lifecycle_status=GraphLifecycleStatus.ACTIVE,
        limit=50_000)
    claims = store.list_claims(
        workspace_id, lifecycle_status=GraphLifecycleStatus.ACTIVE,
        limit=100_000)
    entity_by_id = {e["id"]: e for e in entities}

    ids: List[str] = []
    texts: List[str] = []
    metas: List[Dict[str, Any]] = []
    for entity in entities:
        text = entity_text(workspace_id, entity)
        if not text.strip():
            continue
        ids.append(entity["id"])
        texts.append(text)
        metas.append(_metadata(
            workspace_id, object_kind="entity", entity_id=entity["id"],
            entity_type=entity["entity_type"],
            lifecycle_status=entity["lifecycle_status"],
            authority_status=entity["authority_status"],
            origin=entity["origin"],
            content_hash=hashlib.sha256(
                text.encode("utf-8")).hexdigest()))
    for claim in claims:
        text = claim_text(workspace_id, claim,
                          entity_by_id.get(claim["entity_id"]))
        if not text.strip():
            continue
        ids.append(claim["id"])
        texts.append(text)
        metas.append(_metadata(
            workspace_id, object_kind="claim", claim_id=claim["id"],
            entity_id=claim["entity_id"], claim_type=claim["claim_type"],
            lifecycle_status=claim["lifecycle_status"],
            authority_status=claim["authority_status"],
            origin=claim["origin"],
            content_hash=hashlib.sha256(
                text.encode("utf-8")).hexdigest()))

    collection = vectorstore.get_store(collection_name(workspace_id))
    existing = set(collection.get().get("ids") or [])
    stale_ids = sorted(existing - set(ids))
    if stale_ids:
        collection.delete(ids=stale_ids)
    if ids:
        vectors = embeddings.embed(texts)
        collection.upsert(ids, vectors, texts, metas)
    return {"indexed": len(ids), "removed": len(stale_ids)}


def query(workspace_id: str, text: str, *, limit: int = 20,
          object_kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Vector-similarity hits over the graph collection. Read-only; an
    absent collection returns []."""
    name = collection_name(workspace_id)
    if vectorstore.count_collection(name) == 0:
        return []
    collection = vectorstore.get_store(name)
    where: Optional[Dict[str, Any]] = None
    if object_kind:
        where = {"object_kind": object_kind}
    result = collection.query(embeddings.embed_one(text),
                              n_results=max(1, int(limit)), where=where)
    hits: List[Dict[str, Any]] = []
    for i, hit_id in enumerate(result.get("ids") or []):
        meta = (result.get("metadatas") or [{}])[i] or {}
        distance = (result.get("distances") or [0.0])[i]
        hits.append({"id": hit_id, "metadata": meta,
                     "distance": float(distance),
                     "similarity": 1.0 - float(distance)})
    return hits


def drop_workspace(workspace_id: str) -> None:
    """Drop the graph collection (terminate/delete path). Uses the shared
    batched-drain drop, so the app never freezes on a large projection."""
    vectorstore.drop_collection(collection_name(workspace_id))


__all__ = ["collection_name", "refresh_workspace", "query",
           "drop_workspace", "entity_text", "claim_text"]
