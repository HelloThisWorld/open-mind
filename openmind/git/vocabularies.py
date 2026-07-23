"""Closed vocabularies of the Git and Overlay planes (Phase 7).

Same convention as the rest of OpenMind: small classes of string constants
with a ``VALUES`` frozenset, validated at write boundaries by :func:`require`.
An unknown value at a boundary is a typed failure, never a silent default.

Nothing here imports the database, a provider SDK or the vector store.
"""
from __future__ import annotations

from .errors import GitError


class UnknownGitVocabularyValue(GitError):
    code = "unknown_git_vocabulary_value"
    exit_code = 2
    http_status = 400

    def __init__(self, *, field: str, value: object, allowed) -> None:
        super().__init__(
            f"unknown {field}: {str(value)!r}",
            details={"field": field, "value": str(value),
                     "allowed": list(allowed)})
        self.field = field


def require(vocabulary: "type", value: str, *, field: str) -> str:
    """Validate *value* against a vocabulary class; return the normalized value
    or raise the typed error naming the field and the allowed set."""
    clean = str(value or "").strip().lower()
    if clean not in vocabulary.VALUES:
        raise UnknownGitVocabularyValue(
            field=field, value=value, allowed=sorted(vocabulary.VALUES))
    return clean


class ChangeType:
    """How Git classifies a changed path in a diff (spec §14)."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type-changed"
    UNMERGED = "unmerged"
    SUBMODULE = "submodule"
    UNKNOWN = "unknown"
    VALUES = frozenset({ADDED, MODIFIED, DELETED, RENAMED, COPIED,
                        TYPE_CHANGED, UNMERGED, SUBMODULE, UNKNOWN})

    #: Git's single-letter raw status -> our change type. 'R'/'C' arrive with a
    #: similarity score suffix and are handled by the parser, not this map.
    _STATUS = {"A": ADDED, "M": MODIFIED, "D": DELETED, "T": TYPE_CHANGED,
               "U": UNMERGED, "X": UNKNOWN, "B": UNKNOWN}

    @classmethod
    def from_status(cls, status: str) -> str:
        s = (status or "").strip().upper()
        if not s:
            return cls.UNKNOWN
        head = s[0]
        if head == "R":
            return cls.RENAMED
        if head == "C":
            return cls.COPIED
        return cls._STATUS.get(head, cls.UNKNOWN)


class OverlayKind:
    """The shape of change an overlay represents (spec §11)."""
    COMMIT_RANGE = "commit-range"
    BRANCH = "branch"
    PR = "pr"
    WORKING_TREE = "working-tree"
    CHANGE_SET = "change-set"
    VALUES = frozenset({COMMIT_RANGE, BRANCH, PR, WORKING_TREE, CHANGE_SET})


class OverlayState:
    """Overlay lifecycle states (spec §11)."""
    PLANNED = "planned"
    QUEUED = "queued"
    BUILDING = "building"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"
    CLOSED = "closed"
    MERGED = "merged"
    ABANDONED = "abandoned"
    VALUES = frozenset({PLANNED, QUEUED, BUILDING, READY, STALE, FAILED,
                        CLOSED, MERGED, ABANDONED})
    #: States in which an overlay still holds live derived data + collections.
    LIVE = frozenset({READY, STALE})
    #: Terminal states an overlay can rest in.
    TERMINAL = frozenset({CLOSED, MERGED, ABANDONED, FAILED})


class Side:
    """Which side of a change a segment / evidence row belongs to."""
    BEFORE = "before"
    AFTER = "after"
    VALUES = frozenset({BEFORE, AFTER})


class WorktreeLayer:
    """Provenance of a working-tree change (spec §32)."""
    STAGED = "staged"
    UNSTAGED = "unstaged"
    UNTRACKED = "untracked"
    VALUES = frozenset({STAGED, UNSTAGED, UNTRACKED})


class DeltaType:
    """How a graph object changed between before and after (spec §22)."""
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
    VALUES = frozenset({ADDED, MODIFIED, REMOVED, RENAMED, UNCHANGED, UNKNOWN})


class SegmentChange:
    """Classification of a changed overlay segment (spec §19)."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"
    CONTEXT_CHANGED = "context-changed"
    UNCHANGED = "unchanged"
    VALUES = frozenset({ADDED, MODIFIED, DELETED, MOVED, CONTEXT_CHANGED,
                        UNCHANGED})


class ImpactClass:
    """Why an impact was included (spec §23)."""
    DIRECT = "direct"
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"
    STRUCTURAL = "structural"
    POTENTIAL = "potential"
    UNKNOWN = "unknown"
    VALUES = frozenset({DIRECT, UPSTREAM, DOWNSTREAM, STRUCTURAL, POTENTIAL,
                        UNKNOWN})


class RiskLevel:
    """Deterministic change-risk levels (spec §28), highest-first ordering."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    UNKNOWN = "unknown"
    VALUES = frozenset({CRITICAL, HIGH, MEDIUM, LOW, INFO, UNKNOWN})
    #: Severity rank for the KNOWN levels. ``unknown`` is deliberately absent —
    #: it is never comparable-with / reducible-to a known level (spec: never
    #: downgrade unknown to low). Callers treat it separately.
    _RANK = {INFO: 0, LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4}

    @classmethod
    def rank(cls, level: str) -> int:
        return cls._RANK.get(level, -1)

    @classmethod
    def max(cls, a: str, b: str) -> str:
        """The more severe of two KNOWN levels. ``unknown`` never wins here —
        overall risk carries unknowns as a separate list, not as a level."""
        if a == cls.UNKNOWN:
            return b
        if b == cls.UNKNOWN:
            return a
        return a if cls.rank(a) >= cls.rank(b) else b


__all__ = [
    "require", "UnknownGitVocabularyValue",
    "ChangeType", "OverlayKind", "OverlayState", "Side", "WorktreeLayer",
    "DeltaType", "SegmentChange", "ImpactClass", "RiskLevel",
]
