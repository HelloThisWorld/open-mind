"""The persistence surface :class:`~openmind.services.workspace_service.WorkspaceService`
depends on.

This is a :class:`typing.Protocol`, not a base class: :mod:`openmind.db`
already satisfies it structurally, so nothing has to be registered, subclassed
or adapted. The port exists for one concrete reason — service tests inject a
fake repository to exercise ordering, error and not-found paths without a real
SQLite file or a real vector store.

It is deliberately narrow. It covers only what the workspace use cases need;
the rest of :mod:`openmind.db` (Ask history, kv, model config) is reached
directly by the modules that own those concerns.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceRepository(Protocol):
    """Structural contract satisfied by :mod:`openmind.db`."""

    def create_project(self, name: str,
                       project_id: Optional[str] = None) -> Dict[str, Any]: ...

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]: ...

    def list_projects(self, state: Optional[str] = None) -> List[Dict[str, Any]]: ...

    def update_project_meta(self, project_id: str, meta: Dict[str, Any]) -> None: ...

    def get_project_paths(self, project_id: str) -> List[Dict[str, Any]]: ...

    def upsert_project_path(self, project_id: str, path: str,
                            exclude: List[str]) -> None: ...

    def remove_project_path(self, project_id: str, path: str) -> None: ...

    def get_file_index(self, project_id: str) -> Dict[str, Dict[str, Any]]: ...


__all__ = ["WorkspaceRepository"]
