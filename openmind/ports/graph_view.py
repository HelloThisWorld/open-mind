"""The read-only Graph View port (OpenMind v2 Phase 7).

A narrow protocol over the graph READ surface, so the same impact/trace logic
can run against either the canonical graph or a virtual overlay graph. Unlike
:mod:`openmind.ports.graph_repository` (which includes the write transaction),
a Graph View is strictly read-only — an overlay must never be able to write.

Two implementations live in :mod:`openmind.overlays.graph_view`:

* ``CanonicalGraphView`` — wraps :mod:`openmind.knowledge.store` and returns
  byte-identical results to today's canonical reads;
* ``OverlayGraphView`` — composes the canonical view with an overlay's graph
  delta (added / modified / removed objects) WITHOUT changing a Base row.

Every node returned by a view discloses its ``origin`` (``base`` / ``overlay``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GraphView(Protocol):
    """The read-only graph surface impact analysis depends on."""

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]: ...

    def find_entity_by_key(self, entity_type: str, canonical_key: str
                           ) -> Optional[Dict[str, Any]]: ...

    def list_entities(self, *, entity_type: Optional[str] = None,
                      lifecycle_status: Optional[str] = "active",
                      limit: int = 500) -> List[Dict[str, Any]]: ...

    def get_claim(self, claim_id: str) -> Optional[Dict[str, Any]]: ...

    def list_claims(self, *, entity_id: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]: ...

    def get_relation(self, relation_id: str) -> Optional[Dict[str, Any]]: ...

    def list_relations(self, *, source_entity_id: Optional[str] = None,
                       target_entity_id: Optional[str] = None,
                       relation_type: Optional[str] = None,
                       limit: int = 1000) -> List[Dict[str, Any]]: ...

    def neighbors(self, entity_id: str, *, direction: str = "both"
                  ) -> List[Dict[str, Any]]: ...

    def evidence_for_relation(self, relation_id: str) -> List[Dict[str, Any]]: ...


__all__ = ["GraphView"]
