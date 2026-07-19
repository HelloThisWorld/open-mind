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

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Canonical Asset model vocabulary (OpenMind v2 Phase 2)
#
# Each vocabulary is a small class of string constants plus a ``VALUES``
# frozenset, so a value can be validated at a write boundary without an enum's
# instance/serialization ceremony. The vocabularies are forward-compatible:
# ``coerce`` maps an unknown value to a safe default rather than raising, so a
# database written by a newer OpenMind that added a member still reads back.
# ---------------------------------------------------------------------------
class AssetState:
    """Lifecycle state of an Asset."""
    ACTIVE = "active"
    REMOVED = "removed"
    UNSUPPORTED = "unsupported"
    VALUES = frozenset({ACTIVE, REMOVED, UNSUPPORTED})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.ACTIVE


class AssetType:
    """Deterministic, extension/path-based classification. Never a model."""
    SOURCE_CODE = "source-code"
    CONFIGURATION = "configuration"
    DATABASE_SCHEMA = "database-schema"
    DOCUMENTATION_TEXT = "documentation-text"
    TEST_SOURCE = "test-source"
    BUILD_DEFINITION = "build-definition"
    UNKNOWN = "unknown"
    VALUES = frozenset({SOURCE_CODE, CONFIGURATION, DATABASE_SCHEMA,
                        DOCUMENTATION_TEXT, TEST_SOURCE, BUILD_DEFINITION, UNKNOWN})

    #: Exact (lower-cased) filenames that are build definitions regardless of
    #: extension (pom.xml is .xml but a build file; package.json is .json).
    _BUILD_FILENAMES = frozenset({
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
        "settings.gradle.kts", "build.xml", "package.json", "package-lock.json",
        "makefile", "cmakelists.txt", "dockerfile", "requirements.txt",
        "setup.py", "setup.cfg", "pyproject.toml", "go.mod", "go.sum",
        "cargo.toml", "cargo.lock", "gemfile", "build.sbt",
    })
    _BUILD_EXTS = frozenset({".gradle"})
    _SCHEMA_EXTS = frozenset({".sql", ".ddl"})
    _CODE_EXTS = frozenset({
        ".java", ".kt", ".kts", ".groovy", ".scala", ".ts", ".tsx", ".js",
        ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".py", ".rb", ".cs",
        ".php", ".rs", ".html", ".htm", ".css", ".scss", ".less",
    })
    _CONFIG_EXTS = frozenset({
        ".properties", ".yml", ".yaml", ".xml", ".json", ".toml", ".ini",
        ".conf", ".cfg", ".env", ".config",
    })
    _DOC_EXTS = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt"})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.UNKNOWN

    @classmethod
    def _is_test(cls, logical_key: str, base: str) -> bool:
        parts = {p.lower() for p in logical_key.replace("\\", "/").split("/")}
        if parts & {"test", "tests", "__tests__", "testing"}:
            return True
        b = base.lower()
        return (
            b.endswith(("test.java", "tests.java", "it.java", "itcase.java",
                        ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                        ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
                        "_test.py", "_test.go", "test.cs", "tests.cs",
                        "_test.rb", "_spec.rb"))
            or b.startswith("test_") and b.endswith(".py"))

    @classmethod
    def classify(cls, logical_key: str) -> str:
        """Map a workspace-relative path to a deterministic asset type."""
        key = (logical_key or "").replace("\\", "/")
        base = key.rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1].lower()
        if base.lower() in cls._BUILD_FILENAMES or ext in cls._BUILD_EXTS:
            return cls.BUILD_DEFINITION
        if ext in cls._SCHEMA_EXTS:
            return cls.DATABASE_SCHEMA
        if ext in cls._CODE_EXTS:
            return cls.TEST_SOURCE if cls._is_test(key, base) else cls.SOURCE_CODE
        if ext in cls._CONFIG_EXTS:
            return cls.CONFIGURATION
        if ext in cls._DOC_EXTS:
            return cls.DOCUMENTATION_TEXT
        return cls.UNKNOWN


class RevisionStatus:
    """Lifecycle status of a revision. Phase 2 code revisions are ``unknown``;
    approval authority is never inferred."""
    UNKNOWN = "unknown"
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    EFFECTIVE = "effective"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"
    VALUES = frozenset({UNKNOWN, DRAFT, REVIEWED, APPROVED, EFFECTIVE,
                        SUPERSEDED, WITHDRAWN, ARCHIVED})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.UNKNOWN


class SegmentType:
    """Structural unit kind inside one revision."""
    TYPE = "type"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    FILE = "file"
    VALUES = frozenset({TYPE, METHOD, CONSTRUCTOR, FILE})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.FILE


class ContentMode:
    """Whether a segment's represented content is verbatim source or derived."""
    VERBATIM = "verbatim"
    DERIVED = "derived"
    VALUES = frozenset({VERBATIM, DERIVED})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.VERBATIM


@dataclass
class SourceLocator:
    """Where a piece of Evidence lives in the workspace source. ``file`` is
    always workspace-relative; line numbers are 1-based."""
    file: str
    start_line: int
    end_line: int
    symbol: str = ""
    kind: str = "source-range"

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "file": self.file,
                "startLine": self.start_line, "endLine": self.end_line,
                "symbol": self.symbol}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceLocator":
        return cls(file=str(data.get("file", "")),
                   start_line=int(data.get("startLine", 0) or 0),
                   end_line=int(data.get("endLine", 0) or 0),
                   symbol=str(data.get("symbol", "") or ""),
                   kind=str(data.get("kind", "source-range") or "source-range"))


@dataclass
class Asset:
    """One logical engineering object (in Phase 2, a source/config file)."""
    id: str
    workspace_id: str
    logical_key: str
    asset_type: str
    title: str
    source_kind: str = "file"
    source_path: str = ""
    media_type: str = ""
    state: str = AssetState.ACTIVE
    current_revision_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "workspace_id": self.workspace_id,
            "logical_key": self.logical_key, "asset_type": self.asset_type,
            "title": self.title, "source_kind": self.source_kind,
            "source_path": self.source_path, "media_type": self.media_type,
            "state": self.state, "current_revision_id": self.current_revision_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Asset":
        return cls(
            id=row["id"], workspace_id=row["workspace_id"],
            logical_key=row["logical_key"], asset_type=row["asset_type"],
            title=row["title"], source_kind=row.get("source_kind", "file"),
            source_path=row.get("source_path", ""),
            media_type=row.get("media_type", ""),
            state=row.get("state", AssetState.ACTIVE),
            current_revision_id=row.get("current_revision_id"),
            metadata=dict(row.get("metadata") or {}),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""))


@dataclass
class AssetRevision:
    """An immutable observation of an Asset's contents."""
    id: str
    asset_id: str
    sequence: int
    content_hash: str
    content_size: int
    content_blob_hash: str
    status: str = RevisionStatus.UNKNOWN
    version_label: str = ""
    source_commit: str = ""
    supersedes_revision_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "asset_id": self.asset_id, "sequence": self.sequence,
            "content_hash": self.content_hash, "content_size": self.content_size,
            "content_blob_hash": self.content_blob_hash, "status": self.status,
            "version_label": self.version_label, "source_commit": self.source_commit,
            "supersedes_revision_id": self.supersedes_revision_id,
            "metadata": dict(self.metadata), "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AssetRevision":
        return cls(
            id=row["id"], asset_id=row["asset_id"], sequence=int(row["sequence"]),
            content_hash=row["content_hash"], content_size=int(row["content_size"]),
            content_blob_hash=row["content_blob_hash"],
            status=row.get("status", RevisionStatus.UNKNOWN),
            version_label=row.get("version_label", ""),
            source_commit=row.get("source_commit", ""),
            supersedes_revision_id=row.get("supersedes_revision_id"),
            metadata=dict(row.get("metadata") or {}),
            created_at=row.get("created_at", ""))


@dataclass
class Segment:
    """A stable structural unit inside one revision."""
    id: str
    revision_id: str
    segment_key: str
    segment_type: str
    ordinal: int
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    symbol: str = ""
    content_hash: str = ""
    content_mode: str = ContentMode.VERBATIM
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "revision_id": self.revision_id,
            "segment_key": self.segment_key, "segment_type": self.segment_type,
            "ordinal": self.ordinal, "start_line": self.start_line,
            "end_line": self.end_line, "symbol": self.symbol,
            "content_hash": self.content_hash, "content_mode": self.content_mode,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Segment":
        return cls(
            id=row["id"], revision_id=row["revision_id"],
            segment_key=row["segment_key"], segment_type=row["segment_type"],
            ordinal=int(row["ordinal"]), start_line=row.get("start_line"),
            end_line=row.get("end_line"), symbol=row.get("symbol", ""),
            content_hash=row.get("content_hash", ""),
            content_mode=row.get("content_mode", ContentMode.VERBATIM),
            metadata=dict(row.get("metadata") or {}))


@dataclass
class Evidence:
    """A source-locatable citation for a segment, recoverable from the
    immutable revision blob."""
    id: str
    revision_id: str
    segment_id: Optional[str]
    locator: Dict[str, Any]
    excerpt: str = ""
    content_hash: str = ""
    created_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "revision_id": self.revision_id,
            "segment_id": self.segment_id, "locator": dict(self.locator),
            "excerpt": self.excerpt, "content_hash": self.content_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Evidence":
        return cls(
            id=row["id"], revision_id=row["revision_id"],
            segment_id=row.get("segment_id"),
            locator=dict(row.get("locator") or {}),
            excerpt=row.get("excerpt", ""), content_hash=row.get("content_hash", ""),
            created_at=row.get("created_at", ""))


__all__ = [
    "JOB_STATUS_QUEUED", "JOB_STATUS_RUNNING", "JOB_STATUS_PAUSED",
    "JOB_STATUS_INTERRUPTED", "JOB_STATUS_DONE", "JOB_STATUS_FAILED",
    "TERMINAL_JOB_STATUSES", "SETTLED_JOB_STATUSES", "ACTIVE_JOB_STATUSES",
    "STATUS_OK", "STATUS_WARN", "STATUS_ERROR",
    "HealthCheck", "HealthReport", "JobWaitResult",
    "AssetState", "AssetType", "RevisionStatus", "SegmentType", "ContentMode",
    "SourceLocator", "Asset", "AssetRevision", "Segment", "Evidence",
]
