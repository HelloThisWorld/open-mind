"""The deterministic trace engine: policy-validated path building over the
canonical graph.

This module is a PURE READER of Phase 5 state. It walks only relations whose
type is allowed by a policy transition between two correctly-staged
entities — a generic graph path (``possibly-related``, ``calls``,
``contains`` chains) never becomes a formal trace here, because those edge
types simply are not adjacency unless a transition lists them.

No provider is called; confidence and completeness are computed from
deterministic facts; a missing link is returned as a gap, never invented.

Everything is bounded: depth by ``min(policy.maximumDepth,
MAX_TRACE_DEPTH)``, chains per root, visited entities per root, relations
per entity. Every truncation is disclosed in the result.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import db
from ..knowledge import store as kg
from ..knowledge.vocabularies import (EntityType, GraphLifecycleStatus,
                                      GraphOrigin, RelationState)
from . import TRACE_ENGINE_VERSION
from .errors import TraceRootIneligible
from .models import TraceabilityPolicy
from .vocabularies import (GapType, StepDirection, StepEvidenceStatus,
                           TraceConfidence, TracePathKind, TracePathStatus,
                           TraceStage)

MAX_TRACE_DEPTH = 10
MAX_CHAINS_PER_ROOT = 200
MAX_VISITED_PER_ROOT = 2000
MAX_RELATIONS_PER_ENTITY = 500
MAX_PATHS_PER_KIND = 10
HARD_MAX_PATHS_PER_KIND = 50

#: Which persisted path kind a chain's terminal stage serves. ``operation``
#: has no dedicated kind; operational hops are reported inside the result
#: metadata only.
STAGE_TO_KIND = {
    TraceStage.DESIGN: TracePathKind.REQUIREMENT_TO_DESIGN,
    TraceStage.WORKFLOW: TracePathKind.REQUIREMENT_TO_DESIGN,
    TraceStage.INTERFACE: TracePathKind.REQUIREMENT_TO_INTERFACE,
    TraceStage.DATA: TracePathKind.REQUIREMENT_TO_INTERFACE,
    TraceStage.IMPLEMENTATION: TracePathKind.REQUIREMENT_TO_IMPLEMENTATION,
    TraceStage.CONFIGURATION: TracePathKind.REQUIREMENT_TO_IMPLEMENTATION,
    TraceStage.VERIFICATION: TracePathKind.REQUIREMENT_TO_TEST,
    TraceStage.TEST_RESULT: TracePathKind.REQUIREMENT_TO_EVIDENCE,
    TraceStage.EVIDENCE: TracePathKind.REQUIREMENT_TO_EVIDENCE,
}

#: Entity types accepted by the reverse Code trace.
CODE_TRACE_TYPES = frozenset({
    EntityType.CODE_COMPONENT, EntityType.CODE_SYMBOL,
    EntityType.CONFIGURATION, EntityType.DATABASE_OBJECT,
    EntityType.MESSAGE_TOPIC,
})

#: Entity types accepted by the reverse Test trace.
TEST_TRACE_TYPES = frozenset({EntityType.TEST_CASE, EntityType.TEST_RESULT})


# ---------------------------------------------------------------------------
# Shared per-walk caches
# ---------------------------------------------------------------------------
class _WalkContext:
    """One trace computation's caches: entities, relations, evidence
    verdicts and the workspace's current-revision set. Everything read at
    most once per walk."""

    def __init__(self, workspace_id: str, policy: TraceabilityPolicy,
                 *, include_stale: bool = False) -> None:
        self.workspace_id = workspace_id
        self.policy = policy
        self.include_stale = include_stale
        self.current_revisions = kg.current_revision_ids(workspace_id)
        self._entities: Dict[str, Optional[Dict[str, Any]]] = {}
        self._relations: Dict[str, List[Dict[str, Any]]] = {}
        self._rel_evidence: Dict[str, str] = {}
        self._entity_evidence: Dict[str, str] = {}
        self._claims: Dict[str, List[Dict[str, Any]]] = {}
        self.visited_budget = MAX_VISITED_PER_ROOT
        self.truncated = False

    # -- entities -----------------------------------------------------------
    def entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        if entity_id not in self._entities:
            self._entities[entity_id] = kg.get_entity(self.workspace_id,
                                                      entity_id)
        return self._entities[entity_id]

    def stage_of(self, entity: Dict[str, Any]) -> Optional[str]:
        return self.policy.stage_of_entity_type(entity["entity_type"])

    # -- relations ----------------------------------------------------------
    def relations_of(self, entity_id: str) -> List[Dict[str, Any]]:
        """Every non-rejected relation touching the entity, both
        directions, deterministic order, bounded."""
        if entity_id not in self._relations:
            rows = kg.list_relations(
                self.workspace_id, entity_id=entity_id,
                lifecycle_status=None, limit=MAX_RELATIONS_PER_ENTITY)
            if len(rows) >= MAX_RELATIONS_PER_ENTITY:
                self.truncated = True
            rows = [r for r in rows
                    if r["relation_state"] != RelationState.REJECTED]
            rows.sort(key=lambda r: (r["relation_type"],
                                     r["source_entity_id"],
                                     r["target_entity_id"], r["id"]))
            self._relations[entity_id] = rows
        return self._relations[entity_id]

    def relation_usable(self, relation: Dict[str, Any]) -> bool:
        if self.include_stale:
            return True
        return relation["lifecycle_status"] == GraphLifecycleStatus.ACTIVE

    def entity_usable(self, entity: Dict[str, Any]) -> bool:
        if self.include_stale:
            return entity["lifecycle_status"] not in (
                GraphLifecycleStatus.WITHDRAWN, GraphLifecycleStatus.MERGED)
        return entity["lifecycle_status"] == GraphLifecycleStatus.ACTIVE

    # -- evidence -----------------------------------------------------------
    def _evidence_current(self, evidence_id: str) -> Optional[bool]:
        """True current / False stale / None unresolvable."""
        record = db.get_evidence(self.workspace_id, evidence_id)
        if not record:
            return None
        return record.get("revision_id") in self.current_revisions

    def relation_evidence_status(self, relation: Dict[str, Any]) -> str:
        rid = relation["id"]
        if rid not in self._rel_evidence:
            joins = kg.relation_evidence(rid)
            if not joins:
                # A deterministic explicit projection edge is anchored by
                # its endpoints' bindings; reconciliation stales it when
                # sources move. Its own active lifecycle stands in.
                if relation["origin"] == GraphOrigin.DETERMINISTIC and \
                        relation["relation_state"] == RelationState.EXPLICIT:
                    verdict = StepEvidenceStatus.CURRENT
                else:
                    verdict = StepEvidenceStatus.MISSING
            else:
                flags = [self._evidence_current(j["evidence_id"])
                         for j in joins]
                if any(f is True for f in flags):
                    verdict = StepEvidenceStatus.CURRENT
                elif any(f is False for f in flags):
                    verdict = StepEvidenceStatus.STALE
                else:
                    verdict = StepEvidenceStatus.MISSING
            self._rel_evidence[rid] = verdict
        return self._rel_evidence[rid]

    def claims_of(self, entity_id: str) -> List[Dict[str, Any]]:
        if entity_id not in self._claims:
            self._claims[entity_id] = kg.list_claims(
                self.workspace_id, entity_id=entity_id,
                lifecycle_status=GraphLifecycleStatus.ACTIVE, limit=200)
        return self._claims[entity_id]

    def entity_evidence_status(self, entity_id: str) -> str:
        """Evidence verdict for one entity: current when at least one
        active claim's evidence join (or one active evidence binding)
        resolves to a current revision."""
        if entity_id in self._entity_evidence:
            return self._entity_evidence[entity_id]
        best = StepEvidenceStatus.MISSING
        for claim in self.claims_of(entity_id):
            for join in kg.claim_evidence(claim["id"]):
                flag = self._evidence_current(join["evidence_id"])
                if flag is True:
                    best = StepEvidenceStatus.CURRENT
                    break
                if flag is False and best == StepEvidenceStatus.MISSING:
                    best = StepEvidenceStatus.STALE
            if best == StepEvidenceStatus.CURRENT:
                break
        if best != StepEvidenceStatus.CURRENT:
            for binding in kg.list_bindings(self.workspace_id, entity_id,
                                            status="active"):
                if binding["ref_kind"] != "evidence":
                    continue
                flag = self._evidence_current(binding["ref_id"])
                if flag is True:
                    best = StepEvidenceStatus.CURRENT
                    break
                if flag is False and best == StepEvidenceStatus.MISSING:
                    best = StepEvidenceStatus.STALE
        self._entity_evidence[entity_id] = best
        return best


# ---------------------------------------------------------------------------
# Stage-legal adjacency
# ---------------------------------------------------------------------------
def _stage_edges(ctx: _WalkContext, entity: Dict[str, Any], stage: str,
                 *, reverse: bool = False
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(usable_hops, rejected_edges) from *entity* at *stage*.

    A usable hop is {relation, other, other_stage, transition, direction}.
    ``reverse=True`` walks transitions backward (to-stage -> from-stage) —
    the upstream direction of the reverse Code/Test traces.

    ``rejected_edges`` are edges between correctly-staged endpoints whose
    relation TYPE is not allowed for the transition (``possibly-related``
    where ``implements`` is required, a bare ``calls``, ...) — surfaced so
    the caller can report an unsupported-relation gap instead of silently
    hiding why a stage looks unreachable.
    """
    transitions = (ctx.policy.transitions_to(stage) if reverse
                   else ctx.policy.transitions_from(stage))
    hops: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    if not transitions:
        return hops, rejected
    by_next_stage: Dict[str, Set[str]] = {}
    for t in transitions:
        next_stage = t.from_stage if reverse else t.to_stage
        by_next_stage.setdefault(next_stage, set()).update(t.relation_types)

    for relation in ctx.relations_of(entity["id"]):
        if not ctx.relation_usable(relation):
            continue
        other_id = (relation["target_entity_id"]
                    if relation["source_entity_id"] == entity["id"]
                    else relation["source_entity_id"])
        if other_id == entity["id"]:
            continue
        other = ctx.entity(other_id)
        if not other or not ctx.entity_usable(other):
            continue
        other_stage = ctx.stage_of(other)
        if other_stage is None or other_stage not in by_next_stage:
            continue
        allowed = by_next_stage[other_stage]
        relation_type = relation["relation_type"]
        possibly = relation_type == "possibly-related"
        type_ok = relation_type in allowed and (
            not possibly or ctx.policy.rules.allow_possibly_related)
        if not type_ok:
            rejected.append({
                "relation_id": relation["id"],
                "relation_type": relation_type,
                "relation_state": relation["relation_state"],
                "from_entity_id": entity["id"],
                "to_entity_id": other_id,
                "to_stage": other_stage,
                "reason": ("possibly-related cannot satisfy this "
                           "transition" if possibly else
                           f"relation type {relation_type!r} is not allowed "
                           f"for {stage} -> {other_stage}"),
            })
            continue
        if relation["relation_state"] == RelationState.INFERRED and \
                not ctx.policy.rules.allow_inferred_relations:
            rejected.append({
                "relation_id": relation["id"],
                "relation_type": relation_type,
                "relation_state": relation["relation_state"],
                "from_entity_id": entity["id"],
                "to_entity_id": other_id,
                "to_stage": other_stage,
                "reason": "inferred relations are not allowed by policy",
            })
            continue
        direction = (StepDirection.FORWARD
                     if relation["source_entity_id"] == entity["id"]
                     else StepDirection.REVERSE)
        if reverse:
            direction = (StepDirection.REVERSE
                         if direction == StepDirection.FORWARD
                         else StepDirection.FORWARD)
        hops.append({"relation": relation, "other": other,
                     "other_stage": other_stage, "direction": direction})
    hops.sort(key=lambda h: (h["other_stage"], h["other"]["id"],
                             h["relation"]["id"]))
    return hops, rejected


def _ambiguous_hop_groups(hops: Sequence[Dict[str, Any]]) -> Set[str]:
    """Stages reachable from this entity ONLY via inferred relations AND
    with more than one distinct target — the preserved-ambiguity signal."""
    by_stage: Dict[str, Dict[str, Any]] = {}
    for hop in hops:
        stage = hop["other_stage"]
        record = by_stage.setdefault(stage, {"targets": set(),
                                             "explicit": False})
        record["targets"].add(hop["other"]["id"])
        if hop["relation"]["relation_state"] != RelationState.INFERRED:
            record["explicit"] = True
    return {stage for stage, record in by_stage.items()
            if not record["explicit"] and len(record["targets"]) > 1}


# ---------------------------------------------------------------------------
# Chain enumeration (downstream from a root)
# ---------------------------------------------------------------------------
def _enumerate_chains(ctx: _WalkContext,
                      root: Dict[str, Any]) -> Dict[str, Any]:
    """Bounded DFS from *root* over stage-legal hops. Returns chains (every
    maximal valid chain, capped), the rejected-edge report and ambiguity
    report."""
    root_stage = ctx.stage_of(root)
    depth_cap = min(ctx.policy.rules.maximum_depth, MAX_TRACE_DEPTH)
    chains: List[Dict[str, Any]] = []
    rejected_all: List[Dict[str, Any]] = []
    ambiguities: List[Dict[str, Any]] = []
    seen_ambiguity: Set[Tuple[str, str]] = set()
    visited_count = 0
    truncated = False

    def walk(entity: Dict[str, Any], stage: str,
             steps: List[Dict[str, Any]], on_path: Set[str],
             inferred_count: int, ambiguous: bool) -> None:
        nonlocal visited_count, truncated
        if len(chains) >= MAX_CHAINS_PER_ROOT:
            truncated = True
            return
        visited_count += 1
        if visited_count > MAX_VISITED_PER_ROOT:
            truncated = True
            return
        extended = False
        if len(steps) < depth_cap:
            hops, rejected = _stage_edges(ctx, entity, stage)
            for edge in rejected:
                rejected_all.append(edge)
            ambiguous_stages = _ambiguous_hop_groups(hops)
            for stage_name in ambiguous_stages:
                key = (entity["id"], stage_name)
                if key not in seen_ambiguity:
                    seen_ambiguity.add(key)
                    ambiguities.append({
                        "from_entity_id": entity["id"],
                        "stage": stage_name,
                        "targets": sorted({h["other"]["id"] for h in hops
                                           if h["other_stage"]
                                           == stage_name}),
                    })
            for hop in hops:
                other = hop["other"]
                if other["id"] in on_path:
                    continue
                relation = hop["relation"]
                inferred = relation["relation_state"] == \
                    RelationState.INFERRED
                next_inferred = inferred_count + (1 if inferred else 0)
                if next_inferred > \
                        ctx.policy.rules.inferred_relation_maximum_count:
                    continue
                step = {
                    "stage": hop["other_stage"],
                    "node_id": other["id"],
                    "relation_id": relation["id"],
                    "relation_type": relation["relation_type"],
                    "relation_state": relation["relation_state"],
                    "relation_lifecycle": relation["lifecycle_status"],
                    "direction": hop["direction"],
                    "evidence_status":
                        ctx.relation_evidence_status(relation),
                    "authority_status": other["authority_status"],
                    "entity_lifecycle": other["lifecycle_status"],
                }
                extended = True
                walk(other, hop["other_stage"], steps + [step],
                     on_path | {other["id"]}, next_inferred,
                     ambiguous or hop["other_stage"] in ambiguous_stages)
        else:
            truncated = True
        if steps and not extended:
            chains.append({"steps": steps, "ambiguous": ambiguous,
                           "inferred_count": inferred_count})
        elif steps and extended:
            # Also keep the prefix chain: a shorter terminal stage still
            # serves its own path kind (an implementation hit remains a
            # requirement-to-implementation path even when tests continue).
            chains.append({"steps": steps, "ambiguous": ambiguous,
                           "inferred_count": inferred_count})

    walk(root, root_stage, [], {root["id"]}, 0, False)
    return {"chains": chains[:MAX_CHAINS_PER_ROOT],
            "rejected_edges": rejected_all,
            "ambiguities": ambiguities,
            "truncated": truncated or ctx.truncated,
            "root_stage": root_stage}


# ---------------------------------------------------------------------------
# Chain -> path classification
# ---------------------------------------------------------------------------
def _stages_of_chain(root_stage: str,
                     steps: Sequence[Dict[str, Any]]) -> List[str]:
    out = [root_stage]
    for step in steps:
        if step["stage"] not in out:
            out.append(step["stage"])
    return out


def _completeness(policy: TraceabilityPolicy, reached: Sequence[str]
                  ) -> float:
    required = policy.required_stages()
    if not required:
        return 1.0
    hit = sum(1 for stage in required if stage in reached)
    return round(hit / len(required), 4)


def _chain_status(ctx: _WalkContext, chain: Dict[str, Any],
                  terminal_stage: str) -> str:
    """Deterministic status of one chain serving the kind of its terminal
    stage."""
    stale = False
    for step in chain["steps"]:
        if step["relation_lifecycle"] != GraphLifecycleStatus.ACTIVE or \
                step["entity_lifecycle"] != GraphLifecycleStatus.ACTIVE:
            stale = True
        if ctx.policy.rules.require_current_evidence and \
                step["evidence_status"] == StepEvidenceStatus.STALE:
            stale = True
    # A requiresEvidence terminal stage must actually be evidenced.
    stage_def = ctx.policy.stage(terminal_stage)
    if stage_def and stage_def.requires_evidence and chain["steps"]:
        terminal_entity = chain["steps"][-1]["node_id"]
        verdict = ctx.entity_evidence_status(terminal_entity)
        if verdict == StepEvidenceStatus.STALE:
            stale = True
        elif verdict == StepEvidenceStatus.MISSING:
            return TracePathStatus.PARTIAL
    if stale:
        return TracePathStatus.STALE
    if chain["ambiguous"]:
        return TracePathStatus.AMBIGUOUS
    return TracePathStatus.VERIFIED


def _chain_confidence(ctx: _WalkContext, chain: Dict[str, Any],
                      status: str, completeness: float,
                      root: Dict[str, Any]) -> str:
    if status in (TracePathStatus.STALE, TracePathStatus.AMBIGUOUS,
                  TracePathStatus.PARTIAL, TracePathStatus.BROKEN,
                  TracePathStatus.UNSUPPORTED):
        return TraceConfidence.LOW
    evidence_ok = all(
        step["evidence_status"] == StepEvidenceStatus.CURRENT
        for step in chain["steps"])
    if completeness >= 1.0 and chain["inferred_count"] == 0 and \
            evidence_ok and root["authority_status"] != "informational":
        return TraceConfidence.HIGH
    if completeness >= 1.0 and evidence_ok:
        return TraceConfidence.MEDIUM
    return TraceConfidence.LOW


def _path_hash(root_id: str, target_id: str, kind: str,
               steps: Sequence[Dict[str, Any]],
               policy_checksum: str) -> str:
    basis = "\x1e".join(
        [root_id, target_id, kind, policy_checksum, TRACE_ENGINE_VERSION]
        + [f"{s['node_id']}\x1f{s['relation_id']}\x1f{s['relation_type']}"
           f"\x1f{s['relation_state']}" for s in steps])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _order_key(path: Dict[str, Any]) -> Tuple:
    return (TracePathStatus.rank(path["status"]),
            -path["completeness"],
            TraceConfidence.rank(path["confidence"]),
            path["inferred_count"],
            len(path["steps"]),
            path["path_hash"])


def _chains_to_paths(ctx: _WalkContext, root: Dict[str, Any],
                     enumeration: Dict[str, Any],
                     *, max_paths_per_kind: int) -> List[Dict[str, Any]]:
    """Classify every chain under its terminal stage's path kind, keep the
    bounded best per (kind, target), ordered deterministically."""
    policy = ctx.policy
    root_stage = enumeration["root_stage"]
    candidates: List[Dict[str, Any]] = []
    for chain in enumeration["chains"]:
        if not chain["steps"]:
            continue
        terminal = chain["steps"][-1]
        kind = STAGE_TO_KIND.get(terminal["stage"])
        if not kind:
            continue
        reached = _stages_of_chain(root_stage, chain["steps"])
        status = _chain_status(ctx, chain, terminal["stage"])
        completeness = _completeness(policy, reached)
        confidence = _chain_confidence(ctx, chain, status, completeness,
                                       root)
        steps = []
        for ordinal, step in enumerate(chain["steps"], start=1):
            steps.append({
                "ordinal": ordinal, "stage": step["stage"],
                "node_kind": "entity", "node_id": step["node_id"],
                "relation_id": step["relation_id"],
                "relation_type": step["relation_type"],
                "relation_state": step["relation_state"],
                "evidence_status": step["evidence_status"],
                "authority_status": step["authority_status"],
                "metadata": {"direction": step["direction"]},
            })
        candidates.append({
            "root_entity_id": root["id"],
            "target_entity_id": terminal["node_id"],
            "path_kind": kind,
            "status": status,
            "completeness": completeness,
            "confidence": confidence,
            "inferred_count": chain["inferred_count"],
            "reached_stages": reached,
            "steps": steps,
            "path_hash": _path_hash(root["id"], terminal["node_id"], kind,
                                    chain["steps"], policy.checksum),
        })

    # Deduplicate identical hashes, keep bounded best per kind.
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    seen_hashes: Set[str] = set()
    for path in sorted(candidates, key=_order_key):
        if path["path_hash"] in seen_hashes:
            continue
        seen_hashes.add(path["path_hash"])
        bucket = by_kind.setdefault(path["path_kind"], [])
        if len(bucket) < max_paths_per_kind:
            bucket.append(path)
    out: List[Dict[str, Any]] = []
    for kind in sorted(by_kind):
        out.extend(by_kind[kind])
    return out


# ---------------------------------------------------------------------------
# Root eligibility
# ---------------------------------------------------------------------------
def root_eligibility(ctx: _WalkContext,
                     entity: Dict[str, Any]) -> List[str]:
    """Reasons the entity CANNOT be a trace root (empty = eligible)."""
    reasons: List[str] = []
    policy = ctx.policy
    if entity["entity_type"] not in policy.root_types:
        reasons.append(
            f"entity type {entity['entity_type']!r} is not a root type of "
            f"policy {policy.name!r} (allowed: "
            f"{', '.join(policy.root_types)})")
    if entity["lifecycle_status"] != GraphLifecycleStatus.ACTIVE and \
            not ctx.include_stale:
        reasons.append(
            f"entity is {entity['lifecycle_status']} (must be active)")
    if not ctx.claims_of(entity["id"]):
        reasons.append("entity has no active claim")
    elif ctx.entity_evidence_status(entity["id"]) == \
            StepEvidenceStatus.MISSING:
        reasons.append("entity has no recoverable evidence")
    return reasons


def list_eligible_roots(ctx: _WalkContext,
                        *, limit: int = 2000) -> List[Dict[str, Any]]:
    """Every eligible root entity of the workspace under the policy,
    deterministic order, bounded."""
    roots: List[Dict[str, Any]] = []
    for entity_type in ctx.policy.root_types:
        for entity in kg.list_entities(ctx.workspace_id,
                                       entity_type=entity_type,
                                       lifecycle_status="active",
                                       limit=limit):
            if not root_eligibility(ctx, entity):
                roots.append(entity)
    roots.sort(key=lambda e: (e["canonical_key"], e["id"]))
    return roots[:limit]


# ---------------------------------------------------------------------------
# The three public builders
# ---------------------------------------------------------------------------
def trace_root(workspace_id: str, root_entity: Dict[str, Any],
               policy: TraceabilityPolicy, *,
               include_stale: bool = False,
               max_paths_per_kind: int = MAX_PATHS_PER_KIND,
               ctx: Optional[_WalkContext] = None) -> Dict[str, Any]:
    """The full downstream trace of one Requirement root: paths per kind,
    stage coverage, per-root gap facts, ambiguities, disclosure of every
    rejected edge and cap."""
    ctx = ctx or _WalkContext(workspace_id, policy,
                              include_stale=include_stale)
    max_paths_per_kind = max(1, min(int(max_paths_per_kind),
                                    HARD_MAX_PATHS_PER_KIND))
    enumeration = _enumerate_chains(ctx, root_entity)
    paths = _chains_to_paths(ctx, root_entity, enumeration,
                             max_paths_per_kind=max_paths_per_kind)

    #: Current coverage never counts stale paths (spec §18): ``reached`` is
    #: computed from verified/partial/ambiguous paths only; stale paths'
    #: stages are reported separately as ``stale_only``.
    current_statuses = (TracePathStatus.VERIFIED, TracePathStatus.PARTIAL,
                        TracePathStatus.AMBIGUOUS)
    reached_stages: Set[str] = {enumeration["root_stage"]}
    verified_stages: Set[str] = {enumeration["root_stage"]}
    stale_stages: Set[str] = set()
    for path in paths:
        if path["status"] in current_statuses:
            for stage in path["reached_stages"]:
                reached_stages.add(stage)
        elif path["status"] == TracePathStatus.STALE:
            for stage in path["reached_stages"]:
                stale_stages.add(stage)
        if path["status"] == TracePathStatus.VERIFIED:
            for step in path["steps"]:
                verified_stages.add(step["stage"])
    # partially-implemented detection: implementation reached, but ONLY via
    # partially-implements relations (on non-stale paths).
    impl_full = False
    impl_partial = False
    for path in paths:
        if path["status"] not in current_statuses:
            continue
        for step in path["steps"]:
            if step["stage"] in (TraceStage.IMPLEMENTATION,
                                 TraceStage.CONFIGURATION):
                if step["relation_type"] == "partially-implements":
                    impl_partial = True
                elif step["relation_type"] in ("implements", "configures"):
                    impl_full = True
    partial_only_stages: Set[str] = set()
    if impl_partial and not impl_full:
        partial_only_stages.add(TraceStage.IMPLEMENTATION)

    stage_coverage: Dict[str, Any] = {}
    for stage in policy.stages:
        stage_coverage[stage.name] = {
            "required": stage.required,
            "reached": stage.name in reached_stages,
            "verified": stage.name in verified_stages,
            "stale_only": (stage.name in stale_stages
                           and stage.name not in reached_stages),
        }

    best_per_target: Dict[str, str] = {}
    for path in paths:
        best_per_target.setdefault(path["target_entity_id"],
                                   path["path_hash"])

    return {
        "workspace_id": workspace_id,
        "root": {
            "id": root_entity["id"],
            "entity_type": root_entity["entity_type"],
            "canonical_key": root_entity["canonical_key"],
            "display_name": root_entity["display_name"],
            "authority_status": root_entity["authority_status"],
            "lifecycle_status": root_entity["lifecycle_status"],
        },
        "policy": {"name": policy.name, "checksum": policy.checksum},
        "engine_version": TRACE_ENGINE_VERSION,
        "knowledge_revision": kg.current_revision_number(workspace_id),
        "paths": paths,
        "best_path_per_target": best_per_target,
        "stage_coverage": stage_coverage,
        "partial_implementation_only":
            TraceStage.IMPLEMENTATION in partial_only_stages,
        "ambiguities": enumeration["ambiguities"],
        "rejected_edges": enumeration["rejected_edges"][:50],
        "truncated": enumeration["truncated"],
        "limits": {
            "maximum_depth": min(policy.rules.maximum_depth,
                                 MAX_TRACE_DEPTH),
            "max_paths_per_kind": max_paths_per_kind,
            "max_chains_per_root": MAX_CHAINS_PER_ROOT,
            "max_visited_per_root": MAX_VISITED_PER_ROOT,
        },
    }


def _upstream_requirements(ctx: _WalkContext, entity: Dict[str, Any],
                           stage: str) -> Dict[str, Any]:
    """Bounded reverse BFS to requirement-stage roots, recording one
    shortest stage-legal chain per requirement."""
    depth_cap = min(ctx.policy.rules.maximum_depth, MAX_TRACE_DEPTH)
    frontier: List[Tuple[Dict[str, Any], str, List[Dict[str, Any]]]] = \
        [(entity, stage, [])]
    seen: Set[str] = {entity["id"]}
    requirements: List[Dict[str, Any]] = []
    upstream: Dict[str, List[Dict[str, Any]]] = {}
    truncated = False
    for _ in range(depth_cap):
        next_frontier: List[Tuple[Dict[str, Any], str,
                                  List[Dict[str, Any]]]] = []
        for node, node_stage, chain in frontier:
            hops, _rejected = _stage_edges(ctx, node, node_stage,
                                           reverse=True)
            for hop in hops:
                other = hop["other"]
                if other["id"] in seen:
                    continue
                if len(seen) >= MAX_VISITED_PER_ROOT:
                    truncated = True
                    continue
                seen.add(other["id"])
                step = {
                    "entity_id": other["id"],
                    "canonical_key": other["canonical_key"],
                    "stage": hop["other_stage"],
                    "relation_id": hop["relation"]["id"],
                    "relation_type": hop["relation"]["relation_type"],
                    "relation_state": hop["relation"]["relation_state"],
                }
                new_chain = chain + [step]
                upstream.setdefault(hop["other_stage"], []).append(step)
                if hop["other_stage"] == TraceStage.REQUIREMENT:
                    requirements.append({
                        "entity_id": other["id"],
                        "canonical_key": other["canonical_key"],
                        "display_name": other["display_name"],
                        "authority_status": other["authority_status"],
                        "chain": new_chain,
                    })
                else:
                    next_frontier.append((other, hop["other_stage"],
                                          new_chain))
        frontier = next_frontier
        if not frontier:
            break
    requirements.sort(key=lambda r: (r["canonical_key"], r["entity_id"]))
    return {"requirements": requirements, "upstream": upstream,
            "truncated": truncated}


def trace_code(workspace_id: str, entity: Dict[str, Any],
               policy: TraceabilityPolicy, *,
               include_stale: bool = False) -> Dict[str, Any]:
    """Reverse Code trace: upstream requirements/design/interfaces/
    workflows, downstream tests/results/evidence."""
    ctx = _WalkContext(workspace_id, policy, include_stale=include_stale)
    stage = ctx.stage_of(entity)
    up = _upstream_requirements(ctx, entity, stage) if stage else \
        {"requirements": [], "upstream": {}, "truncated": False}

    # Downstream: verification chains from the code entity.
    downstream = _enumerate_chains(ctx, entity) if stage else \
        {"chains": [], "rejected_edges": [], "ambiguities": [],
         "truncated": False, "root_stage": stage}
    tests: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    seen_nodes: Set[str] = set()
    for chain in downstream["chains"]:
        for step in chain["steps"]:
            if step["node_id"] in seen_nodes:
                continue
            seen_nodes.add(step["node_id"])
            other = ctx.entity(step["node_id"])
            record = {
                "entity_id": step["node_id"],
                "canonical_key": (other or {}).get("canonical_key", ""),
                "stage": step["stage"],
                "relation_type": step["relation_type"],
                "relation_state": step["relation_state"],
                "evidence_status": step["evidence_status"],
            }
            if step["stage"] == TraceStage.VERIFICATION:
                tests.append(record)
            elif step["stage"] in (TraceStage.TEST_RESULT,
                                   TraceStage.EVIDENCE):
                record["entity_evidence_status"] = \
                    ctx.entity_evidence_status(step["node_id"])
                results.append(record)
    tests.sort(key=lambda r: (r["canonical_key"], r["entity_id"]))
    results.sort(key=lambda r: (r["canonical_key"], r["entity_id"]))

    return {
        "workspace_id": workspace_id,
        "entity": {
            "id": entity["id"], "entity_type": entity["entity_type"],
            "canonical_key": entity["canonical_key"],
            "display_name": entity["display_name"],
            "authority_status": entity["authority_status"],
        },
        "stage": stage,
        "policy": {"name": policy.name, "checksum": policy.checksum},
        "engine_version": TRACE_ENGINE_VERSION,
        "knowledge_revision": kg.current_revision_number(workspace_id),
        "requirements": up["requirements"],
        "upstream": {
            stage_name: sorted(
                {s["entity_id"]: s for s in steps}.values(),
                key=lambda s: (s["canonical_key"], s["entity_id"]))
            for stage_name, steps in up["upstream"].items()},
        "tests": tests,
        "test_results": results,
        "orphan": not up["requirements"],
        "classification": ("untraced" if not up["requirements"]
                           else "traced"),
        "truncated": up["truncated"] or downstream["truncated"],
    }


def trace_test(workspace_id: str, entity: Dict[str, Any],
               policy: TraceabilityPolicy, *,
               include_stale: bool = False) -> Dict[str, Any]:
    """Reverse Test trace: verified requirements, implementation targets,
    supporting evidence, honest untraced status."""
    ctx = _WalkContext(workspace_id, policy, include_stale=include_stale)
    stage = ctx.stage_of(entity)
    up = _upstream_requirements(ctx, entity, stage) if stage else \
        {"requirements": [], "upstream": {}, "truncated": False}
    implementation = [
        dict(s) for s in {step["entity_id"]: step for step in
                          up["upstream"].get(TraceStage.IMPLEMENTATION, [])
                          + up["upstream"].get(TraceStage.CONFIGURATION,
                                               [])}.values()]
    implementation.sort(key=lambda s: (s["canonical_key"], s["entity_id"]))

    # Supporting evidence: the test entity's own verified claim evidence.
    supporting: List[Dict[str, Any]] = []
    for claim in ctx.claims_of(entity["id"]):
        for join in kg.claim_evidence(claim["id"]):
            supporting.append({"evidence_id": join["evidence_id"],
                               "claim_id": claim["id"],
                               "role": join["role"]})
    supporting.sort(key=lambda e: (e["evidence_id"], e["claim_id"]))

    return {
        "workspace_id": workspace_id,
        "entity": {
            "id": entity["id"], "entity_type": entity["entity_type"],
            "canonical_key": entity["canonical_key"],
            "display_name": entity["display_name"],
        },
        "stage": stage,
        "policy": {"name": policy.name, "checksum": policy.checksum},
        "engine_version": TRACE_ENGINE_VERSION,
        "knowledge_revision": kg.current_revision_number(workspace_id),
        "requirements": up["requirements"],
        "implementation_targets": implementation,
        "supporting_evidence": supporting[:100],
        "untraced": not up["requirements"],
        "orphan": not up["requirements"] and not implementation,
        "truncated": up["truncated"],
    }


__all__ = [
    "TRACE_ENGINE_VERSION", "MAX_TRACE_DEPTH", "MAX_PATHS_PER_KIND",
    "HARD_MAX_PATHS_PER_KIND", "STAGE_TO_KIND", "CODE_TRACE_TYPES",
    "TEST_TRACE_TYPES",
    "_WalkContext", "trace_root", "trace_code", "trace_test",
    "root_eligibility", "list_eligible_roots",
]
