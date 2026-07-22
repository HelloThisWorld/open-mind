"""Typed errors of the traceability and conflict engine.

Same taxonomy discipline as :mod:`openmind.knowledge.errors`: each failure a
caller must react to differently is its own class, all extending
:class:`openmind.domain.errors.OpenMindError` so the CLI and REST adapters
translate them like every other application error.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..domain.errors import OpenMindError


class TraceabilityError(OpenMindError):
    """Base class for every traceability/conflict error."""

    code = "traceability_error"
    exit_code = 4
    http_status = 500


class UnknownTraceVocabularyValue(TraceabilityError):
    """A write named a vocabulary member the engine does not define."""

    code = "unknown_trace_vocabulary_value"
    exit_code = 2
    http_status = 400

    def __init__(self, *, field: str, value: Any,
                 allowed: Optional[List[str]] = None) -> None:
        super().__init__(
            f"unknown {field}: {str(value)!r}",
            details={"field": field, "value": str(value),
                     "allowed": list(allowed or [])})
        self.field = field
        self.value = value


class PolicyNotFound(TraceabilityError):
    code = "trace_policy_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, name: str) -> None:
        super().__init__(f"traceability policy not found: {name!r}",
                         details={"policy_name": name})
        self.name = name


class PolicyInvalid(TraceabilityError):
    """A policy definition fails schema validation. The errors are
    enumerated; an invalid policy stays listable but never selectable."""

    code = "trace_policy_invalid"
    exit_code = 2
    http_status = 400

    def __init__(self, name: str, errors: List[str]) -> None:
        super().__init__(
            f"traceability policy {name!r} is invalid: " + "; ".join(errors),
            details={"policy_name": name, "errors": list(errors)})
        self.name = name
        self.errors = errors


class TraceRunNotFound(TraceabilityError):
    code = "trace_run_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, run_id: str, *, workspace_id: str = "") -> None:
        super().__init__(f"traceability run not found: {run_id!r}",
                         details={"run_id": run_id,
                                  "workspace_id": workspace_id})
        self.run_id = run_id


class TracePathNotFound(TraceabilityError):
    code = "trace_path_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, trace_id: str, *, workspace_id: str = "") -> None:
        super().__init__(f"trace path not found: {trace_id!r}",
                         details={"trace_id": trace_id,
                                  "workspace_id": workspace_id})
        self.trace_id = trace_id


class GapNotFound(TraceabilityError):
    code = "trace_gap_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, gap_id: str, *, workspace_id: str = "") -> None:
        super().__init__(f"traceability gap not found: {gap_id!r}",
                         details={"gap_id": gap_id,
                                  "workspace_id": workspace_id})
        self.gap_id = gap_id


class ConflictNotFound(TraceabilityError):
    code = "conflict_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, conflict_id: str, *, workspace_id: str = "") -> None:
        super().__init__(f"engineering conflict not found: {conflict_id!r}",
                         details={"conflict_id": conflict_id,
                                  "workspace_id": workspace_id})
        self.conflict_id = conflict_id


class ConflictPromotionBlocked(TraceabilityError):
    """The Conflict Candidate fails a promotion eligibility rule. The
    blocking reasons are enumerated; nothing was written."""

    code = "conflict_promotion_blocked"
    exit_code = 1
    http_status = 409

    def __init__(self, candidate_id: str, reasons: List[str]) -> None:
        super().__init__(
            f"conflict candidate {candidate_id!r} cannot be promoted: "
            + "; ".join(reasons),
            details={"candidate_id": candidate_id,
                     "blocking_reasons": reasons})
        self.candidate_id = candidate_id
        self.reasons = reasons


class ConflictStateInvalid(TraceabilityError):
    """A governance action is illegal for the conflict's or gap's current
    state (resolving a dismissed conflict, reopening an open one, ...)."""

    code = "conflict_state_invalid"
    exit_code = 1
    http_status = 409


class TraceLimitExceeded(TraceabilityError):
    """A bounded traversal or listing was asked to exceed its hard cap."""

    code = "trace_limit_exceeded"
    exit_code = 2
    http_status = 400


class TraceRootIneligible(TraceabilityError):
    """The named entity cannot be a trace root under the active policy
    (wrong type, wrong workspace, inactive, no active claim, no evidence)."""

    code = "trace_root_ineligible"
    exit_code = 1
    http_status = 409

    def __init__(self, entity_id: str, reasons: List[str]) -> None:
        super().__init__(
            f"entity {entity_id!r} is not an eligible trace root: "
            + "; ".join(reasons),
            details={"entity_id": entity_id, "reasons": list(reasons)})
        self.entity_id = entity_id
        self.reasons = reasons


__all__ = [
    "TraceabilityError", "UnknownTraceVocabularyValue",
    "PolicyNotFound", "PolicyInvalid",
    "TraceRunNotFound", "TracePathNotFound", "GapNotFound",
    "ConflictNotFound", "ConflictPromotionBlocked", "ConflictStateInvalid",
    "TraceLimitExceeded", "TraceRootIneligible",
]
