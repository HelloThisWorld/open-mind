"""Vocabularies and value objects that cross the semantic-plane boundaries.

Follows the project convention from :mod:`openmind.domain.types`: closed
vocabularies are small classes of string constants with a ``VALUES``
frozenset (validated at write boundaries, no enum ceremony), and dataclasses
are used only for NEW shapes with no pre-existing contract.

Nothing here imports a provider SDK, the database or httpx — these are pure
values, importable from anywhere (including the dependency-free artifact
export path) without side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Revision-status candidate values are exactly the canonical Phase 2
#: vocabulary — aliased, not copied, so the two can never drift. The model
#: may PROPOSE one of these; ``asset_revisions.status`` is never written from
#: model output.
from ..domain.types import RevisionStatus as RevisionStatusVocabulary


# ---------------------------------------------------------------------------
# Data classification (closed, ORDERED — least to most sensitive)
# ---------------------------------------------------------------------------
class DataClassification:
    """What a workspace's content is allowed to touch. A remote provider
    profile declares the MOST sensitive classification it accepts; a workspace
    above that line is blocked before any content is serialized."""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    VALUES = frozenset({PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED})
    #: Ascending sensitivity. Order is the whole point of the vocabulary.
    ORDER = (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)

    @classmethod
    def rank(cls, value: str) -> int:
        """Sensitivity rank; unknown values rank as RESTRICTED (fail closed)."""
        v = str(value or "").strip().lower()
        try:
            return cls.ORDER.index(v)
        except ValueError:
            return len(cls.ORDER) - 1

    @classmethod
    def coerce(cls, value: Any) -> str:
        """Unknown input coerces to RESTRICTED — the safe direction. This is
        the opposite default from most ``coerce`` helpers on purpose: a typo in
        a classification must never make data MORE shareable."""
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.RESTRICTED

    @classmethod
    def allows(cls, profile_max: str, workspace_classification: str) -> bool:
        """True when a profile capped at *profile_max* may process a workspace
        of *workspace_classification*."""
        return cls.rank(workspace_classification) <= cls.rank(profile_max)


# ---------------------------------------------------------------------------
# Provider vocabulary
# ---------------------------------------------------------------------------
class ProviderKind:
    """The closed set of provider adapters this build ships."""
    LOCAL_OPENAI = "local-openai"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    AZURE_OPENAI = "azure-openai"
    MOCK = "mock"
    VALUES = frozenset({LOCAL_OPENAI, OPENAI, ANTHROPIC, AZURE_OPENAI, MOCK})
    #: Kinds that talk to a REMOTE host (HTTPS required, policy-gated).
    REMOTE = frozenset({OPENAI, ANTHROPIC, AZURE_OPENAI})
    #: Kinds that stay on this machine.
    LOCAL = frozenset({LOCAL_OPENAI, MOCK})


class ModelTier:
    """Which of a profile's three configured models a task uses."""
    FAST = "fast"
    STANDARD = "standard"
    STRONG = "strong"
    VALUES = frozenset({FAST, STANDARD, STRONG})

    @classmethod
    def coerce(cls, value: Any) -> str:
        v = str(value or "").strip().lower()
        return v if v in cls.VALUES else cls.STANDARD


class CostSource:
    """Where an ``estimated_cost`` figure came from. ``unknown`` means the
    cost is NULL — never a fabricated zero."""
    PROVIDER = "provider"
    CONFIGURED = "configured"
    UNKNOWN = "unknown"
    VALUES = frozenset({PROVIDER, CONFIGURED, UNKNOWN})


# ---------------------------------------------------------------------------
# Analysis-run vocabulary
# ---------------------------------------------------------------------------
class SemanticRunStatus:
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    VALUES = frozenset({PLANNED, QUEUED, RUNNING, PARTIAL, DONE, FAILED,
                        CANCELLED})
    #: Statuses a run never leaves on its own.
    TERMINAL = frozenset({PARTIAL, DONE, FAILED, CANCELLED})


class TargetStatus:
    """One analysis target (revision × segment × task) — the resumable
    checkpoint unit. ``cached`` is a completed target whose result came from
    the local cache with zero provider calls; resume skips both ``done`` and
    ``cached``."""
    PENDING = "pending"
    DONE = "done"
    CACHED = "cached"
    FAILED = "failed"
    SKIPPED = "skipped"
    VALUES = frozenset({PENDING, DONE, CACHED, FAILED, SKIPPED})
    COMPLETED = frozenset({DONE, CACHED, SKIPPED})


# ---------------------------------------------------------------------------
# Candidate vocabulary
# ---------------------------------------------------------------------------
class SemanticCandidateKind:
    CLASSIFICATION = "classification"
    ENGINEERING_CONCEPT = "engineering-concept"
    REVISION_STATUS = "revision-status"
    VALUES = frozenset({CLASSIFICATION, ENGINEERING_CONCEPT, REVISION_STATUS})


class EngineeringConceptType:
    """The engineering concepts Phase 4 can extract — as candidates only."""
    REQUIREMENT = "requirement"
    BUSINESS_RULE = "business-rule"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    INTERFACE = "interface"
    ACCEPTANCE_CRITERION = "acceptance-criterion"
    FAILURE_MODE = "failure-mode"
    DATA_MODEL = "data-model"
    WORKFLOW = "workflow"
    TEST_CASE = "test-case"
    VALUES = frozenset({REQUIREMENT, BUSINESS_RULE, DECISION, CONSTRAINT,
                        INTERFACE, ACCEPTANCE_CRITERION, FAILURE_MODE,
                        DATA_MODEL, WORKFLOW, TEST_CASE})


class DocumentClassificationType:
    """What KIND of document this appears to be. A proposal about the
    document, never an authority over it."""
    REQUIREMENTS = "requirements"
    BASIC_DESIGN = "basic-design"
    DETAILED_DESIGN = "detailed-design"
    INTERFACE_SPECIFICATION = "interface-specification"
    DATA_DESIGN = "data-design"
    TEST_SPECIFICATION = "test-specification"
    TEST_RESULT = "test-result"
    CHANGE_REQUEST = "change-request"
    INCIDENT_REPORT = "incident-report"
    OPERATION_MANUAL = "operation-manual"
    ARCHITECTURE = "architecture"
    UNKNOWN = "unknown"
    VALUES = frozenset({REQUIREMENTS, BASIC_DESIGN, DETAILED_DESIGN,
                        INTERFACE_SPECIFICATION, DATA_DESIGN,
                        TEST_SPECIFICATION, TEST_RESULT, CHANGE_REQUEST,
                        INCIDENT_REPORT, OPERATION_MANUAL, ARCHITECTURE,
                        UNKNOWN})


class RelationCandidateType:
    """Proposed relations. Every one is stored with candidate status; the
    verified relation vocabulary of the Knowledge Graph is Phase 5."""
    REFINES = "refines"
    IMPLEMENTS = "implements"
    PARTIALLY_IMPLEMENTS = "partially-implements"
    CONFIGURES = "configures"
    VERIFIES = "verifies"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived-from"
    AFFECTED_BY = "affected-by"
    CONTRADICTS = "contradicts"
    POSSIBLY_RELATED = "possibly-related"
    VALUES = frozenset({REFINES, IMPLEMENTS, PARTIALLY_IMPLEMENTS, CONFIGURES,
                        VERIFIES, SUPERSEDES, DERIVED_FROM, AFFECTED_BY,
                        CONTRADICTS, POSSIBLY_RELATED})


class ConflictCategory:
    """What two references appear to disagree about. Never "confirmed"."""
    DOCUMENT_DOCUMENT = "document-document"
    REQUIREMENT_DESIGN = "requirement-design"
    SPECIFICATION_CODE = "specification-code"
    REQUIREMENT_TEST = "requirement-test"
    INTERFACE_SCHEMA = "interface-schema"
    REVISION_AUTHORITY = "revision-authority"
    POSSIBLY_CONFLICTING = "possibly-conflicting"
    VALUES = frozenset({DOCUMENT_DOCUMENT, REQUIREMENT_DESIGN,
                        SPECIFICATION_CODE, REQUIREMENT_TEST, INTERFACE_SCHEMA,
                        REVISION_AUTHORITY, POSSIBLY_CONFLICTING})


class ReviewStatus:
    """Human review of one candidate. ``confirmed`` means "a human considers
    this suitable for later promotion" — it does NOT make the candidate a
    canonical Requirement or Relation."""
    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    VALUES = frozenset({UNREVIEWED, CONFIRMED, REJECTED})


class LifecycleStatus:
    """Whether the candidate still describes the CURRENT revision of its
    sources. Staleness never deletes; a confirmed-but-stale candidate keeps
    its review status and its history."""
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    VALUES = frozenset({ACTIVE, STALE, SUPERSEDED})


class EvidenceVerificationStatus:
    """The locally computed evidence verdict for one candidate."""
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially-verified"
    REJECTED = "rejected"
    VALUES = frozenset({VERIFIED, PARTIALLY_VERIFIED, REJECTED})


class FinalConfidence:
    """LOCALLY derived confidence. The provider's ``confidenceHint`` never
    becomes this value directly — see :mod:`openmind.semantic.verifier`."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    VALUES = frozenset({HIGH, MEDIUM, LOW})


class ReviewDecision:
    CONFIRM = "confirm"
    REJECT = "reject"
    RESET = "reset"
    VALUES = frozenset({CONFIRM, REJECT, RESET})


# ---------------------------------------------------------------------------
# Lens vocabulary
# ---------------------------------------------------------------------------
class LensSource:
    BUILTIN = "builtin"
    ORGANIZATION = "organization"
    INDUCED = "induced"
    VALUES = frozenset({BUILTIN, ORGANIZATION, INDUCED})


class LensStatus:
    PROVISIONAL = "provisional"
    VALIDATED = "validated"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    VALUES = frozenset({PROVISIONAL, VALIDATED, APPROVED, REJECTED, ACTIVE,
                        SUPERSEDED})


class LensValidationResult:
    VALID = "valid"
    VALID_WITH_WARNINGS = "valid-with-warnings"
    INVALID = "invalid"
    VALUES = frozenset({VALID, VALID_WITH_WARNINGS, INVALID})


# ---------------------------------------------------------------------------
# Provider SPI value objects
# ---------------------------------------------------------------------------
@dataclass
class ProviderProfile:
    """Machine-local provider configuration. Carries the NAME of the API-key
    environment variable, never a key value — see providers/profiles.py for
    the storage rules."""
    name: str
    kind: str
    endpoint: str = ""
    api_key_env: str = ""
    models: Dict[str, str] = field(default_factory=dict)   # tier -> model name
    max_data_classification: str = DataClassification.INTERNAL
    timeout: float = 120.0
    max_retries: int = 2
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def model_for_tier(self, tier: str) -> str:
        """The configured model name for a tier, falling back down the tiers
        (strong -> standard -> fast) so a partially configured profile still
        resolves. Empty when nothing is configured — the caller must treat
        that as a configuration error, not invent a default model name."""
        order = {ModelTier.FAST: (ModelTier.FAST, ModelTier.STANDARD,
                                  ModelTier.STRONG),
                 ModelTier.STANDARD: (ModelTier.STANDARD, ModelTier.FAST,
                                      ModelTier.STRONG),
                 ModelTier.STRONG: (ModelTier.STRONG, ModelTier.STANDARD,
                                    ModelTier.FAST)}
        for t in order.get(ModelTier.coerce(tier), ()):
            model = str(self.models.get(t) or "").strip()
            if model:
                return model
        return ""

    @property
    def is_remote(self) -> bool:
        return self.kind in ProviderKind.REMOTE

    def as_dict(self, *, redacted: bool = True) -> Dict[str, Any]:
        """The storable/reportable shape. There is nothing to redact — the
        key value is never held — but ``redacted`` is accepted so callers can
        state their intent."""
        return {
            "name": self.name, "kind": self.kind, "endpoint": self.endpoint,
            "api_key_env": self.api_key_env, "models": dict(self.models),
            "max_data_classification": self.max_data_classification,
            "timeout": self.timeout, "max_retries": self.max_retries,
            "enabled": self.enabled, "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderProfile":
        return cls(
            name=str(data.get("name") or "").strip(),
            kind=str(data.get("kind") or "").strip().lower(),
            endpoint=str(data.get("endpoint") or "").strip(),
            api_key_env=str(data.get("api_key_env") or "").strip(),
            models={str(k): str(v) for k, v in (data.get("models") or {}).items()},
            max_data_classification=DataClassification.coerce(
                data.get("max_data_classification")),
            timeout=float(data.get("timeout") or 120.0),
            max_retries=int(data.get("max_retries") if data.get("max_retries")
                            is not None else 2),
            enabled=bool(data.get("enabled", True)),
            metadata=dict(data.get("metadata") or {}))


@dataclass
class ProviderCapabilities:
    """What one provider adapter can actually do with one profile. Reported
    facts, not aspirations — the runner branches on these."""
    structured_output: bool = False
    json_schema: bool = False
    tool_schema: bool = False
    streaming: bool = False
    token_usage: bool = False
    cached_token_usage: bool = False
    custom_endpoint: bool = False
    local: bool = False
    remote: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "structured_output": self.structured_output,
            "json_schema": self.json_schema,
            "tool_schema": self.tool_schema,
            "streaming": self.streaming,
            "token_usage": self.token_usage,
            "cached_token_usage": self.cached_token_usage,
            "custom_endpoint": self.custom_endpoint,
            "local": self.local, "remote": self.remote,
        }


@dataclass
class ProviderValidation:
    """The outcome of validating a profile WITHOUT any provider call."""
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors),
                "warnings": list(self.warnings)}


@dataclass
class StructuredSchema:
    """A named, versioned JSON schema the provider must return."""
    name: str
    version: str
    json_schema: Dict[str, Any]
    strict: bool = True


@dataclass
class SemanticRequest:
    """One bounded structured-output request. ``input_packet`` is the ONLY
    place project content appears, in the delimited untrusted form built by
    :mod:`openmind.semantic.prompts`."""
    request_id: str
    workspace_id: str
    analysis_run_id: str
    task_type: str
    model_tier: str
    system_instructions: str
    input_packet: Dict[str, Any]
    schema_name: str
    schema_version: str
    prompt_version: str
    max_output_tokens: int
    timeout: float
    idempotency_key: str
    #: The workspace's data classification, carried for the egress audit
    #: record. Informational at this layer — the policy gate has already run
    #: before a request object exists.
    classification: str = ""


@dataclass
class SemanticResponse:
    """A provider's answer. Usage numbers a provider did not report are
    ``None`` — never a fabricated zero, so the ledger can tell "free" from
    "unreported"."""
    request_id: str
    provider_kind: str
    model: str
    structured_output: Dict[str, Any]
    raw_response_hash: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    finish_reason: str = ""
    provider_request_id: str = ""
    retry_count: int = 0
    warnings: List[str] = field(default_factory=list)


__all__ = [
    "RevisionStatusVocabulary",
    "DataClassification", "ProviderKind", "ModelTier", "CostSource",
    "SemanticRunStatus", "TargetStatus",
    "SemanticCandidateKind", "EngineeringConceptType",
    "DocumentClassificationType", "RelationCandidateType", "ConflictCategory",
    "ReviewStatus", "LifecycleStatus", "EvidenceVerificationStatus",
    "FinalConfidence", "ReviewDecision",
    "LensSource", "LensStatus", "LensValidationResult",
    "ProviderProfile", "ProviderCapabilities", "ProviderValidation",
    "StructuredSchema", "SemanticRequest", "SemanticResponse",
]
