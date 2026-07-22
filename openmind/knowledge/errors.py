"""Typed errors of the canonical Knowledge Graph.

Same taxonomy discipline as :mod:`openmind.semantic.errors`: each failure a
caller must react to differently is its own class, all extending
:class:`openmind.domain.errors.OpenMindError` so the CLI and REST adapters
translate them like every other application error (machine-readable ``code``,
honest ``http_status``, stable ``exit_code``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..domain.errors import OpenMindError


class KnowledgeError(OpenMindError):
    """Base class for every knowledge-graph error."""

    code = "knowledge_error"
    exit_code = 4
    http_status = 500


class UnknownVocabularyValue(KnowledgeError):
    """A write named a vocabulary member the graph does not define. The graph
    never silently accepts an arbitrary (possibly model-generated) type."""

    code = "unknown_vocabulary_value"
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


class EntityNotFound(KnowledgeError):
    code = "entity_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, entity_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"entity_id": entity_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"engineering entity not found: {entity_id!r}",
                         details=details)
        self.entity_id = entity_id


class ClaimNotFound(KnowledgeError):
    code = "claim_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, claim_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"claim_id": claim_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"engineering claim not found: {claim_id!r}",
                         details=details)
        self.claim_id = claim_id


class RelationNotFound(KnowledgeError):
    code = "relation_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, relation_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"relation_id": relation_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"engineering relation not found: {relation_id!r}",
                         details=details)
        self.relation_id = relation_id


class GraphNodeNotFound(KnowledgeError):
    code = "graph_node_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, node_id: str, *, workspace_id: str = "") -> None:
        details: Dict[str, Any] = {"node_id": node_id}
        if workspace_id:
            details["workspace_id"] = workspace_id
        super().__init__(f"graph node not found: {node_id!r}", details=details)
        self.node_id = node_id


class KnowledgeRevisionNotFound(KnowledgeError):
    code = "knowledge_revision_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, revision_number: int, *, workspace_id: str = "") -> None:
        super().__init__(
            f"knowledge revision not found: {revision_number}",
            details={"revision_number": revision_number,
                     "workspace_id": workspace_id})
        self.revision_number = revision_number


class GraphEvidenceInvalid(KnowledgeError):
    """A cited Evidence id does not exist in this Workspace, or a quote is
    not a substring of the immutable Evidence content. The write is rejected
    whole — a fabricated citation never enters canonical storage."""

    code = "graph_evidence_invalid"
    exit_code = 2
    http_status = 400


class AliasCollision(KnowledgeError):
    """The normalized alias is already held by a DIFFERENT active Entity.
    Never silently attached; resolve by explicit merge or manual decision."""

    code = "alias_collision"
    exit_code = 1
    http_status = 409

    def __init__(self, alias: str, holders: List[Dict[str, str]]) -> None:
        super().__init__(
            f"alias {alias!r} collides with an existing active entity alias",
            details={"alias": alias, "holders": holders})
        self.alias = alias
        self.holders = holders


class PromotionBlocked(KnowledgeError):
    """The Candidate fails a promotion eligibility rule. The blocking
    reasons are enumerated; nothing was written."""

    code = "promotion_blocked"
    exit_code = 1
    http_status = 409

    def __init__(self, candidate_id: str, reasons: List[str]) -> None:
        super().__init__(
            f"candidate {candidate_id!r} cannot be promoted: "
            + "; ".join(reasons),
            details={"candidate_id": candidate_id, "blocking_reasons": reasons})
        self.candidate_id = candidate_id
        self.reasons = reasons


class GraphConflict(KnowledgeError):
    """A governance operation is illegal for the object's current state
    (merging into a merged entity, superseding with a withdrawn object,
    splitting objects that do not belong to the source, ...)."""

    code = "graph_conflict"
    exit_code = 1
    http_status = 409


class GraphLimitExceeded(KnowledgeError):
    """A bounded traversal or listing was asked to exceed its hard cap."""

    code = "graph_limit_exceeded"
    exit_code = 2
    http_status = 400


__all__ = [
    "KnowledgeError", "UnknownVocabularyValue",
    "EntityNotFound", "ClaimNotFound", "RelationNotFound",
    "GraphNodeNotFound", "KnowledgeRevisionNotFound",
    "GraphEvidenceInvalid", "AliasCollision", "PromotionBlocked",
    "GraphConflict", "GraphLimitExceeded",
]
