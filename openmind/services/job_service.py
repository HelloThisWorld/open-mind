"""Job use cases — enqueue, inspect, control, and bounded waiting.

This wraps the EXISTING single-worker job engine; it does not replace it. Phase
1 explicitly preserves persistent job state, resumable interrupted jobs, pause
and resume, cancellation, file-boundary checkpoints, asynchronous deletion,
restart recovery and SSE compatibility. The value added here is a stable
surface that the CLI, MCP and tests can drive, plus a bounded wait that does not
exist anywhere today.

The reads deliberately go through the job *reader* (the database) rather than
through :mod:`openmind.jobs`, because that is where job state actually lives —
there is no public ``get_job`` in the engine module.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import db as db_module
from .. import jobs as jobs_module
from ..domain.errors import InvalidRequest, JobNotFound, OperationTimeout
from ..domain.types import (ACTIVE_JOB_STATUSES, SETTLED_JOB_STATUSES,
                            TERMINAL_JOB_STATUSES, JOB_STATUS_DONE,
                            JobWaitResult)
from ..ports.job_repository import JobEngine, JobReader
from ..ports.runtime_ports import Clock, SystemClock

#: How often a bounded wait re-reads persisted job state. Job progress is
#: written on every tick, and the SSE stream polls at 0.8s; matching that keeps
#: CLI latency comparable to the UI without adding database pressure.
DEFAULT_POLL_INTERVAL = 0.5

#: Default ceiling for ``wait_for_terminal``. Generous, because a first ingest
#: of a large repository legitimately takes a long time; callers that want a
#: tighter bound pass one.
DEFAULT_TIMEOUT = 3600.0


class JobService:
    """Use cases over the persistent job engine."""

    def __init__(self, reader: Optional[JobReader] = None,
                 engine: Optional[JobEngine] = None,
                 clock: Optional[Clock] = None) -> None:
        self._reader: Any = reader if reader is not None else db_module
        self._engine: Any = engine if engine is not None else jobs_module
        self._clock: Clock = clock if clock is not None else SystemClock()

    # -- enqueue ------------------------------------------------------------
    def enqueue_ingest(self, workspace_id: str,
                       path: Optional[str] = None) -> Dict[str, Any]:
        """Queue an ingest. The engine dedupes: an ingest already queued or
        running for this workspace is returned instead of a second one."""
        return self._engine.enqueue_ingest(workspace_id, path)

    def enqueue_gendocs(self, workspace_id: str) -> Dict[str, Any]:
        """Queue a generated-documentation build."""
        return self._engine.enqueue_gendocs(workspace_id)

    # -- reads --------------------------------------------------------------
    def get(self, job_id: str) -> Dict[str, Any]:
        job = self._reader.get_job(job_id)
        if not job:
            raise JobNotFound(job_id)
        return job

    def list(self, workspace_ids: Optional[List[str]] = None,
             status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._reader.list_jobs(workspace_ids, status)

    def active(self) -> List[Dict[str, Any]]:
        """Every queued or running job, across all workspaces."""
        return self._reader.active_jobs()

    # -- control ------------------------------------------------------------
    # The engine raises KeyError for an unknown job id; translate it once here
    # so every adapter gets a typed error instead of re-deriving the mapping.
    def pause(self, job_id: str) -> Dict[str, Any]:
        try:
            return self._engine.pause(job_id)
        except KeyError:
            raise JobNotFound(job_id) from None

    def resume(self, job_id: str) -> Dict[str, Any]:
        try:
            return self._engine.resume(job_id)
        except KeyError:
            raise JobNotFound(job_id) from None

    def cancel(self, job_id: str) -> Dict[str, Any]:
        try:
            return self._engine.cancel_job(job_id)
        except KeyError:
            raise JobNotFound(job_id) from None

    # -- bounded waiting ----------------------------------------------------
    def wait_for_terminal(self, job_id: str,
                          timeout: float = DEFAULT_TIMEOUT,
                          poll_interval: float = DEFAULT_POLL_INTERVAL
                          ) -> JobWaitResult:
        """Poll persisted job state until the job settles or *timeout* expires.

        Returns when the job is:

        * ``done``   -> ``completed=True``
        * ``failed`` -> ``completed=False`` (terminal, unsuccessful)
        * ``paused`` / ``interrupted`` -> ``completed=False``

        The last case matters: those are NOT terminal, but the worker will not
        advance them without an explicit resume, so blocking until timeout
        would be a hang, not a wait. The caller gets the real status and can
        decide.

        Never caches: every poll re-reads the row, because job state is written
        by the worker, request threads and cleanup threads alike.

        On timeout this raises :class:`OperationTimeout` — the job is NOT
        cancelled and keeps running; it can still be polled or waited on again.
        """
        if timeout <= 0:
            raise InvalidRequest("timeout must be greater than zero",
                                 details={"timeout": timeout})
        if poll_interval <= 0:
            raise InvalidRequest("poll interval must be greater than zero",
                                 details={"poll_interval": poll_interval})

        started = self._clock.monotonic()
        # Check before sleeping: an already-finished job must return instantly.
        while True:
            job = self.get(job_id)
            status = str(job.get("status") or "")
            if status in TERMINAL_JOB_STATUSES or status in SETTLED_JOB_STATUSES:
                return JobWaitResult(
                    job=job, status=status,
                    completed=(status == JOB_STATUS_DONE),
                    waited_seconds=self._clock.monotonic() - started)

            elapsed = self._clock.monotonic() - started
            if elapsed >= timeout:
                raise OperationTimeout(
                    f"job {job_id} did not finish within {timeout:g}s "
                    f"(last status: {status or 'unknown'}); it is still running "
                    f"and can be polled again",
                    timeout=timeout,
                    details={"job_id": job_id, "status": status,
                             "waited_seconds": round(elapsed, 3)})
            self._clock.sleep(min(poll_interval, max(0.0, timeout - elapsed)))

    def is_active(self, job_id: str) -> bool:
        return str(self.get(job_id).get("status") or "") in ACTIVE_JOB_STATUSES


__all__ = ["JobService", "DEFAULT_POLL_INTERVAL", "DEFAULT_TIMEOUT"]
