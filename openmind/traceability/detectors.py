"""The deterministic conflict-detector framework and the six shipped
detectors.

Every detector is deterministic, calls no provider, compares only typed
comparable facts (:mod:`openmind.traceability.facts`), bounds its
comparisons, attaches evidence to every draft, and reports omissions and
limits in its plan. A detector failure produces a partial scan with an
explicit per-detector error; it never corrupts existing conflicts.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set

from ..knowledge import store as kg
from ..knowledge.vocabularies import EntityType, GraphLifecycleStatus
from .facts import compare_facts
from .models import ComparableFact, ConflictDetectionPlan, ConflictDraft
from .vocabularies import (ComparableValueType, ConflictObjectKind,
                           ConflictObjectRole, GapSeverity)

MAX_GROUP_FACTS = 50
MAX_DRAFTS_PER_DETECTOR = 200
MAX_RELATION_PAIRS = 2000

#: Canonical conflict categories (the Phase 4 candidate vocabulary minus
#: ``possibly-conflicting``, which is a proposal-only category and stays in
#: the semantic plane).
CATEGORY_DOCUMENT_DOCUMENT = "document-document"
CATEGORY_REQUIREMENT_DESIGN = "requirement-design"
CATEGORY_SPECIFICATION_CODE = "specification-code"
CATEGORY_REQUIREMENT_TEST = "requirement-test"
CATEGORY_INTERFACE_SCHEMA = "interface-schema"
CATEGORY_REVISION_AUTHORITY = "revision-authority"

SUPPORTED_CATEGORIES = frozenset({
    CATEGORY_DOCUMENT_DOCUMENT, CATEGORY_REQUIREMENT_DESIGN,
    CATEGORY_SPECIFICATION_CODE, CATEGORY_REQUIREMENT_TEST,
    CATEGORY_INTERFACE_SCHEMA, CATEGORY_REVISION_AUTHORITY,
})

#: Entity-type groups the detectors use to classify a fact's SIDE.
_DOCUMENTED_TYPES = frozenset({
    EntityType.REQUIREMENT, EntityType.BUSINESS_RULE, EntityType.DECISION,
    EntityType.CONSTRAINT, EntityType.DESIGN, EntityType.DOCUMENT,
    EntityType.WORKFLOW, EntityType.ACCEPTANCE_CRITERION,
})
_ACTUAL_TYPES = frozenset({
    EntityType.CODE_COMPONENT, EntityType.CODE_SYMBOL,
    EntityType.CONFIGURATION, EntityType.DATABASE_OBJECT,
    EntityType.MESSAGE_TOPIC, EntityType.INTERFACE, EntityType.DATA_MODEL,
})
_DESIGN_TYPES = frozenset({EntityType.DESIGN, EntityType.DECISION,
                           EntityType.CONSTRAINT})
_REQUIREMENT_TYPES = frozenset({EntityType.REQUIREMENT,
                                EntityType.BUSINESS_RULE})


class ConflictDetectionContext:
    """Everything one scan hands its detectors: the collected facts and
    bounded graph lookups (entity types resolved once)."""

    def __init__(self, workspace_id: str, knowledge_revision: int,
                 facts: Sequence[ComparableFact]) -> None:
        self.workspace_id = workspace_id
        self.knowledge_revision = knowledge_revision
        self.facts = list(facts)
        self._entity_types: Dict[str, str] = {}

    def entity_type(self, entity_id: str) -> str:
        if not entity_id:
            return ""
        if entity_id not in self._entity_types:
            entity = kg.get_entity(self.workspace_id, entity_id)
            self._entity_types[entity_id] = \
                (entity or {}).get("entity_type", "")
        return self._entity_types[entity_id]

    def facts_by_group(self) -> Dict[str, List[ComparableFact]]:
        groups: Dict[str, List[ComparableFact]] = {}
        for fact in self.facts:
            groups.setdefault(fact.comparable_key, []).append(fact)
        return groups


class ConflictDetector(Protocol):
    name: str
    version: str
    categories: Set[str]

    def plan(self, workspace_id: str,
             knowledge_revision: int) -> ConflictDetectionPlan: ...

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]: ...


# ---------------------------------------------------------------------------
# Shared draft helpers
# ---------------------------------------------------------------------------
def _draft(category: str, detector: "_BaseDetector",
           left: ComparableFact, right: ComparableFact,
           *, title: str, severity: str = GapSeverity.HIGH,
           subject_key: str = "") -> ConflictDraft:
    subject = subject_key or left.subject_key
    objects: List[Dict[str, str]] = []
    seen: Set[str] = set()

    def add(kind: str, object_id: str, role: str) -> None:
        if not object_id or (kind, object_id, role) in seen:
            return
        seen.add((kind, object_id, role))
        objects.append({"object_kind": kind, "object_id": object_id,
                        "role": role})

    add(ConflictObjectKind.CLAIM, left.source_claim_id,
        ConflictObjectRole.LEFT)
    add(ConflictObjectKind.CLAIM, right.source_claim_id,
        ConflictObjectRole.RIGHT)
    add(ConflictObjectKind.ENTITY, left.source_entity_id,
        ConflictObjectRole.LEFT)
    add(ConflictObjectKind.ENTITY, right.source_entity_id,
        ConflictObjectRole.RIGHT)

    evidence: List[Dict[str, str]] = []
    for fact, role in ((left, "left"), (right, "right")):
        if fact.evidence_id:
            evidence.append({"evidence_id": fact.evidence_id,
                             "role": role, "quote": fact.quote})

    def shown(fact: ComparableFact) -> str:
        return f"{fact.raw_value}{(' ' + fact.raw_unit) if fact.raw_unit else ''}" \
            if fact.raw_value else str(fact.value)

    description = (
        f"{left.property} of {subject!r}: one source states "
        f"{shown(left)!r}, another states {shown(right)!r} "
        f"(normalized {left.value!r}{left.unit or ''} vs "
        f"{right.value!r}{right.unit or ''}).")
    return ConflictDraft(
        category=category, subject_key=subject, title=title,
        description=description, severity=severity,
        detector_name=detector.name, detector_version=detector.version,
        objects=objects, evidence=evidence, property=left.property,
        left_value=f"{left.value}{left.unit or ''}",
        right_value=f"{right.value}{right.unit or ''}",
        metadata={"left_claim_id": left.source_claim_id,
                  "right_claim_id": right.source_claim_id,
                  "value_type": left.value_type})


class _BaseDetector:
    name = "base"
    version = "1.0.0"
    categories: Set[str] = set()

    def plan(self, workspace_id: str,
             knowledge_revision: int) -> ConflictDetectionPlan:
        from .facts import collect_comparable_facts
        facts = collect_comparable_facts(workspace_id)
        groups: Dict[str, int] = {}
        for fact in facts:
            groups[fact.comparable_key] = \
                groups.get(fact.comparable_key, 0) + 1
        omissions = [
            f"group {key!r} has {count} facts; only the first "
            f"{MAX_GROUP_FACTS} are compared"
            for key, count in sorted(groups.items())
            if count > MAX_GROUP_FACTS]
        return ConflictDetectionPlan(
            detector_name=self.name, detector_version=self.version,
            categories=sorted(self.categories),
            comparable_facts=len(facts), comparison_groups=len(groups),
            omissions=omissions,
            limits={"max_group_facts": MAX_GROUP_FACTS,
                    "max_drafts": MAX_DRAFTS_PER_DETECTOR})

    # subclasses implement detect()


def _pairs(facts: List[ComparableFact]):
    bounded = facts[:MAX_GROUP_FACTS]
    for i in range(len(bounded)):
        for j in range(i + 1, len(bounded)):
            yield bounded[i], bounded[j]


# ---------------------------------------------------------------------------
# 1. Document–Document
# ---------------------------------------------------------------------------
class DocumentDocumentDetector(_BaseDetector):
    """Two active claims on the SAME canonical subject (same entity — the
    stable identity — or the same cross-entity subject key) state
    structurally incompatible values. Never unrestricted natural-language
    contradiction."""
    name = "document-document"
    version = "1.0.0"
    categories = {CATEGORY_DOCUMENT_DOCUMENT}

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        for _key, group in sorted(context.facts_by_group().items()):
            documented = [
                f for f in group
                if context.entity_type(f.source_entity_id)
                in _DOCUMENTED_TYPES and f.source_claim_id]
            for left, right in _pairs(documented):
                if left.source_claim_id == right.source_claim_id:
                    continue
                if compare_facts(left, right) == "different":
                    drafts.append(_draft(
                        CATEGORY_DOCUMENT_DOCUMENT, self, left, right,
                        title=(f"documents disagree on {left.property} "
                               f"of {left.subject_key}")))
                if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                    return drafts
        return drafts


# ---------------------------------------------------------------------------
# 2. Requirement–Design
# ---------------------------------------------------------------------------
class RequirementDesignDetector(_BaseDetector):
    """Requirement and design claims are canonically related
    (refines / derived-from between their entities) and a deterministically
    extracted attribute differs."""
    name = "requirement-design"
    version = "1.0.0"
    categories = {CATEGORY_REQUIREMENT_DESIGN}

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        facts_by_entity: Dict[str, List[ComparableFact]] = {}
        for fact in context.facts:
            if fact.source_entity_id:
                facts_by_entity.setdefault(fact.source_entity_id,
                                           []).append(fact)
        relations = kg.list_relations(
            context.workspace_id, relation_type="refines",
            lifecycle_status=GraphLifecycleStatus.ACTIVE,
            limit=MAX_RELATION_PAIRS)
        relations += kg.list_relations(
            context.workspace_id, relation_type="derived-from",
            lifecycle_status=GraphLifecycleStatus.ACTIVE,
            limit=MAX_RELATION_PAIRS)
        for relation in relations:
            ends = (relation["source_entity_id"],
                    relation["target_entity_id"])
            types = tuple(context.entity_type(e) for e in ends)
            requirement_id = design_id = ""
            for entity_id, entity_type in zip(ends, types):
                if entity_type in _REQUIREMENT_TYPES:
                    requirement_id = entity_id
                elif entity_type in _DESIGN_TYPES:
                    design_id = entity_id
            if not requirement_id or not design_id:
                continue
            for left in facts_by_entity.get(requirement_id, []):
                for right in facts_by_entity.get(design_id, []):
                    if compare_facts(left, right) == "different":
                        draft = _draft(
                            CATEGORY_REQUIREMENT_DESIGN, self, left,
                            right,
                            title=(f"requirement and design disagree on "
                                   f"{left.property}"),
                            subject_key=left.subject_key)
                        draft.metadata["relation_id"] = relation["id"]
                        drafts.append(draft)
                    if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                        return drafts
        return drafts


# ---------------------------------------------------------------------------
# 3. Specification–Code
# ---------------------------------------------------------------------------
class SpecificationCodeDetector(_BaseDetector):
    """A documented fact and a code/configuration fact share a
    deterministic subject (config key, endpoint path) and differ. Prose is
    never compared to code; only closed extracted facts are."""
    name = "specification-code"
    version = "1.0.0"
    categories = {CATEGORY_SPECIFICATION_CODE}

    #: cross-entity subject properties this detector owns (endpoint facts
    #: belong to the interface-schema detector).
    _PROPERTIES = {"configuration-value", "boolean-obligation"}

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        for _key, group in sorted(context.facts_by_group().items()):
            if not group or group[0].property not in self._PROPERTIES:
                continue
            documented = [f for f in group
                          if context.entity_type(f.source_entity_id)
                          in _DOCUMENTED_TYPES]
            actual = [f for f in group
                      if context.entity_type(f.source_entity_id)
                      in _ACTUAL_TYPES]
            for left in documented[:MAX_GROUP_FACTS]:
                for right in actual[:MAX_GROUP_FACTS]:
                    if compare_facts(left, right) == "different":
                        drafts.append(_draft(
                            CATEGORY_SPECIFICATION_CODE, self, left,
                            right,
                            title=(f"specification and code disagree on "
                                   f"{left.subject_key}")))
                    if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                        return drafts
        return drafts


# ---------------------------------------------------------------------------
# 4. Requirement–Test
# ---------------------------------------------------------------------------
class RequirementTestDetector(_BaseDetector):
    """Requirement and test case are canonically linked (a direct
    ``verifies`` or the closed chain test -verifies-> code
    -implements-> interface -refines-> requirement) and their comparable
    expected values differ. A missing test is a GAP, never a conflict."""
    name = "requirement-test"
    version = "1.0.0"
    categories = {CATEGORY_REQUIREMENT_TEST}

    def _linked_requirements(self, context: ConflictDetectionContext,
                             test_entity_id: str) -> List[str]:
        """Requirement entity ids linked to this test by the closed chain,
        bounded to 3 hops."""
        found: List[str] = []
        seen: Set[str] = {test_entity_id}
        frontier = [test_entity_id]
        allowed = {0: {"verifies"},
                   1: {"implements", "partially-implements", "verifies",
                       "refines"},
                   2: {"refines", "derived-from"}}
        for hop in range(3):
            next_frontier: List[str] = []
            for entity_id in frontier:
                for relation in kg.list_relations(
                        context.workspace_id, source_entity_id=entity_id,
                        lifecycle_status=GraphLifecycleStatus.ACTIVE,
                        limit=200):
                    if relation["relation_type"] not in allowed[hop]:
                        continue
                    other = relation["target_entity_id"]
                    if other in seen:
                        continue
                    seen.add(other)
                    if context.entity_type(other) in _REQUIREMENT_TYPES:
                        found.append(other)
                    else:
                        next_frontier.append(other)
            frontier = next_frontier
            if not frontier:
                break
        return found

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        facts_by_entity: Dict[str, List[ComparableFact]] = {}
        test_entities: Set[str] = set()
        for fact in context.facts:
            if not fact.source_entity_id:
                continue
            facts_by_entity.setdefault(fact.source_entity_id,
                                       []).append(fact)
            if context.entity_type(fact.source_entity_id) in (
                    EntityType.TEST_CASE, EntityType.ACCEPTANCE_CRITERION):
                test_entities.add(fact.source_entity_id)
        for test_id in sorted(test_entities):
            for requirement_id in self._linked_requirements(context,
                                                            test_id):
                for left in facts_by_entity.get(requirement_id, []):
                    for right in facts_by_entity.get(test_id, []):
                        if compare_facts(left, right) == "different":
                            drafts.append(_draft(
                                CATEGORY_REQUIREMENT_TEST, self, left,
                                right,
                                title=(f"requirement and test disagree "
                                       f"on {left.property}"),
                                subject_key=left.subject_key))
                        if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                            return drafts
        return drafts


# ---------------------------------------------------------------------------
# 5. Interface schema
# ---------------------------------------------------------------------------
class InterfaceSchemaDetector(_BaseDetector):
    """Structural interface drift: HTTP method mismatch for the same
    normalized path (documented endpoint vs the canonical interface
    entity), and field data-type mismatches between schema descriptions.
    No deep runtime-compatibility claim is ever made."""
    name = "interface-schema"
    version = "1.0.0"
    categories = {CATEGORY_INTERFACE_SCHEMA}

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        for _key, group in sorted(context.facts_by_group().items()):
            if not group:
                continue
            prop = group[0].property
            if prop == "http-method":
                for left, right in _pairs(group):
                    if left.source_entity_id == right.source_entity_id:
                        continue
                    if compare_facts(left, right) == "different":
                        drafts.append(_draft(
                            CATEGORY_INTERFACE_SCHEMA, self, left, right,
                            title=(f"HTTP method mismatch for "
                                   f"{left.subject_key}")))
                    if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                        return drafts
            elif prop == "data-type":
                for left, right in _pairs(group):
                    if left.source_claim_id == right.source_claim_id:
                        continue
                    if compare_facts(left, right) == "different":
                        drafts.append(_draft(
                            CATEGORY_INTERFACE_SCHEMA, self, left, right,
                            title=(f"data type mismatch for field "
                                   f"{left.subject_key}")))
                    if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                        return drafts
        return drafts


# ---------------------------------------------------------------------------
# 6. Revision authority
# ---------------------------------------------------------------------------
class RevisionAuthorityDetector(_BaseDetector):
    """Multiple active claims share a stable subject+property, at least one
    side is EXPLICITLY authoritative (authority is never inferred), their
    values conflict, and supersession has not resolved it (active claims
    only are compared)."""
    name = "revision-authority"
    version = "1.0.0"
    categories = {CATEGORY_REVISION_AUTHORITY}

    def detect(self, context: ConflictDetectionContext
               ) -> List[ConflictDraft]:
        drafts: List[ConflictDraft] = []
        for _key, group in sorted(context.facts_by_group().items()):
            authoritative = [f for f in group
                             if f.authority_status == "authoritative"]
            if not authoritative:
                continue
            for left in authoritative[:MAX_GROUP_FACTS]:
                for right in group[:MAX_GROUP_FACTS]:
                    if left.source_claim_id == right.source_claim_id:
                        continue
                    if compare_facts(left, right) == "different":
                        draft = _draft(
                            CATEGORY_REVISION_AUTHORITY, self, left,
                            right,
                            title=(f"authoritative value contradicted "
                                   f"for {left.subject_key}"),
                            severity=GapSeverity.CRITICAL)
                        # The authoritative side is disclosed by role.
                        for obj in draft.objects:
                            if obj["object_id"] in (left.source_claim_id,
                                                    left.source_entity_id):
                                obj["role"] = ConflictObjectRole.AUTHORITY
                        drafts.append(draft)
                    if len(drafts) >= MAX_DRAFTS_PER_DETECTOR:
                        return drafts
        return drafts


ALL_DETECTORS: List[_BaseDetector] = [
    DocumentDocumentDetector(),
    RequirementDesignDetector(),
    SpecificationCodeDetector(),
    RequirementTestDetector(),
    InterfaceSchemaDetector(),
    RevisionAuthorityDetector(),
]


def conflict_dedup_key(workspace_id: str, category: str, subject_key: str,
                       object_ids: Sequence[str], property_name: str,
                       detector_name: str) -> str:
    """Deterministic conflict identity (spec §24). Detector VERSION is
    deliberately not part of the key: a detector upgrade must not duplicate
    an unchanged conflict."""
    basis = "\x1e".join([workspace_id, category, subject_key,
                         property_name, detector_name]
                        + sorted(set(object_ids)))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def suppression_fingerprint(draft_values: Sequence[str],
                            evidence_quote_hashes: Sequence[str]) -> str:
    """Ties a dismissal (or an observation) to the exact compared values
    and evidence. A change in either no longer matches."""
    basis = "\x1e".join(sorted(draft_values)
                        + sorted(evidence_quote_hashes))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


__all__ = [
    "SUPPORTED_CATEGORIES", "ALL_DETECTORS",
    "ConflictDetector", "ConflictDetectionContext",
    "DocumentDocumentDetector", "RequirementDesignDetector",
    "SpecificationCodeDetector", "RequirementTestDetector",
    "InterfaceSchemaDetector", "RevisionAuthorityDetector",
    "conflict_dedup_key", "suppression_fingerprint",
    "MAX_GROUP_FACTS", "MAX_DRAFTS_PER_DETECTOR",
]
