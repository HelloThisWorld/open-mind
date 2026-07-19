"""Domain layer: application errors and the types that cross service
boundaries. Imports nothing from FastAPI, the CLI, or the persistence layer.
"""
from .errors import (AssetNotFound, ContentCorruption, DependencyUnavailable,
                     EvidenceNotFound, InvalidRequest, JobFailed, JobNotFound,
                     NotFound, OpenMindError, OperationTimeout, RevisionNotFound,
                     SegmentNotFound, WorkspaceNotFound)
from .types import (ACTIVE_JOB_STATUSES, SETTLED_JOB_STATUSES,
                    TERMINAL_JOB_STATUSES, Asset, AssetRevision, AssetState,
                    AssetType, ContentMode, Evidence, HealthCheck, HealthReport,
                    JobWaitResult, RevisionStatus, Segment, SegmentType,
                    SourceLocator, STATUS_ERROR, STATUS_OK, STATUS_WARN)

__all__ = [
    "AssetNotFound", "ContentCorruption", "DependencyUnavailable",
    "EvidenceNotFound", "InvalidRequest", "JobFailed", "JobNotFound",
    "NotFound", "OpenMindError", "OperationTimeout", "RevisionNotFound",
    "SegmentNotFound", "WorkspaceNotFound",
    "ACTIVE_JOB_STATUSES", "SETTLED_JOB_STATUSES", "TERMINAL_JOB_STATUSES",
    "HealthCheck", "HealthReport", "JobWaitResult",
    "STATUS_ERROR", "STATUS_OK", "STATUS_WARN",
    "Asset", "AssetRevision", "AssetState", "AssetType", "ContentMode",
    "Evidence", "RevisionStatus", "Segment", "SegmentType", "SourceLocator",
]
