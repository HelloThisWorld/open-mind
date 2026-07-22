"""Traceability Policy schema validation.

Deterministic and closed: every stage name, entity type, relation type, gap
type and severity must come from its closed vocabulary; rule keys are an
allow-list; executable content, provider URLs and secret-looking values are
rejected. An invalid policy is REPORTED with every error found (never just
the first), stays listable, and can never be selected.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from ..knowledge.vocabularies import EntityType, RelationType
from .models import (POLICY_SCHEMA_VERSION, PolicyRules, PolicyStage,
                     PolicyTransition, TraceabilityPolicy)
from .vocabularies import GapSeverity, GapType, TraceStage

MAX_POLICY_STAGES = 20
MAX_POLICY_TRANSITIONS = 60
MAX_POLICY_DEPTH = 10
MAX_NAME_CHARS = 80
MAX_TITLE_CHARS = 200

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
#: Executable/secret content that must never appear in a declarative policy.
_FORBIDDEN_CONTENT = re.compile(
    r"(?i)(https?://|<script|\beval\s*\(|\bexec\s*\(|__import__|\bimport\s+os"
    r"|subprocess|api[_-]?key|secret|password|bearer\s)")

_RULE_KEYS = {
    "allowInferredRelations", "inferredRelationMaximumCount",
    "allowPossiblyRelated", "requireCurrentEvidence", "requireActiveObjects",
    "requireAuthoritativeRoots", "maximumDepth",
    "coverageHealthyMinimumPct", "coverageWarningMinimumPct",
    "gapSeverities",
}
_TOP_KEYS = {"schemaVersion", "name", "title", "rootTypes", "stages",
             "transitions", "rules"}


def _scan_forbidden(data: Any, path: str, errors: List[str]) -> None:
    """Reject executable code, provider URLs and secret-looking strings
    anywhere in the document."""
    if isinstance(data, dict):
        for key, value in data.items():
            _scan_forbidden(key, path, errors)
            _scan_forbidden(value, f"{path}.{key}", errors)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _scan_forbidden(item, f"{path}[{i}]", errors)
    elif isinstance(data, str):
        if _FORBIDDEN_CONTENT.search(data):
            errors.append(f"{path}: forbidden content (executable code, "
                          f"URL or secret-looking value): {data[:60]!r}")


def validate_policy_data(data: Any, *, source: str
                         ) -> Tuple[TraceabilityPolicy, List[str]]:
    """Validate one raw policy document. Returns ``(policy, errors)`` — the
    policy object is only meaningful when ``errors`` is empty."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return TraceabilityPolicy(name="", title="", source=source), \
            ["policy document must be a mapping"]

    _scan_forbidden(data, "policy", errors)
    for key in data:
        if key not in _TOP_KEYS:
            errors.append(f"unknown top-level key: {key!r}")

    schema_version = str(data.get("schemaVersion") or "")
    if schema_version != POLICY_SCHEMA_VERSION:
        errors.append(
            f"schemaVersion must be {POLICY_SCHEMA_VERSION!r} "
            f"(got {schema_version!r})")

    name = str(data.get("name") or "").strip()
    if not _NAME_RE.match(name):
        errors.append(
            f"name must match [a-z0-9][a-z0-9-]* and be at most "
            f"{MAX_NAME_CHARS} characters (got {name!r})")
    title = str(data.get("title") or "").strip()[:MAX_TITLE_CHARS] or name

    # -- root types ---------------------------------------------------------
    root_types: List[str] = []
    raw_roots = data.get("rootTypes")
    if not isinstance(raw_roots, list) or not raw_roots:
        errors.append("rootTypes must be a non-empty list of entity types")
    else:
        for value in raw_roots:
            entity_type = str(value or "").strip().lower()
            if entity_type not in EntityType.VALUES:
                errors.append(f"rootTypes: unknown entity type "
                              f"{entity_type!r}")
            elif entity_type not in root_types:
                root_types.append(entity_type)

    # -- stages -------------------------------------------------------------
    stages: List[PolicyStage] = []
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        errors.append("stages must be a non-empty list")
        raw_stages = []
    if len(raw_stages) > MAX_POLICY_STAGES:
        errors.append(f"at most {MAX_POLICY_STAGES} stages "
                      f"(got {len(raw_stages)})")
        raw_stages = raw_stages[:MAX_POLICY_STAGES]
    seen_stages: set = set()
    for i, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            errors.append(f"stages[{i}] must be a mapping")
            continue
        stage_name = str(raw.get("name") or "").strip().lower()
        if stage_name not in TraceStage.VALUES:
            errors.append(f"stages[{i}]: unknown stage {stage_name!r} "
                          f"(allowed: {', '.join(sorted(TraceStage.VALUES))})")
            continue
        if stage_name in seen_stages:
            errors.append(f"stages[{i}]: duplicate stage {stage_name!r}")
            continue
        seen_stages.add(stage_name)
        entity_types: List[str] = []
        for value in raw.get("entityTypes") or []:
            entity_type = str(value or "").strip().lower()
            if entity_type not in EntityType.VALUES:
                errors.append(f"stages[{i}] ({stage_name}): unknown entity "
                              f"type {entity_type!r}")
            elif entity_type not in entity_types:
                entity_types.append(entity_type)
        if not entity_types:
            errors.append(f"stages[{i}] ({stage_name}): entityTypes must "
                          f"be non-empty")
        stages.append(PolicyStage(
            name=stage_name, entity_types=entity_types,
            required=bool(raw.get("required", False)),
            requires_evidence=bool(raw.get("requiresEvidence", False))))

    # The first stage must be the root stage and required.
    if stages:
        if stages[0].name != TraceStage.REQUIREMENT:
            errors.append("the first stage must be 'requirement'")
        elif not stages[0].required:
            errors.append("the 'requirement' stage must be required")

    # -- transitions --------------------------------------------------------
    transitions: List[PolicyTransition] = []
    raw_transitions = data.get("transitions")
    if not isinstance(raw_transitions, list) or not raw_transitions:
        errors.append("transitions must be a non-empty list")
        raw_transitions = []
    if len(raw_transitions) > MAX_POLICY_TRANSITIONS:
        errors.append(f"at most {MAX_POLICY_TRANSITIONS} transitions "
                      f"(got {len(raw_transitions)})")
        raw_transitions = raw_transitions[:MAX_POLICY_TRANSITIONS]
    for i, raw in enumerate(raw_transitions):
        if not isinstance(raw, dict):
            errors.append(f"transitions[{i}] must be a mapping")
            continue
        from_stage = str(raw.get("from") or "").strip().lower()
        to_stage = str(raw.get("to") or "").strip().lower()
        for label, stage_name in (("from", from_stage), ("to", to_stage)):
            if stage_name not in seen_stages:
                errors.append(f"transitions[{i}].{label}: stage "
                              f"{stage_name!r} is not declared in stages")
        if from_stage == to_stage:
            errors.append(f"transitions[{i}]: from and to must differ")
        relation_types: List[str] = []
        for value in raw.get("relationTypes") or []:
            relation_type = str(value or "").strip().lower()
            if relation_type not in RelationType.VALUES:
                errors.append(f"transitions[{i}]: unknown relation type "
                              f"{relation_type!r}")
            elif relation_type not in relation_types:
                relation_types.append(relation_type)
        if not relation_types:
            errors.append(f"transitions[{i}]: relationTypes must be "
                          f"non-empty")
        transitions.append(PolicyTransition(
            from_stage=from_stage, to_stage=to_stage,
            relation_types=relation_types))

    # -- rules --------------------------------------------------------------
    rules = PolicyRules()
    raw_rules = data.get("rules")
    if raw_rules is not None and not isinstance(raw_rules, dict):
        errors.append("rules must be a mapping")
        raw_rules = None
    for key in (raw_rules or {}):
        if key not in _RULE_KEYS:
            errors.append(f"rules: unknown key {key!r}")
    if raw_rules:
        rules.allow_inferred_relations = bool(
            raw_rules.get("allowInferredRelations", True))
        try:
            rules.inferred_relation_maximum_count = max(0, int(
                raw_rules.get("inferredRelationMaximumCount", 2)))
        except (TypeError, ValueError):
            errors.append("rules.inferredRelationMaximumCount must be an "
                          "integer")
        rules.allow_possibly_related = bool(
            raw_rules.get("allowPossiblyRelated", False))
        rules.require_current_evidence = bool(
            raw_rules.get("requireCurrentEvidence", True))
        rules.require_active_objects = bool(
            raw_rules.get("requireActiveObjects", True))
        rules.require_authoritative_roots = bool(
            raw_rules.get("requireAuthoritativeRoots", False))
        try:
            depth = int(raw_rules.get("maximumDepth", 8))
            if not 1 <= depth <= MAX_POLICY_DEPTH:
                errors.append(f"rules.maximumDepth must be between 1 and "
                              f"{MAX_POLICY_DEPTH} (got {depth})")
            rules.maximum_depth = min(max(depth, 1), MAX_POLICY_DEPTH)
        except (TypeError, ValueError):
            errors.append("rules.maximumDepth must be an integer")
        for key, attr in (("coverageHealthyMinimumPct",
                           "coverage_healthy_minimum_pct"),
                          ("coverageWarningMinimumPct",
                           "coverage_warning_minimum_pct")):
            if key in raw_rules:
                value = raw_rules.get(key)
                if value is None:
                    setattr(rules, attr, None)
                else:
                    try:
                        pct = float(value)
                        if not 0.0 <= pct <= 100.0:
                            errors.append(f"rules.{key} must be within "
                                          f"0..100")
                        setattr(rules, attr, pct)
                    except (TypeError, ValueError):
                        errors.append(f"rules.{key} must be a number or "
                                      f"null")
        severities = raw_rules.get("gapSeverities") or {}
        if not isinstance(severities, dict):
            errors.append("rules.gapSeverities must be a mapping")
            severities = {}
        clean_severities: Dict[str, str] = {}
        for gap_type, severity in severities.items():
            gap_type = str(gap_type or "").strip().lower()
            severity = str(severity or "").strip().lower()
            if gap_type not in GapType.VALUES:
                errors.append(f"rules.gapSeverities: unknown gap type "
                              f"{gap_type!r}")
                continue
            if severity not in GapSeverity.VALUES:
                errors.append(f"rules.gapSeverities[{gap_type}]: unknown "
                              f"severity {severity!r}")
                continue
            clean_severities[gap_type] = severity
        rules.gap_severities = clean_severities

    policy = TraceabilityPolicy(
        name=name, title=title, source=source, root_types=root_types,
        stages=stages, transitions=transitions, rules=rules,
        schema_version=schema_version or POLICY_SCHEMA_VERSION)
    return policy, errors


__all__ = ["validate_policy_data", "MAX_POLICY_STAGES",
           "MAX_POLICY_TRANSITIONS", "MAX_POLICY_DEPTH"]
