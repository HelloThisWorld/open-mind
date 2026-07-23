"""Canonical and virtual-overlay Graph Views (spec §21).

``CanonicalGraphView`` is a thin, read-only wrapper over
:mod:`openmind.knowledge.store` — it returns exactly what a canonical read
returns today, tagged ``origin='base'``. ``OverlayGraphView`` composes that
base view with an overlay's graph delta:

    Base active object + added delta + modified delta − removed delta
        = virtual overlay graph

No Base row is ever changed. A removed Base object simply disappears from the
virtual view; a modified Base object is returned with its overlay overrides but
keeps a link to the Base object; an added overlay object uses an overlay id and
a canonical-key CANDIDATE. Every returned node discloses its ``origin``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..knowledge import store as kstore
from ..knowledge.vocabularies import GraphLifecycleStatus
from . import store as ovl_store


class CanonicalGraphView:
    """Read-only view of the canonical graph for one workspace."""

    origin_default = "base"

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id

    @staticmethod
    def _tag(obj: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if obj is None:
            return None
        obj = dict(obj)
        obj.setdefault("origin_view", "base")
        return obj

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        return self._tag(kstore.get_entity(self.workspace_id, entity_id))

    def find_entity_by_key(self, entity_type: str, canonical_key: str
                           ) -> Optional[Dict[str, Any]]:
        return self._tag(kstore.find_entity_by_key(
            self.workspace_id, entity_type, canonical_key))

    def list_entities(self, *, entity_type: Optional[str] = None,
                      lifecycle_status: Optional[str] = "active",
                      limit: int = 500) -> List[Dict[str, Any]]:
        rows = kstore.list_entities(self.workspace_id, entity_type=entity_type,
                                    lifecycle_status=lifecycle_status,
                                    limit=limit)
        return [self._tag(r) for r in rows]

    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]:
        return self._tag(kstore.get_claim(self.workspace_id, claim_id))

    def list_claims(self, *, entity_id: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
        rows = kstore.list_claims(self.workspace_id, entity_id=entity_id,
                                  limit=limit)
        return [self._tag(r) for r in rows]

    def get_relation(self, relation_id: str) -> Optional[Dict[str, Any]]:
        return self._tag(kstore.get_relation(self.workspace_id, relation_id))

    def list_relations(self, *, source_entity_id: Optional[str] = None,
                       target_entity_id: Optional[str] = None,
                       relation_type: Optional[str] = None,
                       limit: int = 1000) -> List[Dict[str, Any]]:
        rows = kstore.list_relations(
            self.workspace_id, source_entity_id=source_entity_id,
            target_entity_id=target_entity_id, relation_type=relation_type,
            lifecycle_status=GraphLifecycleStatus.ACTIVE, limit=limit)
        return [self._tag(r) for r in rows]

    def neighbors(self, entity_id: str, *, direction: str = "both"
                  ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if direction in ("outgoing", "both"):
            out.extend(self.list_relations(source_entity_id=entity_id))
        if direction in ("incoming", "both"):
            out.extend(self.list_relations(target_entity_id=entity_id))
        return out

    def evidence_for_relation(self, relation_id: str) -> List[Dict[str, Any]]:
        return kstore.relation_evidence(relation_id)


class OverlayGraphView:
    """Virtual graph = canonical base + this overlay's delta (spec §21).

    Deltas are loaded once from the overlay store and applied in memory. The
    base view is never mutated."""

    def __init__(self, workspace_id: str, overlay_id: str, *,
                 base: Optional[CanonicalGraphView] = None) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id
        self.base = base or CanonicalGraphView(workspace_id)
        self._load_delta()

    def _load_delta(self) -> None:
        self._removed_entities: set = set()
        self._modified_entities: Dict[str, Dict[str, Any]] = {}
        self._added_entities: Dict[str, Dict[str, Any]] = {}
        self._added_by_key: Dict[str, Dict[str, Any]] = {}
        for d in ovl_store.list_entity_deltas(self.overlay_id):
            dt = d["delta_type"]
            after = d.get("after") or {}
            if dt == "removed" and d["base_entity_id"]:
                self._removed_entities.add(d["base_entity_id"])
            elif dt == "modified" and d["base_entity_id"]:
                self._modified_entities[d["base_entity_id"]] = after
            elif dt == "added":
                vid = after.get("id") or f"overlay:{d['canonical_key']}"
                node = {
                    "id": vid,
                    "entity_type": d.get("entity_type", ""),
                    "canonical_key": d.get("canonical_key", ""),
                    "display_name": after.get("display_name",
                                              d.get("canonical_key", "")),
                    "lifecycle_status": "active",
                    "authority_status": after.get("authority_status", "unknown"),
                    "origin": "overlay",
                    "origin_view": "overlay",
                }
                self._added_entities[vid] = node
                if d.get("canonical_key"):
                    self._added_by_key[d["canonical_key"]] = node
        self._removed_relations: set = set()
        self._added_relations: List[Dict[str, Any]] = []
        for d in ovl_store.list_relation_deltas(self.overlay_id):
            dt = d["delta_type"]
            if dt == "removed" and d["base_relation_id"]:
                self._removed_relations.add(d["base_relation_id"])
            elif dt == "added":
                after = d.get("after") or {}
                self._added_relations.append({
                    "id": after.get("id") or f"overlay-rel:{len(self._added_relations)}",
                    "source_entity_id": after.get("source_entity_id", ""),
                    "target_entity_id": after.get("target_entity_id", ""),
                    "relation_type": d.get("relation_type", ""),
                    "relation_state": after.get("relation_state", "asserted"),
                    "confidence": after.get("confidence", "low"),
                    "lifecycle_status": "active",
                    "origin": "overlay",
                    "origin_view": "overlay",
                })

    # -- entity reads -------------------------------------------------------
    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        if entity_id in self._removed_entities:
            return None
        if entity_id in self._added_entities:
            return dict(self._added_entities[entity_id])
        base = self.base.get_entity(entity_id)
        if base is None:
            return None
        if entity_id in self._modified_entities:
            merged = dict(base)
            merged.update(self._modified_entities[entity_id])
            merged["origin_view"] = "overlay"
            merged["base_entity_id"] = entity_id
            return merged
        return base

    def find_entity_by_key(self, entity_type: str, canonical_key: str
                           ) -> Optional[Dict[str, Any]]:
        if canonical_key in self._added_by_key:
            node = self._added_by_key[canonical_key]
            if not entity_type or node.get("entity_type") == entity_type:
                return dict(node)
        base = self.base.find_entity_by_key(entity_type, canonical_key)
        if base and base["id"] in self._removed_entities:
            return None
        if base and base["id"] in self._modified_entities:
            return self.get_entity(base["id"])
        return base

    def list_entities(self, *, entity_type: Optional[str] = None,
                      lifecycle_status: Optional[str] = "active",
                      limit: int = 500) -> List[Dict[str, Any]]:
        base_rows = [r for r in self.base.list_entities(
            entity_type=entity_type, lifecycle_status=lifecycle_status,
            limit=limit) if r["id"] not in self._removed_entities]
        for r in base_rows:
            if r["id"] in self._modified_entities:
                r.update(self._modified_entities[r["id"]])
                r["origin_view"] = "overlay"
        added = [dict(n) for n in self._added_entities.values()
                 if not entity_type or n.get("entity_type") == entity_type]
        return base_rows + added

    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]:
        return self.base.get_claim(claim_id)

    def list_claims(self, *, entity_id: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
        if entity_id and entity_id in self._removed_entities:
            return []
        return self.base.list_claims(entity_id=entity_id, limit=limit)

    # -- relation reads -----------------------------------------------------
    def get_relation(self, relation_id: str) -> Optional[Dict[str, Any]]:
        if relation_id in self._removed_relations:
            return None
        return self.base.get_relation(relation_id)

    def list_relations(self, *, source_entity_id: Optional[str] = None,
                       target_entity_id: Optional[str] = None,
                       relation_type: Optional[str] = None,
                       limit: int = 1000) -> List[Dict[str, Any]]:
        rows = [r for r in self.base.list_relations(
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id, relation_type=relation_type,
            limit=limit)
            if r["id"] not in self._removed_relations
            and r["source_entity_id"] not in self._removed_entities
            and r["target_entity_id"] not in self._removed_entities]
        for r in self._added_relations:
            if source_entity_id and r["source_entity_id"] != source_entity_id:
                continue
            if target_entity_id and r["target_entity_id"] != target_entity_id:
                continue
            if relation_type and r["relation_type"] != relation_type:
                continue
            rows.append(dict(r))
        return rows

    def neighbors(self, entity_id: str, *, direction: str = "both"
                  ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if direction in ("outgoing", "both"):
            out.extend(self.list_relations(source_entity_id=entity_id))
        if direction in ("incoming", "both"):
            out.extend(self.list_relations(target_entity_id=entity_id))
        return out

    def evidence_for_relation(self, relation_id: str) -> List[Dict[str, Any]]:
        if relation_id in self._removed_relations:
            return []
        return self.base.evidence_for_relation(relation_id)


__all__ = ["CanonicalGraphView", "OverlayGraphView"]
