"""Lazily-constructed, cached application services.

The container exists so every adapter shares one set of service instances
instead of each constructing its own. It is intentionally not a DI framework:
services are constructed by name, dependencies are wired explicitly in one
readable place, and the whole thing is about forty lines.

Construction is lazy because the surfaces need different subsets.
``openmind export`` must not touch the vector store or the job engine, and
``doctor`` must not spawn a worker thread. A container that eagerly built
everything would make those commands pay for services they never call.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..ports.runtime_ports import Clock
from .export_service import ExportService
from .health_service import HealthService
from .ingest_service import IngestService
from .job_service import JobService
from .workspace_service import WorkspaceService


class ServiceContainer:
    """Holds the application services for one runtime."""

    def __init__(self, ensure_worker: Optional[Callable[[], None]] = None,
                 clock: Optional[Clock] = None) -> None:
        self._ensure_worker = ensure_worker
        self._clock = clock
        self._workspaces: Optional[WorkspaceService] = None
        self._jobs: Optional[JobService] = None
        self._ingest: Optional[IngestService] = None
        self._export: Optional[ExportService] = None
        self._health: Optional[HealthService] = None

    @property
    def workspaces(self) -> WorkspaceService:
        if self._workspaces is None:
            self._workspaces = WorkspaceService()
        return self._workspaces

    @property
    def jobs(self) -> JobService:
        if self._jobs is None:
            self._jobs = JobService(clock=self._clock)
        return self._jobs

    @property
    def ingest(self) -> IngestService:
        if self._ingest is None:
            self._ingest = IngestService(self.workspaces, self.jobs,
                                         ensure_worker=self._ensure_worker)
        return self._ingest

    @property
    def export(self) -> ExportService:
        if self._export is None:
            self._export = ExportService()
        return self._export

    @property
    def health(self) -> HealthService:
        if self._health is None:
            self._health = HealthService()
        return self._health


__all__ = ["ServiceContainer"]
