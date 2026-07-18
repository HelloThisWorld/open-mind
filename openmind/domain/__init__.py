"""Domain layer: application errors and the types that cross service
boundaries. Imports nothing from FastAPI, the CLI, or the persistence layer.
"""
from .errors import (DependencyUnavailable, InvalidRequest, JobFailed,
                     JobNotFound, NotFound, OpenMindError, OperationTimeout,
                     WorkspaceNotFound)
from .types import (ACTIVE_JOB_STATUSES, SETTLED_JOB_STATUSES,
                    TERMINAL_JOB_STATUSES, HealthCheck, HealthReport,
                    JobWaitResult, STATUS_ERROR, STATUS_OK, STATUS_WARN)

__all__ = [
    "DependencyUnavailable", "InvalidRequest", "JobFailed", "JobNotFound",
    "NotFound", "OpenMindError", "OperationTimeout", "WorkspaceNotFound",
    "ACTIVE_JOB_STATUSES", "SETTLED_JOB_STATUSES", "TERMINAL_JOB_STATUSES",
    "HealthCheck", "HealthReport", "JobWaitResult",
    "STATUS_ERROR", "STATUS_OK", "STATUS_WARN",
]
