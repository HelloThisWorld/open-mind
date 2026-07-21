"""The closed, versioned semantic task registry.

Every analysis verb OpenMind can ask a model to perform is declared here —
its schema, prompt version, default tier, what it may run over, its bounds
and its verification policy. Nothing outside this registry can be requested:
the planner iterates it, the CLI validates ``--tasks`` against it, and the
runner refuses a task type it does not contain.

Versioning: ``task_version`` and ``prompt_version`` are recorded on every
run and candidate. Changing a prompt's TEXT requires a NEW version module
under ``prompt_texts/`` (the hash of the released text is part of the cache
key, so an in-place edit would be caught as a cache-behavior change and, more
importantly, would silently re-interpret recorded provenance — hence the
rule).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

from ..domain.types import AssetType, DocumentBlockType
from .models import (DocumentClassificationType, EngineeringConceptType,
                     ModelTier, RevisionStatusVocabulary,
                     SemanticCandidateKind)

#: Segment types that carry analyzable document prose.
_DOC_BLOCKS: FrozenSet[str] = frozenset(
    DocumentBlockType.VALUES - DocumentBlockType.CONTAINERS)
#: Code segment types (Phase 2 vocabulary).
_CODE_SEGMENTS: FrozenSet[str] = frozenset({"type", "method", "constructor",
                                            "file"})


@dataclass(frozen=True)
class TaskDefinition:
    task_type: str
    task_version: str
    prompt_version: str
    schema_name: str
    schema_version: str
    default_model_tier: str
    supported_asset_types: FrozenSet[str]
    supported_segment_types: FrozenSet[str]
    max_evidence_items: int
    max_input_tokens: int
    max_output_tokens: int
    verification_policy: str          # 'evidence-required' | 'deterministic'
    #: For extraction tasks: which candidate kind rows are written, and which
    #: candidate types the schema vocabulary is narrowed to.
    candidate_kind: str = ""
    allowed_candidate_types: FrozenSet[str] = field(default_factory=frozenset)
    #: Run granularity: 'segment' (one target per segment), 'revision' (one
    #: target per revision), 'pair' (bounded candidate pairs), 'workspace'.
    granularity: str = "segment"

    def as_dict(self) -> Dict[str, object]:
        return {
            "task_type": self.task_type,
            "task_version": self.task_version,
            "prompt_version": self.prompt_version,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "default_model_tier": self.default_model_tier,
            "supported_asset_types": sorted(self.supported_asset_types),
            "supported_segment_types": sorted(self.supported_segment_types),
            "max_evidence_items": self.max_evidence_items,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "verification_policy": self.verification_policy,
            "candidate_kind": self.candidate_kind,
            "allowed_candidate_types": sorted(self.allowed_candidate_types),
            "granularity": self.granularity,
        }


def _concept_task(task_type: str, concept: str, *,
                  asset_types: FrozenSet[str],
                  segment_types: FrozenSet[str],
                  tier: str = ModelTier.STANDARD) -> TaskDefinition:
    return TaskDefinition(
        task_type=task_type, task_version="1", prompt_version="1",
        schema_name="engineering-candidates", schema_version="1",
        default_model_tier=tier,
        supported_asset_types=asset_types,
        supported_segment_types=segment_types,
        max_evidence_items=6, max_input_tokens=8_000, max_output_tokens=2_000,
        verification_policy="evidence-required",
        candidate_kind=SemanticCandidateKind.ENGINEERING_CONCEPT,
        allowed_candidate_types=frozenset({concept}),
        granularity="segment")


_DOCS = frozenset({AssetType.DOCUMENT, AssetType.DOCUMENTATION_TEXT})

REGISTRY: Dict[str, TaskDefinition] = {t.task_type: t for t in (
    TaskDefinition(
        task_type="document-classification", task_version="1",
        prompt_version="1", schema_name="document-classification",
        schema_version="1", default_model_tier=ModelTier.FAST,
        supported_asset_types=_DOCS,
        supported_segment_types=frozenset({DocumentBlockType.DOCUMENT}),
        max_evidence_items=4, max_input_tokens=6_000, max_output_tokens=800,
        verification_policy="evidence-required",
        candidate_kind=SemanticCandidateKind.CLASSIFICATION,
        allowed_candidate_types=DocumentClassificationType.VALUES,
        granularity="revision"),
    _concept_task("requirement-extraction",
                  EngineeringConceptType.REQUIREMENT,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("business-rule-extraction",
                  EngineeringConceptType.BUSINESS_RULE,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("decision-extraction", EngineeringConceptType.DECISION,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("constraint-extraction", EngineeringConceptType.CONSTRAINT,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("interface-extraction", EngineeringConceptType.INTERFACE,
                  asset_types=_DOCS | frozenset({AssetType.SOURCE_CODE,
                                                 AssetType.CONFIGURATION}),
                  segment_types=_DOC_BLOCKS | _CODE_SEGMENTS),
    _concept_task("acceptance-criterion-extraction",
                  EngineeringConceptType.ACCEPTANCE_CRITERION,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("failure-mode-extraction",
                  EngineeringConceptType.FAILURE_MODE,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("data-model-extraction", EngineeringConceptType.DATA_MODEL,
                  asset_types=_DOCS | frozenset({AssetType.DATABASE_SCHEMA}),
                  segment_types=_DOC_BLOCKS | frozenset({"sql-object"})),
    _concept_task("workflow-extraction", EngineeringConceptType.WORKFLOW,
                  asset_types=_DOCS, segment_types=_DOC_BLOCKS),
    _concept_task("test-case-extraction", EngineeringConceptType.TEST_CASE,
                  asset_types=_DOCS | frozenset({AssetType.TEST_SOURCE}),
                  segment_types=_DOC_BLOCKS | _CODE_SEGMENTS),
    TaskDefinition(
        task_type="revision-status-inference", task_version="1",
        prompt_version="1", schema_name="revision-status",
        schema_version="1", default_model_tier=ModelTier.FAST,
        supported_asset_types=_DOCS,
        supported_segment_types=frozenset({DocumentBlockType.DOCUMENT}),
        max_evidence_items=4, max_input_tokens=4_000, max_output_tokens=600,
        verification_policy="evidence-required",
        candidate_kind=SemanticCandidateKind.REVISION_STATUS,
        allowed_candidate_types=RevisionStatusVocabulary.VALUES,
        granularity="revision"),
    TaskDefinition(
        task_type="relation-candidate-analysis", task_version="1",
        prompt_version="1", schema_name="relation-candidates",
        schema_version="1", default_model_tier=ModelTier.STANDARD,
        supported_asset_types=frozenset(), supported_segment_types=frozenset(),
        max_evidence_items=8, max_input_tokens=8_000, max_output_tokens=2_000,
        verification_policy="evidence-required", granularity="pair"),
    TaskDefinition(
        task_type="conflict-candidate-analysis", task_version="1",
        prompt_version="1", schema_name="conflict-candidates",
        schema_version="1", default_model_tier=ModelTier.STRONG,
        supported_asset_types=frozenset(), supported_segment_types=frozenset(),
        max_evidence_items=8, max_input_tokens=8_000, max_output_tokens=2_000,
        verification_policy="evidence-required", granularity="pair"),
    TaskDefinition(
        task_type="project-lens-induction", task_version="1",
        prompt_version="1", schema_name="project-lens",
        schema_version="2.0.0", default_model_tier=ModelTier.STRONG,
        supported_asset_types=frozenset(), supported_segment_types=frozenset(),
        max_evidence_items=64, max_input_tokens=24_000,
        max_output_tokens=6_000, verification_policy="deterministic",
        granularity="workspace"),
)}

#: Task types runnable through `semantic plan/analyze` (lens induction has
#: its own dedicated verbs and is excluded from the generic analysis set).
ANALYSIS_TASK_TYPES: List[str] = [t for t in REGISTRY
                                  if t != "project-lens-induction"]

#: The default task set when the caller names none: per-segment extraction
#: over documents. Relation/conflict analysis is opt-in because it multiplies
#: provider requests.
DEFAULT_TASK_TYPES: List[str] = ["document-classification",
                                 "requirement-extraction"]


def get_task(task_type: str) -> Optional[TaskDefinition]:
    return REGISTRY.get(str(task_type or "").strip().lower())


def require_task(task_type: str) -> TaskDefinition:
    task = get_task(task_type)
    if task is None:
        from .errors import ProviderConfigurationError
        raise ProviderConfigurationError(
            f"unknown semantic task: {task_type!r}",
            details={"available": sorted(REGISTRY)})
    return task


def list_tasks() -> List[Dict[str, object]]:
    return [REGISTRY[name].as_dict() for name in sorted(REGISTRY)]


__all__ = ["TaskDefinition", "REGISTRY", "ANALYSIS_TASK_TYPES",
           "DEFAULT_TASK_TYPES", "get_task", "require_task", "list_tasks"]
