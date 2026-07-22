"""Closed vocabularies of the canonical Engineering Knowledge Graph.

Follows the project convention (:mod:`openmind.domain.types`,
:mod:`openmind.semantic.models`): small classes of string constants with a
``VALUES`` frozenset, validated at write boundaries. The graph vocabularies
deliberately do NOT have a forgiving ``coerce`` — an unknown Entity type,
Claim type or Relation type at a write boundary is a typed failure
(:func:`require`), never a silent default. A model- or caller-invented
vocabulary member must not enter canonical storage.

Nothing here imports the database, a provider SDK or the vector store.
"""
from __future__ import annotations

from typing import Iterable

from .errors import UnknownVocabularyValue


def require(vocabulary: "type", value: str, *, field: str) -> str:
    """Validate *value* against a vocabulary class. Returns the normalized
    value or raises the typed error naming the field and the allowed set."""
    clean = str(value or "").strip().lower()
    if clean not in vocabulary.VALUES:
        raise UnknownVocabularyValue(field=field, value=value,
                                     allowed=sorted(vocabulary.VALUES))
    return clean


class EntityType:
    """What kind of engineering concept a canonical Entity is."""
    REQUIREMENT = "requirement"
    BUSINESS_RULE = "business-rule"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    DESIGN = "design"
    INTERFACE = "interface"
    DATA_MODEL = "data-model"
    WORKFLOW = "workflow"
    BATCH_JOB = "batch-job"
    TRANSACTION = "transaction"
    FAILURE_MODE = "failure-mode"
    ACCEPTANCE_CRITERION = "acceptance-criterion"
    TEST_CASE = "test-case"
    TEST_RESULT = "test-result"
    CODE_COMPONENT = "code-component"
    CODE_SYMBOL = "code-symbol"
    CONFIGURATION = "configuration"
    DATABASE_OBJECT = "database-object"
    MESSAGE_TOPIC = "message-topic"
    CHANGE_REQUEST = "change-request"
    INCIDENT = "incident"
    OPERATIONAL_PROCEDURE = "operational-procedure"
    DOCUMENT = "document"
    BUILD_DEFINITION = "build-definition"
    UNKNOWN = "unknown"
    VALUES = frozenset({
        REQUIREMENT, BUSINESS_RULE, DECISION, CONSTRAINT, DESIGN, INTERFACE,
        DATA_MODEL, WORKFLOW, BATCH_JOB, TRANSACTION, FAILURE_MODE,
        ACCEPTANCE_CRITERION, TEST_CASE, TEST_RESULT, CODE_COMPONENT,
        CODE_SYMBOL, CONFIGURATION, DATABASE_OBJECT, MESSAGE_TOPIC,
        CHANGE_REQUEST, INCIDENT, OPERATIONAL_PROCEDURE, DOCUMENT,
        BUILD_DEFINITION, UNKNOWN,
    })

    #: Structural containers the deterministic projector may create WITHOUT a
    #: Claim (their existence is the recorded deterministic fact). The one
    #: documented exception to "an Entity needs Evidence" — everything else
    #: needs at least one Evidence at manual creation.
    STRUCTURAL_CONTAINERS = frozenset({
        CODE_COMPONENT, CODE_SYMBOL, CONFIGURATION, DATABASE_OBJECT,
        DATA_MODEL, DOCUMENT, BUILD_DEFINITION, INTERFACE, MESSAGE_TOPIC,
    })


class ClaimType:
    """What kind of statement a canonical Claim makes about its Entity."""
    DEFINITION = "definition"
    NORMATIVE_STATEMENT = "normative-statement"
    BEHAVIOR = "behavior"
    CONSTRAINT = "constraint"
    DECISION_RATIONALE = "decision-rationale"
    INTERFACE_CONTRACT = "interface-contract"
    DATA_DEFINITION = "data-definition"
    WORKFLOW_STEP = "workflow-step"
    FAILURE_CONDITION = "failure-condition"
    ACCEPTANCE_CONDITION = "acceptance-condition"
    TEST_EXPECTATION = "test-expectation"
    CLASSIFICATION = "classification"
    REVISION_STATUS = "revision-status"
    AUTHORITY = "authority"
    IMPLEMENTATION_NOTE = "implementation-note"
    OPERATIONAL_INSTRUCTION = "operational-instruction"
    UNKNOWN = "unknown"
    VALUES = frozenset({
        DEFINITION, NORMATIVE_STATEMENT, BEHAVIOR, CONSTRAINT,
        DECISION_RATIONALE, INTERFACE_CONTRACT, DATA_DEFINITION,
        WORKFLOW_STEP, FAILURE_CONDITION, ACCEPTANCE_CONDITION,
        TEST_EXPECTATION, CLASSIFICATION, REVISION_STATUS, AUTHORITY,
        IMPLEMENTATION_NOTE, OPERATIONAL_INSTRUCTION, UNKNOWN,
    })


class RelationType:
    """The closed set of canonical Relation types.

    There is deliberately NO ``depends-on``: dependency semantics must be
    expressed through the closest honest member below, or the deterministic
    fact stays outside the graph.
    """
    CONTAINS = "contains"
    SUPERSEDES = "supersedes"
    REFINES = "refines"
    IMPLEMENTS = "implements"
    PARTIALLY_IMPLEMENTS = "partially-implements"
    CONFIGURES = "configures"
    CALLS = "calls"
    READS = "reads"
    WRITES = "writes"
    PUBLISHES = "publishes"
    CONSUMES = "consumes"
    VERIFIES = "verifies"
    EVIDENCED_BY = "evidenced-by"
    CONTRADICTS = "contradicts"
    AFFECTED_BY = "affected-by"
    DERIVED_FROM = "derived-from"
    POSSIBLY_RELATED = "possibly-related"
    VALUES = frozenset({
        CONTAINS, SUPERSEDES, REFINES, IMPLEMENTS, PARTIALLY_IMPLEMENTS,
        CONFIGURES, CALLS, READS, WRITES, PUBLISHES, CONSUMES, VERIFIES,
        EVIDENCED_BY, CONTRADICTS, AFFECTED_BY, DERIVED_FROM,
        POSSIBLY_RELATED,
    })

    #: Types where source == target could ever be meaningful. Empty on
    #: purpose: every current type rejects self-relations.
    SELF_RELATING = frozenset()


class RelationState:
    """How the Relation came to be believed."""
    EXPLICIT = "explicit"       # structurally explicit deterministic fact
    INFERRED = "inferred"       # deterministic analyzer with ambiguity kept
    CONFIRMED = "confirmed"     # a human promoted / created it
    REJECTED = "rejected"       # governance history; excluded from active graph
    STALE = "stale"
    SUPERSEDED = "superseded"
    VALUES = frozenset({EXPLICIT, INFERRED, CONFIRMED, REJECTED, STALE,
                        SUPERSEDED})
    #: States that participate in the active graph.
    ACTIVE = frozenset({EXPLICIT, INFERRED, CONFIRMED})
    #: States a manual `relation create` may set. ``inferred`` needs a
    #: documented analyzer origin, which a manual caller does not have.
    MANUAL = frozenset({EXPLICIT, CONFIRMED})


class GraphLifecycleStatus:
    """Lifecycle of every canonical graph object."""
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    MERGED = "merged"
    VALUES = frozenset({ACTIVE, STALE, SUPERSEDED, WITHDRAWN, MERGED})


class AuthorityStatus:
    """Explicit human authority judgement. Never inferred from names,
    timestamps or model output."""
    UNKNOWN = "unknown"
    INFORMATIONAL = "informational"
    AUTHORITATIVE = "authoritative"
    NON_AUTHORITATIVE = "non-authoritative"
    VALUES = frozenset({UNKNOWN, INFORMATIONAL, AUTHORITATIVE,
                        NON_AUTHORITATIVE})


class GraphOrigin:
    """Which of the permitted write paths created the object."""
    DETERMINISTIC = "deterministic"
    SEMANTIC_PROMOTION = "semantic-promotion"
    MANUAL = "manual"
    MIGRATION = "migration"
    VALUES = frozenset({DETERMINISTIC, SEMANTIC_PROMOTION, MANUAL, MIGRATION})


class AliasType:
    IDENTIFIER = "identifier"
    NAME = "name"
    ACRONYM = "acronym"
    LEGACY_NAME = "legacy-name"
    PATH = "path"
    SYMBOL = "symbol"
    MANUAL = "manual"
    VALUES = frozenset({IDENTIFIER, NAME, ACRONYM, LEGACY_NAME, PATH, SYMBOL,
                        MANUAL})


class AliasStatus:
    ACTIVE = "active"
    REMOVED = "removed"
    SUPERSEDED = "superseded"
    VALUES = frozenset({ACTIVE, REMOVED, SUPERSEDED})


class BindingRefKind:
    """What source-plane object a binding points at."""
    ASSET = "asset"
    REVISION = "revision"
    SEGMENT = "segment"
    EVIDENCE = "evidence"
    DOCUMENT_BLOCK = "document-block"
    CODE_SYMBOL = "code-symbol"
    CONFIGURATION_KEY = "configuration-key"
    DATABASE_OBJECT = "database-object"
    API_OPERATION = "api-operation"
    MESSAGE_TOPIC = "message-topic"
    VALUES = frozenset({ASSET, REVISION, SEGMENT, EVIDENCE, DOCUMENT_BLOCK,
                        CODE_SYMBOL, CONFIGURATION_KEY, DATABASE_OBJECT,
                        API_OPERATION, MESSAGE_TOPIC})


class BindingRole:
    PRIMARY_SOURCE = "primary-source"
    DEFINITION_SOURCE = "definition-source"
    IMPLEMENTATION = "implementation"
    CONFIGURATION = "configuration"
    TEST = "test"
    INTERFACE = "interface"
    SUPPORTING = "supporting"
    HISTORICAL = "historical"
    VALUES = frozenset({PRIMARY_SOURCE, DEFINITION_SOURCE, IMPLEMENTATION,
                        CONFIGURATION, TEST, INTERFACE, SUPPORTING,
                        HISTORICAL})


class BindingStatus:
    ACTIVE = "active"
    STALE = "stale"
    REMOVED = "removed"
    VALUES = frozenset({ACTIVE, STALE, REMOVED})


class ClaimEvidenceRole:
    PRIMARY = "primary"
    SUPPORTING = "supporting"
    CONTEXT = "context"
    AUTHORITY = "authority"
    VALUES = frozenset({PRIMARY, SUPPORTING, CONTEXT, AUTHORITY})


class DecisionType:
    """Every governance write records exactly one of these.

    The ``CONFLICT_*``, ``GAP_*`` and ``TRACE_POLICY_CHANGE`` members were
    added in Phase 6 (additively — nothing existing renamed): conflict and
    gap governance and traceability-policy selection are canonical
    governance actions and must be auditable in the same ledger as every
    other graph decision.
    """
    PROMOTE_CANDIDATE = "promote-candidate"
    PROMOTE_RELATION = "promote-relation"
    CREATE_ENTITY = "create-entity"
    CREATE_CLAIM = "create-claim"
    CREATE_RELATION = "create-relation"
    ADD_ALIAS = "add-alias"
    REMOVE_ALIAS = "remove-alias"
    MERGE_ENTITY = "merge-entity"
    SPLIT_ENTITY = "split-entity"
    MARK_AUTHORITATIVE = "mark-authoritative"
    MARK_NON_AUTHORITATIVE = "mark-non-authoritative"
    SUPERSEDE = "supersede"
    WITHDRAW = "withdraw"
    REJECT_RELATION = "reject-relation"
    RESTORE_RELATION = "restore-relation"
    CONFLICT_DETECT = "conflict-detect"
    CONFLICT_PROMOTE = "conflict-promote"
    CONFLICT_REVIEW = "conflict-review"
    CONFLICT_ACCEPT_RISK = "conflict-accept-risk"
    CONFLICT_RESOLVE = "conflict-resolve"
    CONFLICT_DISMISS = "conflict-dismiss"
    CONFLICT_REOPEN = "conflict-reopen"
    CONFLICT_SUPERSEDE = "conflict-supersede"
    GAP_RESOLVE = "gap-resolve"
    GAP_ACCEPT = "gap-accept"
    GAP_DISMISS = "gap-dismiss"
    GAP_REOPEN = "gap-reopen"
    TRACE_POLICY_CHANGE = "trace-policy-change"
    VALUES = frozenset({
        PROMOTE_CANDIDATE, PROMOTE_RELATION, CREATE_ENTITY, CREATE_CLAIM,
        CREATE_RELATION, ADD_ALIAS, REMOVE_ALIAS, MERGE_ENTITY, SPLIT_ENTITY,
        MARK_AUTHORITATIVE, MARK_NON_AUTHORITATIVE, SUPERSEDE, WITHDRAW,
        REJECT_RELATION, RESTORE_RELATION,
        CONFLICT_DETECT, CONFLICT_PROMOTE, CONFLICT_REVIEW,
        CONFLICT_ACCEPT_RISK, CONFLICT_RESOLVE, CONFLICT_DISMISS,
        CONFLICT_REOPEN, CONFLICT_SUPERSEDE,
        GAP_RESOLVE, GAP_ACCEPT, GAP_DISMISS, GAP_REOPEN,
        TRACE_POLICY_CHANGE,
    })


class DecisionTargetKind:
    ENTITY = "entity"
    CLAIM = "claim"
    RELATION = "relation"
    ALIAS = "alias"
    BINDING = "binding"
    CANDIDATE = "candidate"
    RELATION_CANDIDATE = "relation-candidate"
    WORKSPACE = "workspace"
    CONFLICT = "conflict"                       # Phase 6, additive
    CONFLICT_CANDIDATE = "conflict-candidate"   # Phase 6, additive
    GAP = "gap"                                 # Phase 6, additive
    VALUES = frozenset({ENTITY, CLAIM, RELATION, ALIAS, BINDING, CANDIDATE,
                        RELATION_CANDIDATE, WORKSPACE, CONFLICT,
                        CONFLICT_CANDIDATE, GAP})


class RevisionAction:
    """What one Knowledge Revision recorded. Not a closed write-gate (a
    bounded free label would also be safe) but kept closed for honest
    reporting."""
    GRAPH_SEED = "graph-seed"
    GRAPH_SYNC = "graph-sync"
    GRAPH_RECONCILE = "graph-reconcile"
    CANDIDATE_PROMOTION = "candidate-promotion"
    RELATION_PROMOTION = "relation-promotion"
    MANUAL_ENTITY_CREATE = "manual-entity-create"
    MANUAL_CLAIM_CREATE = "manual-claim-create"
    MANUAL_RELATION_CREATE = "manual-relation-create"
    ALIAS_CHANGE = "alias-change"
    ENTITY_MERGE = "entity-merge"
    ENTITY_SPLIT = "entity-split"
    CLAIM_SUPERSEDE = "claim-supersede"
    SUPERSEDE = "supersede"
    WITHDRAW = "withdraw"
    AUTHORITY_CHANGE = "authority-change"
    RELATION_STATE_CHANGE = "relation-state-change"
    CONFLICT_SCAN = "conflict-scan"                     # Phase 6, additive
    CONFLICT_PROMOTION = "conflict-promotion"           # Phase 6, additive
    CONFLICT_GOVERNANCE = "conflict-governance"         # Phase 6, additive
    GAP_GOVERNANCE = "gap-governance"                   # Phase 6, additive
    TRACE_POLICY_CHANGE = "trace-policy-change"         # Phase 6, additive
    VALUES = frozenset({
        GRAPH_SEED, GRAPH_SYNC, GRAPH_RECONCILE, CANDIDATE_PROMOTION,
        RELATION_PROMOTION, MANUAL_ENTITY_CREATE, MANUAL_CLAIM_CREATE,
        MANUAL_RELATION_CREATE, ALIAS_CHANGE, ENTITY_MERGE, ENTITY_SPLIT,
        CLAIM_SUPERSEDE, SUPERSEDE, WITHDRAW, AUTHORITY_CHANGE,
        RELATION_STATE_CHANGE, CONFLICT_SCAN, CONFLICT_PROMOTION,
        CONFLICT_GOVERNANCE, GAP_GOVERNANCE, TRACE_POLICY_CHANGE,
    })


class PromotionCandidateKind:
    SEMANTIC_CANDIDATE = "semantic-candidate"
    RELATION_CANDIDATE = "relation-candidate"
    CONFLICT_CANDIDATE = "conflict-candidate"   # Phase 6, additive
    VALUES = frozenset({SEMANTIC_CANDIDATE, RELATION_CANDIDATE,
                        CONFLICT_CANDIDATE})


class PromotionStatus:
    PROMOTED = "promoted"
    BLOCKED = "blocked"
    ALREADY_PROMOTED = "already-promoted"
    VALUES = frozenset({PROMOTED, BLOCKED, ALREADY_PROMOTED})


class PromotionExpectedAction:
    """What a promotion PLAN reports would happen."""
    CREATE_ENTITY_AND_CLAIM = "create-entity-and-claim"
    ATTACH_CLAIM_TO_EXISTING_ENTITY = "attach-claim-to-existing-entity"
    CREATE_RELATION = "create-relation"
    ALREADY_PROMOTED = "already-promoted"
    BLOCKED = "blocked"
    IDENTITY_CONFLICT = "identity-conflict"
    VALUES = frozenset({CREATE_ENTITY_AND_CLAIM,
                        ATTACH_CLAIM_TO_EXISTING_ENTITY, CREATE_RELATION,
                        ALREADY_PROMOTED, BLOCKED, IDENTITY_CONFLICT})


class GraphNodeKind:
    """Node kinds of the read-graph abstraction. Source-plane kinds are
    PROJECTED from their canonical Phase 2 rows, never copied into graph
    tables."""
    ENTITY = "entity"
    CLAIM = "claim"
    ASSET = "asset"
    REVISION = "revision"
    SEGMENT = "segment"
    EVIDENCE = "evidence"
    VALUES = frozenset({ENTITY, CLAIM, ASSET, REVISION, SEGMENT, EVIDENCE})


#: Mapping from Phase 4 engineering-concept candidate types to canonical
#: Entity types (identical strings today, but the mapping is explicit so a
#: future rename on either side is a visible decision, not an accident).
CONCEPT_TYPE_TO_ENTITY_TYPE = {
    "requirement": EntityType.REQUIREMENT,
    "business-rule": EntityType.BUSINESS_RULE,
    "decision": EntityType.DECISION,
    "constraint": EntityType.CONSTRAINT,
    "interface": EntityType.INTERFACE,
    "acceptance-criterion": EntityType.ACCEPTANCE_CRITERION,
    "failure-mode": EntityType.FAILURE_MODE,
    "data-model": EntityType.DATA_MODEL,
    "workflow": EntityType.WORKFLOW,
    "test-case": EntityType.TEST_CASE,
}

#: Default Claim type for a promoted engineering concept, by Entity type.
ENTITY_TYPE_TO_CLAIM_TYPE = {
    EntityType.REQUIREMENT: ClaimType.NORMATIVE_STATEMENT,
    EntityType.BUSINESS_RULE: ClaimType.NORMATIVE_STATEMENT,
    EntityType.DECISION: ClaimType.DECISION_RATIONALE,
    EntityType.CONSTRAINT: ClaimType.CONSTRAINT,
    EntityType.INTERFACE: ClaimType.INTERFACE_CONTRACT,
    EntityType.ACCEPTANCE_CRITERION: ClaimType.ACCEPTANCE_CONDITION,
    EntityType.FAILURE_MODE: ClaimType.FAILURE_CONDITION,
    EntityType.DATA_MODEL: ClaimType.DATA_DEFINITION,
    EntityType.WORKFLOW: ClaimType.WORKFLOW_STEP,
    EntityType.TEST_CASE: ClaimType.TEST_EXPECTATION,
}

#: Phase 4 relation-candidate types that map onto canonical relation types.
#: Identical strings — the explicit table exists for the same reason as above.
RELATION_CANDIDATE_TYPE_TO_RELATION_TYPE = {
    "refines": RelationType.REFINES,
    "implements": RelationType.IMPLEMENTS,
    "partially-implements": RelationType.PARTIALLY_IMPLEMENTS,
    "configures": RelationType.CONFIGURES,
    "verifies": RelationType.VERIFIES,
    "supersedes": RelationType.SUPERSEDES,
    "derived-from": RelationType.DERIVED_FROM,
    "affected-by": RelationType.AFFECTED_BY,
    "contradicts": RelationType.CONTRADICTS,
    "possibly-related": RelationType.POSSIBLY_RELATED,
}


__all__ = [
    "require",
    "EntityType", "ClaimType", "RelationType", "RelationState",
    "GraphLifecycleStatus", "AuthorityStatus", "GraphOrigin",
    "AliasType", "AliasStatus", "BindingRefKind", "BindingRole",
    "BindingStatus", "ClaimEvidenceRole", "DecisionType",
    "DecisionTargetKind", "RevisionAction", "PromotionCandidateKind",
    "PromotionStatus", "PromotionExpectedAction", "GraphNodeKind",
    "CONCEPT_TYPE_TO_ENTITY_TYPE", "ENTITY_TYPE_TO_CLAIM_TYPE",
    "RELATION_CANDIDATE_TYPE_TO_RELATION_TYPE",
]
