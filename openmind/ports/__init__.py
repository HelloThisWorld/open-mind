"""Ports: the narrow boundaries the application services depend on.

Each is justified by a real test seam exercised by ``tests/verify_services.py``
or ``tests/verify_asset_model.py``:

* :class:`~openmind.ports.workspace_repository.WorkspaceRepository` — satisfied
  by :mod:`openmind.db`;
* :class:`~openmind.ports.asset_repository.AssetRepository` — satisfied by
  :mod:`openmind.db`;
* :class:`~openmind.ports.job_repository.JobReader` / :class:`JobEngine` —
  satisfied by :mod:`openmind.db` and :mod:`openmind.jobs`;
* :class:`~openmind.ports.runtime_ports.Clock` — real vs. fake, so bounded
  waits are tested in milliseconds.

Deliberately absent: ports over configuration, the filesystem, the vector store
and the deterministic extractors. Those have one implementation and an existing
env-var test seam; a Protocol over them would be indirection without coverage.
"""
from .asset_repository import AssetRepository
from .job_repository import JobEngine, JobReader
from .runtime_ports import Clock, FakeClock, SystemClock
from .workspace_repository import WorkspaceRepository

__all__ = ["AssetRepository", "JobEngine", "JobReader", "Clock", "FakeClock",
           "SystemClock", "WorkspaceRepository"]
