"""Closed vocabularies of the traceability and conflict engine.

Same convention as :mod:`openmind.knowledge.vocabularies`: small classes of
string constants with a ``VALUES`` frozenset, validated at write boundaries
with :func:`require` — an unknown member is a typed failure, never a silent
default. Trace stages, path kinds, gap types, conflict statuses and
comparable value types are all closed; a model- or caller-invented member
must not enter storage.

Nothing here imports the database, a provider SDK or the vector store.
"""
from __future__ import annotations

from .errors import UnknownTraceVocabularyValue


def require(vocabulary: "type", value: str, *, field: str) -> str:
    """Validate *value* against a vocabulary class. Returns the normalized
    value or raises the typed error naming the field and the allowed set."""
    clean = str(value or "").strip().lower()
    if clean not in vocabulary.VALUES:
        raise UnknownTraceVocabularyValue(field=field, value=value,
                                          allowed=sorted(vocabulary.VALUES))
    return clean


class TraceStage:
    """Policy stages. Stages are POLICY concepts, not entity types — one
    stage may allow several entity types, and no arbitrary model-generated
    stage is ever persisted."""
    REQUIREMENT = "requirement"
    DESIGN = "design"
    INTERFACE = "interface"
    DATA = "data"
    WORKFLOW = "workflow"
    IMPLEMENTATION = "implementation"
    CONFIGURATION = "configuration"
    VERIFICATION = "verification"
    TEST_RESULT = "test-result"
    EVIDENCE = "evidence"
    OPERATION = "operation"
    VALUES = frozenset({
        REQUIREMENT, DESIGN, INTERFACE, DATA, WORKFLOW, IMPLEMENTATION,
        CONFIGURATION, VERIFICATION, TEST_RESULT, EVIDENCE, OPERATION,
    })


class PolicySource:
    BUILTIN = "builtin"
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"
    VALUES = frozenset({BUILTIN, ORGANIZATION, WORKSPACE})


class TraceRunStatus:
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    VALUES = frozenset({PLANNED, RUNNING, PARTIAL, DONE, FAILED, CANCELLED,
                        STALE})
    #: Statuses a run never leaves on its own.
    TERMINAL = frozenset({PARTIAL, DONE, FAILED, CANCELLED, STALE})


class TracePathKind:
    REQUIREMENT_TO_DESIGN = "requirement-to-design"
    REQUIREMENT_TO_INTERFACE = "requirement-to-interface"
    REQUIREMENT_TO_IMPLEMENTATION = "requirement-to-implementation"
    REQUIREMENT_TO_TEST = "requirement-to-test"
    REQUIREMENT_TO_EVIDENCE = "requirement-to-evidence"
    CODE_TO_REQUIREMENT = "code-to-requirement"
    TEST_TO_REQUIREMENT = "test-to-requirement"
    VALUES = frozenset({
        REQUIREMENT_TO_DESIGN, REQUIREMENT_TO_INTERFACE,
        REQUIREMENT_TO_IMPLEMENTATION, REQUIREMENT_TO_TEST,
        REQUIREMENT_TO_EVIDENCE, CODE_TO_REQUIREMENT, TEST_TO_REQUIREMENT,
    })


class TracePathStatus:
    VERIFIED = "verified"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"
    BROKEN = "broken"
    UNSUPPORTED = "unsupported"
    VALUES = frozenset({VERIFIED, PARTIAL, AMBIGUOUS, STALE, BROKEN,
                        UNSUPPORTED})
    #: Deterministic ordering rank (lower sorts first).
    ORDER = (VERIFIED, PARTIAL, AMBIGUOUS, STALE, BROKEN, UNSUPPORTED)

    @classmethod
    def rank(cls, value: str) -> int:
        try:
            return cls.ORDER.index(str(value or ""))
        except ValueError:
            return len(cls.ORDER)


class TraceConfidence:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    VALUES = frozenset({HIGH, MEDIUM, LOW})
    ORDER = (HIGH, MEDIUM, LOW)

    @classmethod
    def rank(cls, value: str) -> int:
        try:
            return cls.ORDER.index(str(value or ""))
        except ValueError:
            return len(cls.ORDER)


class StepEvidenceStatus:
    """Per-step evidence verdict, computed deterministically."""
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    VALUES = frozenset({CURRENT, STALE, MISSING})


class StepDirection:
    """The underlying relation's true direction relative to trace flow.
    ``forward`` = the relation's source entity is the earlier stage."""
    FORWARD = "forward"
    REVERSE = "reverse"
    VALUES = frozenset({FORWARD, REVERSE})


class GapType:
    MISSING_DESIGN = "missing-design"
    MISSING_INTERFACE = "missing-interface"
    MISSING_DATA_MODEL = "missing-data-model"
    MISSING_WORKFLOW = "missing-workflow"
    MISSING_IMPLEMENTATION = "missing-implementation"
    PARTIAL_IMPLEMENTATION_ONLY = "partial-implementation-only"
    MISSING_CONFIGURATION = "missing-configuration"
    MISSING_TEST = "missing-test"
    MISSING_TEST_RESULT = "missing-test-result"
    MISSING_EVIDENCE = "missing-evidence"
    STALE_PATH = "stale-path"
    BROKEN_PATH = "broken-path"
    AMBIGUOUS_TARGET = "ambiguous-target"
    UNSUPPORTED_RELATION = "unsupported-relation"
    AUTHORITY_GAP = "authority-gap"
    ORPHAN_REQUIREMENT = "orphan-requirement"
    ORPHAN_CODE = "orphan-code"
    ORPHAN_TEST = "orphan-test"
    ORPHAN_DOCUMENT = "orphan-document"
    VALUES = frozenset({
        MISSING_DESIGN, MISSING_INTERFACE, MISSING_DATA_MODEL,
        MISSING_WORKFLOW, MISSING_IMPLEMENTATION,
        PARTIAL_IMPLEMENTATION_ONLY, MISSING_CONFIGURATION, MISSING_TEST,
        MISSING_TEST_RESULT, MISSING_EVIDENCE, STALE_PATH, BROKEN_PATH,
        AMBIGUOUS_TARGET, UNSUPPORTED_RELATION, AUTHORITY_GAP,
        ORPHAN_REQUIREMENT, ORPHAN_CODE, ORPHAN_TEST, ORPHAN_DOCUMENT,
    })

    #: The missing-<stage> gap for a skipped REQUIRED policy stage.
    BY_MISSING_STAGE = {
        TraceStage.DESIGN: MISSING_DESIGN,
        TraceStage.INTERFACE: MISSING_INTERFACE,
        TraceStage.DATA: MISSING_DATA_MODEL,
        TraceStage.WORKFLOW: MISSING_WORKFLOW,
        TraceStage.IMPLEMENTATION: MISSING_IMPLEMENTATION,
        TraceStage.CONFIGURATION: MISSING_CONFIGURATION,
        TraceStage.VERIFICATION: MISSING_TEST,
        TraceStage.TEST_RESULT: MISSING_TEST_RESULT,
        TraceStage.EVIDENCE: MISSING_EVIDENCE,
    }


class GapSeverity:
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    VALUES = frozenset({INFO, LOW, MEDIUM, HIGH, CRITICAL})
    ORDER = (INFO, LOW, MEDIUM, HIGH, CRITICAL)

    @classmethod
    def rank(cls, value: str) -> int:
        try:
            return cls.ORDER.index(str(value or ""))
        except ValueError:
            return 0


#: Deterministic shipped severity defaults (spec §15). Organization policies
#: may override within the closed GapSeverity set; nothing here is decided
#: by a model.
DEFAULT_GAP_SEVERITIES = {
    GapType.MISSING_DESIGN: GapSeverity.HIGH,
    GapType.MISSING_INTERFACE: GapSeverity.HIGH,
    GapType.MISSING_DATA_MODEL: GapSeverity.HIGH,
    GapType.MISSING_WORKFLOW: GapSeverity.HIGH,
    GapType.MISSING_IMPLEMENTATION: GapSeverity.HIGH,
    GapType.PARTIAL_IMPLEMENTATION_ONLY: GapSeverity.MEDIUM,
    GapType.MISSING_CONFIGURATION: GapSeverity.HIGH,
    GapType.MISSING_TEST: GapSeverity.HIGH,
    GapType.MISSING_TEST_RESULT: GapSeverity.MEDIUM,
    GapType.MISSING_EVIDENCE: GapSeverity.MEDIUM,
    GapType.STALE_PATH: GapSeverity.HIGH,
    GapType.BROKEN_PATH: GapSeverity.HIGH,
    GapType.AMBIGUOUS_TARGET: GapSeverity.MEDIUM,
    GapType.UNSUPPORTED_RELATION: GapSeverity.MEDIUM,
    GapType.AUTHORITY_GAP: GapSeverity.LOW,
    GapType.ORPHAN_REQUIREMENT: GapSeverity.HIGH,
    GapType.ORPHAN_CODE: GapSeverity.INFO,
    GapType.ORPHAN_TEST: GapSeverity.LOW,
    GapType.ORPHAN_DOCUMENT: GapSeverity.INFO,
}


class GapStatus:
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    STALE = "stale"
    VALUES = frozenset({OPEN, RESOLVED, ACCEPTED, DISMISSED, STALE})


class CoverageStatus:
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    VALUES = frozenset({HEALTHY, WARNING, CRITICAL, UNKNOWN})


class ConflictStatus:
    OPEN = "open"
    UNDER_REVIEW = "under-review"
    ACCEPTED_RISK = "accepted-risk"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    STALE = "stale"
    SUPERSEDED = "superseded"
    VALUES = frozenset({OPEN, UNDER_REVIEW, ACCEPTED_RISK, RESOLVED,
                        DISMISSED, STALE, SUPERSEDED})
    #: Statuses that count as "current" (unresolved governance surface).
    CURRENT = frozenset({OPEN, UNDER_REVIEW, ACCEPTED_RISK})


class ConflictOrigin:
    DETERMINISTIC = "deterministic"
    SEMANTIC_PROMOTION = "semantic-promotion"
    MANUAL = "manual"
    VALUES = frozenset({DETERMINISTIC, SEMANTIC_PROMOTION, MANUAL})


class ConflictSeverity:
    """Reuses the gap severity scale for one consistent triage vocabulary."""
    VALUES = GapSeverity.VALUES
    ORDER = GapSeverity.ORDER


class ConflictObjectRole:
    SUBJECT = "subject"
    LEFT = "left"
    RIGHT = "right"
    EXPECTED = "expected"
    ACTUAL = "actual"
    AUTHORITY = "authority"
    VALUES = frozenset({SUBJECT, LEFT, RIGHT, EXPECTED, ACTUAL, AUTHORITY})


class ConflictObjectKind:
    ENTITY = "entity"
    CLAIM = "claim"
    RELATION = "relation"
    VALUES = frozenset({ENTITY, CLAIM, RELATION})


class ConflictDecisionType:
    START_REVIEW = "start-review"
    ACCEPT_RISK = "accept-risk"
    RESOLVE = "resolve"
    DISMISS = "dismiss"
    REOPEN = "reopen"
    SUPERSEDE = "supersede"
    VALUES = frozenset({START_REVIEW, ACCEPT_RISK, RESOLVE, DISMISS, REOPEN,
                        SUPERSEDE})


class ConflictResolutionType:
    LEFT_CORRECT = "left-correct"
    RIGHT_CORRECT = "right-correct"
    BOTH_UPDATED = "both-updated"
    SUPERSEDED = "superseded"
    FALSE_POSITIVE = "false-positive"
    OTHER = "other"
    VALUES = frozenset({LEFT_CORRECT, RIGHT_CORRECT, BOTH_UPDATED,
                        SUPERSEDED, FALSE_POSITIVE, OTHER})


class ComparableValueType:
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DURATION = "duration"
    SIZE = "size"
    COUNT = "count"
    HTTP_METHOD = "http-method"
    API_PATH = "api-path"
    DATA_TYPE = "data-type"
    IDENTIFIER = "identifier"
    ENUM_SET = "enum-set"
    VALUES = frozenset({STRING, INTEGER, DECIMAL, BOOLEAN, DURATION, SIZE,
                        COUNT, HTTP_METHOD, API_PATH, DATA_TYPE, IDENTIFIER,
                        ENUM_SET})


class TraceJobStep:
    """Progress steps of a traceability_refresh job."""
    PLANNING = "planning"
    SELECTING_ROOTS = "selecting-roots"
    BUILDING_PATHS = "building-paths"
    VALIDATING_PATHS = "validating-paths"
    CALCULATING_COVERAGE = "calculating-coverage"
    DETECTING_GAPS = "detecting-gaps"
    DETECTING_ORPHANS = "detecting-orphans"
    PERSISTING_SNAPSHOT = "persisting-snapshot"
    DONE = "done"


class ConflictJobStep:
    """Progress steps of a conflict_scan job."""
    PLANNING = "planning"
    COLLECTING_FACTS = "collecting-comparable-facts"
    RUNNING_DETECTORS = "running-detectors"
    VERIFYING_EVIDENCE = "verifying-evidence"
    DEDUPLICATING = "deduplicating"
    PERSISTING_CONFLICTS = "persisting-conflicts"
    RECONCILING_CONFLICTS = "reconciling-conflicts"
    DONE = "done"


__all__ = [
    "require",
    "TraceStage", "PolicySource", "TraceRunStatus", "TracePathKind",
    "TracePathStatus", "TraceConfidence", "StepEvidenceStatus",
    "StepDirection", "GapType", "GapSeverity", "DEFAULT_GAP_SEVERITIES",
    "GapStatus", "CoverageStatus",
    "ConflictStatus", "ConflictOrigin", "ConflictSeverity",
    "ConflictObjectRole", "ConflictObjectKind", "ConflictDecisionType",
    "ConflictResolutionType", "ComparableValueType",
    "TraceJobStep", "ConflictJobStep",
]
