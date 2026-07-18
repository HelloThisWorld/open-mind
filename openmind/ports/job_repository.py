"""The job surfaces :class:`~openmind.services.job_service.JobService` and
:class:`~openmind.services.ingest_service.IngestService` depend on.

Two ports, because the existing code really is split in two and pretending
otherwise would hide it:

* :class:`JobReader` — job *state*, owned by :mod:`openmind.db`. There is no
  public ``get_job``/``list_jobs`` in :mod:`openmind.jobs`; callers read the
  database directly.
* :class:`JobEngine` — job *lifecycle*, owned by :mod:`openmind.jobs`.

Both are :class:`typing.Protocol` definitions that the existing modules satisfy
structurally. The test seam is the point: a fake :class:`JobReader` drives the
bounded-wait state machine through queued -> running -> done, or straight to
``paused``, in milliseconds and without a worker thread.

The job engine itself is NOT replaced in this phase. These ports are the
boundary a later phase would implement with a typed worker pool or a job DAG.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class JobReader(Protocol):
    """Persisted job state. Satisfied by :mod:`openmind.db`.

    Implementations must read through to storage on every call. Job state is
    written from the worker thread, request threads and delete-cleanup threads;
    a caching reader would break the reconnect/resume story that depends on the
    database being the single source of truth.
    """

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def list_jobs(self, project_ids: Optional[List[str]] = None,
                  status: Optional[str] = None) -> List[Dict[str, Any]]: ...

    def active_jobs(self) -> List[Dict[str, Any]]: ...


@runtime_checkable
class JobEngine(Protocol):
    """Job lifecycle control. Satisfied by :mod:`openmind.jobs`.

    ``pause``, ``resume`` and ``cancel_job`` raise :class:`KeyError` for an
    unknown job id; :class:`~openmind.services.job_service.JobService`
    translates that into
    :class:`~openmind.domain.errors.JobNotFound`.
    """

    def start_worker(self) -> None: ...

    def enqueue_ingest(self, project_id: str,
                       path: Optional[str] = None) -> Dict[str, Any]: ...

    def enqueue_gendocs(self, project_id: str) -> Dict[str, Any]: ...

    def pause(self, job_id: str) -> Dict[str, Any]: ...

    def resume(self, job_id: str) -> Dict[str, Any]: ...

    def cancel_job(self, job_id: str) -> Dict[str, Any]: ...

    def terminate_project(self, project_id: str,
                          clear_cases: bool = False) -> Dict[str, Any]: ...

    def request_delete(self, project_id: str) -> Dict[str, Any]: ...


__all__ = ["JobReader", "JobEngine"]
