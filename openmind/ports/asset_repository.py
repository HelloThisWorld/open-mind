"""The persistence surface :class:`~openmind.services.asset_service.AssetService`
depends on.

Like :mod:`openmind.ports.workspace_repository`, this is a
:class:`typing.Protocol` structurally satisfied by :mod:`openmind.db` — nothing
is registered, subclassed or adapted. The port exists so service tests can inject
a fake repository and exercise the workspace-scoping, not-found and ordering
paths without a real SQLite file, a real content store, or a real ingest.

It is intentionally narrow: only the reads the Asset use cases need plus the one
transactional writer (:meth:`commit_revision`). Everything else in the Asset
section of :mod:`openmind.db` (the ingestion-facing helpers, cascade cleanup) is
reached directly by the modules that own those concerns.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class AssetRepository(Protocol):
    """Structural contract satisfied by :mod:`openmind.db`."""

    def get_asset(self, workspace_id: str,
                  asset_id: str) -> Optional[Dict[str, Any]]: ...

    def find_asset_by_logical_key(self, workspace_id: str,
                                  logical_key: str) -> Optional[Dict[str, Any]]: ...

    def list_assets(self, workspace_id: str, asset_type: Optional[str] = None,
                    state: Optional[str] = None, limit: int = 100,
                    offset: int = 0) -> List[Dict[str, Any]]: ...

    def count_assets(self, workspace_id: str, asset_type: Optional[str] = None,
                     state: Optional[str] = None) -> int: ...

    def get_revision(self, workspace_id: str,
                     revision_id: str) -> Optional[Dict[str, Any]]: ...

    def list_revisions(self, workspace_id: str, asset_id: str,
                       limit: int = 50) -> List[Dict[str, Any]]: ...

    def list_segments(self, workspace_id: str, revision_id: str, limit: int = 200,
                      offset: int = 0) -> List[Dict[str, Any]]: ...

    def count_segments(self, workspace_id: str, revision_id: str) -> int: ...

    def get_segment(self, workspace_id: str,
                    segment_id: str) -> Optional[Dict[str, Any]]: ...

    def get_evidence(self, workspace_id: str,
                     evidence_id: str) -> Optional[Dict[str, Any]]: ...

    def get_evidence_for_segment(self, workspace_id: str,
                                 segment_id: str) -> Optional[Dict[str, Any]]: ...

    def evidence_ids_for_revision(self, workspace_id: str,
                                  revision_id: str) -> Dict[str, str]: ...

    def asset_stats(self, workspace_id: str) -> Dict[str, int]: ...


__all__ = ["AssetRepository"]
