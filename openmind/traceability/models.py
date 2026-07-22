"""Value objects of the traceability engine: the declarative Traceability
Policy and the typed comparable fact.

A policy is DATA — a closed declarative document with no executable content.
Validation lives in :mod:`openmind.traceability.validator`; this module only
defines the shapes and the deterministic checksum.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .vocabularies import ComparableValueType

POLICY_SCHEMA_VERSION = "1.0.0"


@dataclass
class PolicyStage:
    """One stage of a policy lifecycle."""
    name: str
    entity_types: List[str] = field(default_factory=list)
    required: bool = False
    requires_evidence: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "entityTypes": list(self.entity_types),
                "required": self.required,
                "requiresEvidence": self.requires_evidence}


@dataclass
class PolicyTransition:
    """One allowed stage transition and the relation types that satisfy it."""
    from_stage: str
    to_stage: str
    relation_types: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"from": self.from_stage, "to": self.to_stage,
                "relationTypes": list(self.relation_types)}


@dataclass
class PolicyRules:
    """Closed rule set. Defaults are the conservative direction."""
    allow_inferred_relations: bool = True
    inferred_relation_maximum_count: int = 2
    allow_possibly_related: bool = False
    require_current_evidence: bool = True
    require_active_objects: bool = True
    require_authoritative_roots: bool = False
    maximum_depth: int = 8
    #: Coverage-status thresholds applied to the fully-traced percentage
    #: (policy-driven, never a global hardcode). ``None`` -> status unknown.
    coverage_healthy_minimum_pct: Optional[float] = 90.0
    coverage_warning_minimum_pct: Optional[float] = 50.0
    #: Per-gap-type severity overrides (closed keys/values, validated).
    gap_severities: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowInferredRelations": self.allow_inferred_relations,
            "inferredRelationMaximumCount":
                self.inferred_relation_maximum_count,
            "allowPossiblyRelated": self.allow_possibly_related,
            "requireCurrentEvidence": self.require_current_evidence,
            "requireActiveObjects": self.require_active_objects,
            "requireAuthoritativeRoots": self.require_authoritative_roots,
            "maximumDepth": self.maximum_depth,
            "coverageHealthyMinimumPct": self.coverage_healthy_minimum_pct,
            "coverageWarningMinimumPct": self.coverage_warning_minimum_pct,
            "gapSeverities": dict(sorted(self.gap_severities.items())),
        }


@dataclass
class TraceabilityPolicy:
    """One validated policy. ``checksum`` is derived, never stored inside
    the definition itself."""
    name: str
    title: str
    source: str                     # PolicySource
    root_types: List[str] = field(default_factory=list)
    stages: List[PolicyStage] = field(default_factory=list)
    transitions: List[PolicyTransition] = field(default_factory=list)
    rules: PolicyRules = field(default_factory=PolicyRules)
    schema_version: str = POLICY_SCHEMA_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "title": self.title,
            "rootTypes": list(self.root_types),
            "stages": [s.as_dict() for s in self.stages],
            "transitions": [t.as_dict() for t in self.transitions],
            "rules": self.rules.as_dict(),
        }

    @property
    def checksum(self) -> str:
        """SHA-256 over the canonical JSON serialization. Deterministic:
        the same definition always hashes the same, and any edit changes
        the checksum."""
        canonical = json.dumps(self.as_dict(), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- lookup helpers (pure, used by the engine) --------------------------
    def stage_of_entity_type(self, entity_type: str) -> Optional[str]:
        """The FIRST stage (policy order) that allows this entity type, or
        None for an unmapped type. First-match keeps the mapping
        deterministic when two stages share a type."""
        for stage in self.stages:
            if entity_type in stage.entity_types:
                return stage.name
        return None

    def stage(self, name: str) -> Optional[PolicyStage]:
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def required_stages(self) -> List[str]:
        return [s.name for s in self.stages if s.required]

    def transitions_from(self, stage_name: str) -> List[PolicyTransition]:
        return [t for t in self.transitions if t.from_stage == stage_name]

    def transitions_to(self, stage_name: str) -> List[PolicyTransition]:
        return [t for t in self.transitions if t.to_stage == stage_name]

    def transition_relation_types(self, from_stage: str,
                                  to_stage: str) -> List[str]:
        out: List[str] = []
        for t in self.transitions:
            if t.from_stage == from_stage and t.to_stage == to_stage:
                for rt in t.relation_types:
                    if rt not in out:
                        out.append(rt)
        return out

    def gap_severity(self, gap_type: str) -> str:
        from .vocabularies import DEFAULT_GAP_SEVERITIES, GapSeverity
        return self.rules.gap_severities.get(
            gap_type, DEFAULT_GAP_SEVERITIES.get(gap_type, GapSeverity.LOW))


@dataclass
class ComparableFact:
    """One deterministically extracted, typed, comparable statement about a
    subject. Facts are what conflict detectors compare — never raw prose."""
    subject_key: str            # normalized subject identity
    property: str               # normalized property name
    operator: str               # "=", "<=", ">=", "<", ">"
    value: Any                  # normalized value (canonical unit)
    unit: str                   # canonical unit or "" (NEVER guessed)
    value_type: str             # ComparableValueType
    source_claim_id: str = ""
    source_entity_id: str = ""
    evidence_id: str = ""
    quote: str = ""
    authority_status: str = "unknown"
    raw_value: str = ""         # pre-normalization, for the report
    raw_unit: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject_key": self.subject_key, "property": self.property,
            "operator": self.operator, "value": self.value,
            "unit": self.unit, "value_type": self.value_type,
            "source_claim_id": self.source_claim_id,
            "source_entity_id": self.source_entity_id,
            "evidence_id": self.evidence_id,
            "authority_status": self.authority_status,
            "raw_value": self.raw_value, "raw_unit": self.raw_unit,
        }

    @property
    def comparable_key(self) -> str:
        """Facts sharing this key are candidates for comparison."""
        return f"{self.subject_key}\x1f{self.property}"


@dataclass
class ConflictDraft:
    """What a detector emits. The scan layer verifies evidence, deduplicates
    and persists — a draft alone changes nothing."""
    category: str
    subject_key: str
    title: str
    description: str
    severity: str
    detector_name: str
    detector_version: str
    #: [{object_kind, object_id, role}]
    objects: List[Dict[str, str]] = field(default_factory=list)
    #: [{evidence_id, role, quote}]
    evidence: List[Dict[str, str]] = field(default_factory=list)
    #: normalized property under dispute (part of conflict identity)
    property: str = ""
    #: the compared values, for the report and the suppression fingerprint
    left_value: str = ""
    right_value: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictDetectionPlan:
    """What one detector WOULD examine. Deterministic, writes nothing."""
    detector_name: str
    detector_version: str
    categories: List[str]
    comparable_facts: int = 0
    comparison_groups: int = 0
    omissions: List[str] = field(default_factory=list)
    limits: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "detector_name": self.detector_name,
            "detector_version": self.detector_version,
            "categories": list(self.categories),
            "comparable_facts": self.comparable_facts,
            "comparison_groups": self.comparison_groups,
            "omissions": list(self.omissions),
            "limits": dict(self.limits),
        }


__all__ = [
    "POLICY_SCHEMA_VERSION", "PolicyStage", "PolicyTransition", "PolicyRules",
    "TraceabilityPolicy", "ComparableFact", "ConflictDraft",
    "ConflictDetectionPlan",
]
