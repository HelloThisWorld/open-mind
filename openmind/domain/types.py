"""Types that cross the service boundary.

Services return plain dictionaries for records that already have a stable,
externally-visible shape (project rows, job rows) — those shapes are part of the
REST contract, and wrapping them in dataclasses would mean maintaining two
definitions of the same thing and converting on every call.

Dataclasses are used where this phase introduces a NEW shape that has no
existing contract to honour: health checks, the health report, and the
terminal-wait outcome.

VOCABULARY
----------
"Workspace" is internal vocabulary only. The stored entity is still a project,
the REST API still says ``/projects``, and ``workspace_id`` is the existing
``p_*`` project id. A later v2 phase may introduce a real Asset/Workspace model;
this phase does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# Job statuses, exactly as jobs.py writes them.
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_INTERRUPTED = "interrupted"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"

#: Statuses a job never leaves on its own.
TERMINAL_JOB_STATUSES = frozenset({JOB_STATUS_DONE, JOB_STATUS_FAILED})

#: Not terminal, but not progressing either: the worker will not advance these
#: without an explicit resume. A bounded wait must return rather than block.
SETTLED_JOB_STATUSES = frozenset({JOB_STATUS_PAUSED, JOB_STATUS_INTERRUPTED})

#: Statuses that mean the job is still moving.
ACTIVE_JOB_STATUSES = frozenset({JOB_STATUS_QUEUED, JOB_STATUS_RUNNING})

#: Health severities, ordered worst-first.
STATUS_ERROR = "error"
STATUS_WARN = "warn"
STATUS_OK = "ok"
_SEVERITY = {STATUS_ERROR: 0, STATUS_WARN: 1, STATUS_OK: 2}


@dataclass
class HealthCheck:
    """One diagnostic. ``status`` is ok / warn / error.

    ``warn`` is for a degraded-but-usable condition — a missing optional local
    model, an embedding fallback. It must NOT fail ``doctor``: only ``error``
    does, so an absent optional model never breaks diagnostics for someone who
    never asked for a model-dependent operation.
    """

    name: str
    status: str
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != STATUS_ERROR

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.data:
            out["data"] = dict(self.data)
        return out


@dataclass
class HealthReport:
    """The aggregate ``doctor`` result."""

    version: str
    checks: List[HealthCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Worst severity across all checks."""
        if not self.checks:
            return STATUS_OK
        return min((c.status for c in self.checks),
                   key=lambda s: _SEVERITY.get(s, 0))

    @property
    def ok(self) -> bool:
        """True unless some check is a hard error."""
        return all(c.ok for c in self.checks)

    def failures(self) -> List[HealthCheck]:
        return [c for c in self.checks if c.status == STATUS_ERROR]

    def warnings(self) -> List[HealthCheck]:
        return [c for c in self.checks if c.status == STATUS_WARN]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
        }


@dataclass
class JobWaitResult:
    """The outcome of a bounded wait on a job.

    ``completed`` means the job reached ``done``. A ``failed``, ``paused`` or
    ``interrupted`` job returns with ``completed=False`` and the real status —
    the waiter never pretends a stopped job finished, and never blocks forever
    on one that will not progress without a resume.
    """

    job: Dict[str, Any]
    status: str
    completed: bool
    waited_seconds: float
    timed_out: bool = False

    @property
    def job_id(self) -> str:
        return str(self.job.get("job_id", ""))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "completed": self.completed,
            "timed_out": self.timed_out,
            "waited_seconds": round(self.waited_seconds, 3),
            "job": self.job,
        }


__all__ = [
    "JOB_STATUS_QUEUED", "JOB_STATUS_RUNNING", "JOB_STATUS_PAUSED",
    "JOB_STATUS_INTERRUPTED", "JOB_STATUS_DONE", "JOB_STATUS_FAILED",
    "TERMINAL_JOB_STATUSES", "SETTLED_JOB_STATUSES", "ACTIVE_JOB_STATUSES",
    "STATUS_OK", "STATUS_WARN", "STATUS_ERROR",
    "HealthCheck", "HealthReport", "JobWaitResult",
]
