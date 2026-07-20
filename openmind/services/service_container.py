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
from .asset_service import AssetService
from .document_service import DocumentService
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
        self._assets: Optional[AssetService] = None
        self._documents: Optional[DocumentService] = None
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
    def assets(self) -> AssetService:
        # Read-only Asset queries plus single-file sync. It reuses the ingest
        # service for sync_file; constructing it never starts a worker (only a
        # waited sync does), so the read-only MCP asset tools stay side-effect free.
        if self._assets is None:
            self._assets = AssetService(self.workspaces, self.ingest)
        return self._assets

    @property
    def documents(self) -> DocumentService:
        # Document append + read-only document queries (v2 Phase 3). Like the
        # asset service, constructing it never starts a worker — only a waited
        # import does — so the read-only MCP document tools stay side-effect free.
        if self._documents is None:
            self._documents = DocumentService(
                self.workspaces, self.jobs, self.assets,
                ensure_worker=self._ensure_worker)
        return self._documents

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
