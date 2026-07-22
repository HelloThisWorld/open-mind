"""Graph search: deterministic fusion of exact, lexical and vector signals.

Precedence is fixed and tested (spec §30): an exact canonical key beats an
exact alias, which beats an exact identifier token, which beats a lexical
substring, which beats vector similarity. An exact identifier can therefore
never be outranked by something that merely reads similar. Entities and
Claims are returned as SEPARATE sections, each hit carrying either Evidence
ids or a navigable Claim id, plus the current Knowledge Revision.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import identity, store, vector_projection
from .vocabularies import GraphLifecycleStatus

#: Deterministic tier scores. Vector similarity is scaled into (0, TIER_GAP)
#: so it can never cross into the lexical tier above it.
SCORE_EXACT_KEY = 100.0
SCORE_EXACT_ALIAS = 90.0
SCORE_EXACT_TOKEN = 80.0
SCORE_LEXICAL = 50.0
SCORE_VECTOR_MAX = 40.0

MAX_LIMIT = 100


def _token_pattern(query: str) -> Optional[re.Pattern]:
    text = str(query or "").strip()
    if not text or " " in text:
        return None
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(text)
                      + r"(?![A-Za-z0-9_])")


def search_graph(workspace_id: str, query: str, *, limit: int = 20,
                 include_stale: bool = False) -> Dict[str, Any]:
    """One fused search over active Entities and Claims."""
    query = str(query or "").strip()
    limit = max(1, min(int(limit), MAX_LIMIT))
    lifecycle = None if include_stale else GraphLifecycleStatus.ACTIVE

    entity_hits: Dict[str, Dict[str, Any]] = {}
    claim_hits: Dict[str, Dict[str, Any]] = {}

    def add_entity(entity: Dict[str, Any], score: float, via: str) -> None:
        if not include_stale and entity["lifecycle_status"] != \
                GraphLifecycleStatus.ACTIVE:
            return
        hit = entity_hits.get(entity["id"])
        if hit is None or score > hit["score"]:
            entity_hits[entity["id"]] = {"entity": entity, "score": score,
                                         "via": via}

    def add_claim(claim: Dict[str, Any], score: float, via: str) -> None:
        if not include_stale and claim["lifecycle_status"] != \
                GraphLifecycleStatus.ACTIVE:
            return
        hit = claim_hits.get(claim["id"])
        if hit is None or score > hit["score"]:
            claim_hits[claim["id"]] = {"claim": claim, "score": score,
                                       "via": via}

    if query:
        # 1. exact canonical key. Keys embed their type as the first segment
        # ("requirement:REQ-NC-017"), so a full-key query resolves directly
        # through the identity index; the lexical pass below additionally
        # catches case-insensitive full-key matches.
        if ":" in query:
            direct = store.find_entity_by_key(
                workspace_id, query.split(":", 1)[0].strip().lower(), query)
            if direct:
                add_entity(direct, SCORE_EXACT_KEY, "canonical-key")
        for entity in store.search_entities_lexical(
                workspace_id, query, lifecycle_status=lifecycle,
                limit=MAX_LIMIT):
            if entity["canonical_key"].lower() == query.lower():
                add_entity(entity, SCORE_EXACT_KEY, "canonical-key")

        # 2. exact normalized alias.
        for entity in store.find_entity_by_alias(
                workspace_id, identity.normalize_alias(query)):
            add_entity(entity, SCORE_EXACT_ALIAS, "alias")

        # 3 + 4. token / lexical over entities and claims.
        token = _token_pattern(query)
        for entity in store.search_entities_lexical(
                workspace_id, query, lifecycle_status=lifecycle,
                limit=MAX_LIMIT):
            haystack = (f"{entity['canonical_key']} "
                        f"{entity['display_name']} "
                        f"{entity.get('description', '')}")
            if token and token.search(haystack):
                add_entity(entity, SCORE_EXACT_TOKEN, "identifier-token")
            else:
                add_entity(entity, SCORE_LEXICAL, "lexical")
        for claim in store.search_claims_lexical(
                workspace_id, query, lifecycle_status=lifecycle,
                limit=MAX_LIMIT):
            if token and token.search(claim["statement"]):
                add_claim(claim, SCORE_EXACT_TOKEN, "identifier-token")
            else:
                add_claim(claim, SCORE_LEXICAL, "lexical")

        # 5. vector similarity (never crosses into the lexical tier).
        try:
            vector_hits = vector_projection.query(workspace_id, query,
                                                  limit=MAX_LIMIT)
        except Exception:
            vector_hits = []        # projection absent/failed: exact still works
        for hit in vector_hits:
            similarity = max(0.0, min(1.0, hit.get("similarity", 0.0)))
            score = similarity * SCORE_VECTOR_MAX
            kind = (hit.get("metadata") or {}).get("object_kind", "")
            if kind == "entity":
                entity = store.get_entity(workspace_id, hit["id"])
                if entity:
                    add_entity(entity, score, "vector")
            elif kind == "claim":
                claim = store.get_claim(workspace_id, hit["id"])
                if claim:
                    add_claim(claim, score, "vector")

    def entity_result(hit: Dict[str, Any]) -> Dict[str, Any]:
        entity = hit["entity"]
        claims = store.list_claims(
            workspace_id, entity_id=entity["id"],
            lifecycle_status=GraphLifecycleStatus.ACTIVE, limit=5)
        return {
            "id": entity["id"], "object_kind": "entity",
            "entity_type": entity["entity_type"],
            "canonical_key": entity["canonical_key"],
            "display_name": entity["display_name"],
            "lifecycle_status": entity["lifecycle_status"],
            "authority_status": entity["authority_status"],
            "origin": entity["origin"],
            "score": round(hit["score"], 3), "matched_via": hit["via"],
            "claim_ids": [c["id"] for c in claims],
        }

    def claim_result(hit: Dict[str, Any]) -> Dict[str, Any]:
        claim = hit["claim"]
        evidence = store.claim_evidence(claim["id"])
        return {
            "id": claim["id"], "object_kind": "claim",
            "entity_id": claim["entity_id"],
            "claim_type": claim["claim_type"],
            "statement": claim["statement"],
            "lifecycle_status": claim["lifecycle_status"],
            "authority_status": claim["authority_status"],
            "origin": claim["origin"],
            "score": round(hit["score"], 3), "matched_via": hit["via"],
            "evidence_ids": [e["evidence_id"] for e in evidence],
        }

    entities = sorted(entity_hits.values(),
                      key=lambda h: (-h["score"],
                                     h["entity"]["canonical_key"],
                                     h["entity"]["id"]))[:limit]
    claims = sorted(claim_hits.values(),
                    key=lambda h: (-h["score"], h["claim"]["id"]))[:limit]
    return {
        "workspace_id": workspace_id,
        "query": query,
        "entities": [entity_result(h) for h in entities],
        "claims": [claim_result(h) for h in claims],
        "knowledge_revision": store.current_revision_number(workspace_id),
    }


__all__ = ["search_graph"]
