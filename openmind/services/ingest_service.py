"""Ingest use cases.

Thin on purpose. The ingestion ALGORITHM is not rewritten in this phase — the
incremental walk, per-file hashing, semantic chunking, glossary and structure
builds, embedding batching and file-boundary checkpoints all stay exactly where
they are, in the persistent job engine. This service only owns the use-case
shape around them: validate the workspace, resolve an optional path filter,
enqueue, and optionally wait.

The one thing it does add is the requirement that a worker actually exists.
Before this phase, ``jobs.start_worker()`` was called only from the FastAPI
lifespan, so an ingest enqueued from any other process sat ``queued`` forever.
The runtime now starts the worker on demand, and ``start(wait=True)`` asks for
it explicitly.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .. import machine
from ..domain.errors import InvalidRequest
from ..domain.types import JobWaitResult
from .job_service import DEFAULT_TIMEOUT, JobService
from .workspace_service import WorkspaceService


class IngestService:
    """Start and observe ingestion for a workspace."""

    def __init__(self, workspaces: WorkspaceService, jobs: JobService,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        # Supplied by the runtime. Without it a --wait ingest would block on a
        # job no worker will ever pick up.
        self._ensure_worker = ensure_worker

    def start(self, workspace_id: str, path: Optional[str] = None,
              wait: bool = False,
              timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """Enqueue an ingest, optionally waiting for it to finish.

        *path* restricts the ingest to a subtree of the workspace's registered
        source. It is stored relative to the workspace root by the engine, so an
        absolute machine path never reaches the portable database.

        Without *wait* this returns as soon as the job row is persisted, so the
        caller gets a durable job id it can poll later.
        """
        workspace = self._workspaces.get(workspace_id)
        if not self._workspaces.paths(workspace_id):
            raise InvalidRequest(
                "workspace has no registered source path; add one before "
                "ingesting",
                details={"workspace_id": workspace_id})

        if path is not None and not str(path).strip():
            raise InvalidRequest("path filter must not be blank",
                                 details={"field": "path"})

        if wait and self._ensure_worker is not None:
            self._ensure_worker()

        job = self._jobs.enqueue_ingest(workspace_id, path)
        result: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "workspace": workspace.get("name"),
            "job_id": job["job_id"],
            "job": job,
            "waited": False,
        }
        if not wait:
            return result

        outcome: JobWaitResult = self._jobs.wait_for_terminal(
            job["job_id"], timeout=timeout)
        result.update({
            "waited": True,
            "job": outcome.job,
            "status": outcome.status,
            "completed": outcome.completed,
            "waited_seconds": round(outcome.waited_seconds, 3),
        })
        return result

    def status(self, workspace_id: str) -> Dict[str, Any]:
        """Ingest state for a workspace: its own state plus its ingest jobs,
        newest first."""
        workspace = self._workspaces.get(workspace_id)
        ingest_jobs = [j for j in self._jobs.list([workspace_id])
                       if j.get("type") == "ingest"]
        active = [j for j in ingest_jobs
                  if j.get("status") in ("queued", "running")]
        return {
            "workspace_id": workspace_id,
            "state": workspace.get("state"),
            "source_root": self._workspaces.source_root(workspace_id),
            "active_job": active[0] if active else None,
            "jobs": ingest_jobs,
        }

    def relative_path(self, workspace_id: str, path: str) -> str:
        """A workspace-relative form of *path*, for callers that hold an
        absolute machine path and must not leak it into stored state."""
        return machine.to_rel(workspace_id, path)


__all__ = ["IngestService"]
