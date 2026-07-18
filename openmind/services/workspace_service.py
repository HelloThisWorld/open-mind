"""Workspace use cases — create, inspect, path registration, template
selection, termination and deletion.

VOCABULARY: "workspace" is internal naming only. The stored entity is still a
project, the REST API still says ``/projects``, and ``workspace_id`` IS the
existing ``p_*`` project id. Nothing about the persisted shape changes.

This service holds the orchestration that used to live inside FastAPI route
bodies, so the same use cases are reachable from the CLI, MCP and tests without
constructing an HTTP request. It returns plain dictionaries — the same shapes
the REST API already publishes.

NOT here, on purpose:

* Template AUTO-selection. The ingest job already scores templates against the
  walked file list (``jobs._run_ingest``). Re-implementing it here would create
  a second, divergent selector. This service only reads the recorded result and
  owns the explicit user OVERRIDE.
* Path normalization. The existing REST routes store the path exactly as given,
  and path matching elsewhere depends on that. Callers that need a resolved
  absolute path (the CLI does) normalize before calling.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import db as db_module
from .. import jobs as jobs_module
from .. import machine, mapio, templates, vectorstore
from ..domain.errors import InvalidRequest, WorkspaceNotFound
from ..ports.job_repository import JobEngine
from ..ports.workspace_repository import WorkspaceRepository


class WorkspaceService:
    """Use cases over the project/workspace entity."""

    def __init__(self, repo: Optional[WorkspaceRepository] = None,
                 engine: Optional[JobEngine] = None) -> None:
        self._repo: Any = repo if repo is not None else db_module
        self._engine: Any = engine if engine is not None else jobs_module

    # -- reads --------------------------------------------------------------
    def get(self, workspace_id: str) -> Dict[str, Any]:
        """The workspace record, or raise :class:`WorkspaceNotFound`.

        A workspace being deleted is already reported as gone by the repository
        (its storage is reclaimed in the background), so this raises for it too
        rather than returning a half-alive record.
        """
        record = self._repo.get_project(workspace_id)
        if not record:
            raise WorkspaceNotFound(workspace_id)
        return record

    def list(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._repo.list_projects(state)

    def exists(self, workspace_id: str) -> bool:
        return self._repo.get_project(workspace_id) is not None

    def describe(self, workspace_id: str) -> Dict[str, Any]:
        """The workspace record decorated with live store counts — the shape
        ``GET /projects/{id}`` returns."""
        record = self.get(workspace_id)
        record["code_chunks"] = vectorstore.get_code_store(workspace_id).count()
        record["cases_count"] = vectorstore.get_cases_store(workspace_id).count()
        record["files_indexed"] = len(self._repo.get_file_index(workspace_id))
        return record

    # -- creation -----------------------------------------------------------
    def create(self, name: str, path: Optional[str] = None,
               exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create a workspace and optionally register its first source path.

        Creation never ingests. Learning is an explicit, separate step
        (``POST /ingest`` or ``openmind ingest``), so creating a workspace stays
        instant and side-effect-light.
        """
        clean = (name or "").strip()
        if not clean:
            raise InvalidRequest("workspace name must not be empty",
                                 details={"field": "name"})
        record = self._repo.create_project(clean)
        if path:
            self._repo.upsert_project_path(record["id"], path, list(exclude or []))
            record = self.get(record["id"])
        return record

    # -- paths --------------------------------------------------------------
    def add_path(self, workspace_id: str, path: str,
                 exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        """Register or update a source path and its exclude set.

        The path is stored in the machine-local sidecar, never in the portable
        database — that is what keeps a copied ``data/`` folder free of an
        absolute origin path. The value is stored verbatim; see the module
        docstring on normalization.
        """
        self.get(workspace_id)          # 404 before touching the sidecar
        if not (path or "").strip():
            raise InvalidRequest("path must not be empty", details={"field": "path"})
        self._repo.upsert_project_path(workspace_id, path, list(exclude or []))
        return self.get(workspace_id)

    def remove_path(self, workspace_id: str, path: str) -> Dict[str, Any]:
        self.get(workspace_id)
        if not (path or "").strip():
            raise InvalidRequest("path must not be empty", details={"field": "path"})
        self._repo.remove_project_path(workspace_id, path)
        return self.get(workspace_id)

    def paths(self, workspace_id: str) -> List[Dict[str, Any]]:
        self.get(workspace_id)
        return self._repo.get_project_paths(workspace_id)

    def source_root(self, workspace_id: str) -> str:
        """The machine-local absolute root the workspace's relative paths hang
        off, or '' when unset (data copied to a machine that has not pointed the
        root yet)."""
        return machine.project_root(workspace_id)

    # -- template selection -------------------------------------------------
    def template_selection(self, workspace_id: str) -> Dict[str, Any]:
        """What the user chose, what detection recorded, and which is effective."""
        return templates.selection_info(self.get(workspace_id))

    def set_template(self, workspace_id: str,
                     name: Optional[str]) -> Dict[str, Any]:
        """Set or clear the explicit template override.

        An empty/None name CLEARS the override and falls back to the recorded
        auto-selection. A name that does not resolve is rejected here rather
        than silently ignored — the caller said "use THIS one". Names are
        lower-cased, matching the existing REST behaviour.
        """
        record = self.get(workspace_id)
        clean = (name or "").strip().lower()
        if clean and not templates.get_template(clean):
            raise InvalidRequest(
                f"unknown or invalid template: {clean!r} "
                "(GET /templates lists what is available)",
                details={"template": clean,
                         "available": [t["name"] for t in templates.list_templates()]})
        meta = dict(record.get("meta") or {})
        if clean:
            meta["template"] = clean
        else:
            meta.pop("template", None)
        self._repo.update_project_meta(workspace_id, meta)
        return templates.selection_info(self.get(workspace_id))

    # -- status -------------------------------------------------------------
    def status(self, workspace_id: str) -> Dict[str, Any]:
        """Everything ``openmind status`` reports for one workspace.

        Counts come from the live stores. A store that cannot be opened reports
        ``None`` rather than 0 — a false zero would read as "nothing indexed"
        when the truth is "could not tell". ``doctor`` reports backend health
        separately, so the reason is never lost.
        """
        record = self.get(workspace_id)
        return {
            "workspace": record,
            "paths": self._repo.get_project_paths(workspace_id),
            "state": record.get("state"),
            "template": templates.selection_info(record),
            "counts": {
                "indexed_files": _count(
                    lambda: len(self._repo.get_file_index(workspace_id))),
                "code_chunks": _count(
                    lambda: vectorstore.get_code_store(workspace_id).count()),
                "solved_cases": _count(
                    lambda: vectorstore.get_cases_store(workspace_id).count()),
                "glossary_terms": _count(
                    lambda: len(mapio.load_glossary(workspace_id).get("terms") or {})),
            },
        }

    # -- lifecycle ----------------------------------------------------------
    def terminate(self, workspace_id: str,
                  clear_cases: bool = False) -> Dict[str, Any]:
        """Drop everything learned about the workspace, keeping the workspace
        itself. Synchronous: it stops running jobs first (bounded wait)."""
        self.get(workspace_id)
        return self._engine.terminate_project(workspace_id, clear_cases=clear_cases)

    def request_delete(self, workspace_id: str) -> Dict[str, Any]:
        """Tombstone the workspace and reclaim its storage in the background.

        Returns immediately. The workspace is treated as gone from this point;
        the row is dropped for real when cleanup finishes.
        """
        self.get(workspace_id)
        return self._engine.request_delete(workspace_id)


def _count(fn) -> Optional[int]:
    """A store count, or None when the store cannot be read.

    Broad by design and narrow in effect: the ONLY thing it hides is "this
    optional backend would not open", and it reports that as ``None`` (unknown)
    rather than 0 (empty). Every caller renders None distinctly.
    """
    try:
        return int(fn())
    except Exception:
        return None


__all__ = ["WorkspaceService"]
