"""Ports: the narrow boundaries the application services depend on.

Only three exist, and each is justified by a real test seam that is exercised
by ``tests/verify_services.py``:

* :class:`~openmind.ports.workspace_repository.WorkspaceRepository` — satisfied
  by :mod:`openmind.db`;
* :class:`~openmind.ports.job_repository.JobReader` / :class:`JobEngine` —
  satisfied by :mod:`openmind.db` and :mod:`openmind.jobs`;
* :class:`~openmind.ports.runtime_ports.Clock` — real vs. fake, so bounded
  waits are tested in milliseconds.

Deliberately absent: ports over configuration, the filesystem, the vector store
and the deterministic extractors. Those have one implementation and an existing
env-var test seam; a Protocol over them would be indirection without coverage.
"""
from .job_repository import JobEngine, JobReader
from .runtime_ports import Clock, FakeClock, SystemClock
from .workspace_repository import WorkspaceRepository

__all__ = ["JobEngine", "JobReader", "Clock", "FakeClock", "SystemClock",
           "WorkspaceRepository"]
