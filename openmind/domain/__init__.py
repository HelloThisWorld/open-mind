"""Domain layer: application errors and the types that cross service
boundaries. Imports nothing from FastAPI, the CLI, or the persistence layer.
"""
from .errors import (AssetNotFound, ContentCorruption, DependencyUnavailable,
                     EvidenceNotFound, InvalidRequest, JobFailed, JobNotFound,
                     NotFound, OpenMindError, OperationTimeout, RevisionNotFound,
                     SegmentNotFound, WorkspaceNotFound)
from .types import (ACTIVE_JOB_STATUSES, CANDIDATE_STATUS,
                    DOCUMENT_LOCATOR_KINDS, SETTLED_JOB_STATUSES,
                    TERMINAL_JOB_STATUSES, Asset, AssetRevision, AssetState,
                    AssetType, CandidateConfidence, CandidateType, ContentMode,
                    DocumentBlockType, DocumentParse, DocumentParseStatus,
                    Evidence, HealthCheck, HealthReport, ImportStatus,
                    JobWaitResult, RevisionStatus, Segment, SegmentType,
                    SourceKind, SourceLocator, STATUS_ERROR, STATUS_OK,
                    STATUS_WARN, is_document_locator, locator_document_key)

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
    # -- v2 Phase 3: document ingestion -------------------------------------
    "CANDIDATE_STATUS", "DOCUMENT_LOCATOR_KINDS", "CandidateConfidence",
    "CandidateType", "DocumentBlockType", "DocumentParse",
    "DocumentParseStatus", "ImportStatus", "SourceKind", "is_document_locator",
    "locator_document_key",
]
