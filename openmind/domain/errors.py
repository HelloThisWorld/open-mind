"""Typed application errors.

Services raise these instead of ``HTTPException`` (which would drag FastAPI into
the application layer) or bare ``ValueError`` (which an adapter cannot map
without string-matching). Each adapter translates them on its own terms:

    error                    HTTP    CLI exit
    ---------------------    ----    --------
    WorkspaceNotFound        404     1
    JobNotFound              404     1
    AssetNotFound            404     1
    RevisionNotFound         404     1
    SegmentNotFound          404     1
    EvidenceNotFound         404     1
    InvalidRequest           400     2
    DependencyUnavailable    503     3
    JobFailed               (n/a)    4
    OperationTimeout        (n/a)    5
    ContentCorruption        500     4

Every error carries a machine-readable ``code`` and an optional ``details``
dict, so ``--json`` output stays parseable and callers never have to parse
prose.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class OpenMindError(Exception):
    """Base class for every application-layer error."""

    code = "openmind_error"
    exit_code = 1
    http_status = 500

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class NotFound(OpenMindError):
    """A requested entity does not exist. Honest 'not found', never an empty
    stand-in that reads like a real record."""

    code = "not_found"
    exit_code = 1
    http_status = 404


class WorkspaceNotFound(NotFound):
    code = "workspace_not_found"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace not found: {workspace_id!r}",
                         details={"workspace_id": workspace_id})
        self.workspace_id = workspace_id


class JobNotFound(NotFound):
    code = "job_not_found"

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job not found: {job_id!r}", details={"job_id": job_id})
        self.job_id = job_id


class AssetNotFound(NotFound):
    code = "asset_not_found"

    def __init__(self, asset_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"asset_id": asset_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"asset not found: {asset_id!r}", details=details)
        self.asset_id = asset_id
        self.workspace_id = workspace_id


class RevisionNotFound(NotFound):
    code = "revision_not_found"

    def __init__(self, revision_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"revision_id": revision_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"revision not found: {revision_id!r}", details=details)
        self.revision_id = revision_id
        self.workspace_id = workspace_id


class SegmentNotFound(NotFound):
    code = "segment_not_found"

    def __init__(self, segment_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"segment_id": segment_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"segment not found: {segment_id!r}", details=details)
        self.segment_id = segment_id
        self.workspace_id = workspace_id


class EvidenceNotFound(NotFound):
    code = "evidence_not_found"

    def __init__(self, evidence_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"evidence_id": evidence_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"evidence not found: {evidence_id!r}", details=details)
        self.evidence_id = evidence_id
        self.workspace_id = workspace_id


class ContentCorruption(OpenMindError):
    """A stored content blob failed its SHA-256 integrity check on read, or an
    evidence range no longer resolves against the immutable snapshot. The bytes
    are never returned silently-wrong; the failure is explicit."""

    code = "content_corruption"
    exit_code = 4
    http_status = 500

    def __init__(self, message: str, *, blob_hash: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if blob_hash:
            merged.setdefault("blob_hash", blob_hash)
        super().__init__(message, details=merged)
        self.blob_hash = blob_hash


class InvalidRequest(OpenMindError):
    """Caller-supplied arguments or configuration are unusable."""

    code = "invalid_request"
    exit_code = 2
    http_status = 400


class DependencyUnavailable(OpenMindError):
    """A required runtime dependency or backend is missing (the ``mcp`` package,
    a vector-store backend, ``uvicorn``)."""

    code = "dependency_unavailable"
    exit_code = 3
    http_status = 503

    def __init__(self, message: str, *, dependency: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if dependency:
            merged.setdefault("dependency", dependency)
        super().__init__(message, details=merged)
        self.dependency = dependency


class JobFailed(OpenMindError):
    """A job reached a terminal state that is not success, or stopped without
    completing (``paused``/``interrupted`` are not progressing)."""

    code = "job_failed"
    exit_code = 4
    http_status = 500

    def __init__(self, message: str, *, job_id: str = "", status: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if job_id:
            merged.setdefault("job_id", job_id)
        if status:
            merged.setdefault("status", status)
        super().__init__(message, details=merged)
        self.job_id = job_id
        self.status = status


class OperationTimeout(OpenMindError):
    """A bounded wait expired. The underlying work is NOT cancelled — the job
    keeps running and can still be polled."""

    code = "timeout"
    exit_code = 5
    http_status = 504

    def __init__(self, message: str, *, timeout: Optional[float] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if timeout is not None:
            merged.setdefault("timeout_seconds", timeout)
        super().__init__(message, details=merged)
        self.timeout = timeout


__all__ = [
    "OpenMindError", "NotFound", "WorkspaceNotFound", "JobNotFound",
    "AssetNotFound", "RevisionNotFound", "SegmentNotFound", "EvidenceNotFound",
    "ContentCorruption",
    "InvalidRequest", "DependencyUnavailable", "JobFailed", "OperationTimeout",
]
