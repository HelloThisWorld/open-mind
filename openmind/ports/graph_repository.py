"""The graph repository port (OpenMind v2 Phase 5).

A narrow protocol over the canonical-graph persistence functions that the
:class:`~openmind.knowledge.service.KnowledgeService` and the deterministic
projector depend on. Like the other ports, it exists as a TESTING SEAM: a
test can substitute an in-memory implementation to exercise service logic
without a database, and the protocol documents exactly which persistence
surface the graph layer is allowed to touch.

The production implementation is :mod:`openmind.knowledge.store` (module
functions over the shared WAL connection); :data:`default_graph_repository`
adapts it to this protocol.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class GraphRepository(Protocol):
    """What the knowledge layer requires from graph persistence."""

    # -- transactions -------------------------------------------------------
    def graph_transaction(self, workspace_id: str, *, action: str,
                          actor: str = "", summary: str = "",
                          change_set_id: str = ""):
        """A context manager yielding a transaction handle with the insert/
        update methods of :class:`openmind.knowledge.store.GraphTransaction`.
        One successful body = one Knowledge Revision; a failed body writes
        nothing."""
        ...

    # -- entity reads -------------------------------------------------------
    def get_entity(self, workspace_id: str,
                   entity_id: str) -> Optional[Dict[str, Any]]: ...

    def find_entity_by_key(self, workspace_id: str, entity_type: str,
                           canonical_key: str) -> Optional[Dict[str, Any]]: ...

    def list_entities(self, workspace_id: str,
                      **filters: Any) -> List[Dict[str, Any]]: ...

    # -- claim reads --------------------------------------------------------
    def get_claim(self, workspace_id: str,
                  claim_id: str) -> Optional[Dict[str, Any]]: ...

    def find_claim_by_hash(self, workspace_id: str, entity_id: str,
                           normalized_statement_hash: str,
                           lifecycle_status: str = "active"
                           ) -> Optional[Dict[str, Any]]: ...

    # -- relation reads -----------------------------------------------------
    def get_relation(self, workspace_id: str,
                     relation_id: str) -> Optional[Dict[str, Any]]: ...

    def find_active_relation(self, workspace_id: str, source_entity_id: str,
                             target_entity_id: str, relation_type: str
                             ) -> Optional[Dict[str, Any]]: ...

    # -- ledger -------------------------------------------------------------
    def current_revision_number(self, workspace_id: str) -> int: ...

    def find_promotion(self, workspace_id: str, candidate_kind: str,
                       candidate_id: str) -> Optional[Dict[str, Any]]: ...


class _ModuleGraphRepository:
    """Adapts :mod:`openmind.knowledge.store` to :class:`GraphRepository`.

    Thin by design — every attribute defers to the store module at call time,
    so a test that monkeypatches a store function still takes effect through
    the repository."""

    def __getattr__(self, name: str) -> Any:
        from ..knowledge import store
        return getattr(store, name)


def default_graph_repository() -> Any:
    """The production repository over the shared SQLite connection."""
    return _ModuleGraphRepository()


__all__ = ["GraphRepository", "default_graph_repository"]
