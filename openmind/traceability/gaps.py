"""Gap detection and orphan queries.

A gap is FIRST-CLASS governance data: the engine returns what is missing
instead of inventing a link. Detection is deterministic — the gap set of one
root is a pure function of its trace result and the policy — and every gap
carries a detection fingerprint (root + stage + type + blocking identity)
so governance status survives refreshes while the facts are unchanged and
stops applying when they change.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from ..knowledge import store as kg
from ..knowledge.vocabularies import GraphOrigin
from .engine import (CODE_TRACE_TYPES, TEST_TRACE_TYPES, _WalkContext,
                     trace_code, trace_test)
from .models import TraceabilityPolicy
from .vocabularies import (GapType, StepEvidenceStatus, TracePathStatus,
                           TraceStage)


def detection_fingerprint(root_entity_id: str, stage: str, gap_type: str,
                          blocking_ids: Optional[List[str]] = None) -> str:
    basis = "\x1e".join([root_entity_id, stage, gap_type]
                        + sorted(blocking_ids or []))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _gap(policy: TraceabilityPolicy, *, root_entity_id: str, stage: str,
         gap_type: str, reason: str,
         blocking: Optional[Dict[str, Any]] = None,
         blocking_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "root_entity_id": root_entity_id,
        "stage": stage,
        "gap_type": gap_type,
        "severity": policy.gap_severity(gap_type),
        "reason": reason,
        "blocking_object": blocking or {},
        "detection_fingerprint": detection_fingerprint(
            root_entity_id, stage, gap_type, blocking_ids),
    }


def detect_root_gaps(policy: TraceabilityPolicy,
                     trace_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The deterministic gap set of ONE root's trace result."""
    gaps: List[Dict[str, Any]] = []
    root = trace_result["root"]
    root_id = root["id"]
    stage_coverage = trace_result["stage_coverage"]
    paths = trace_result["paths"]

    # 1. Missing required stages (never invented; optional stages absent
    #    are silence, not a gap).
    for stage_name, coverage in stage_coverage.items():
        if stage_name == TraceStage.REQUIREMENT:
            continue
        if not coverage["required"] or coverage["reached"]:
            continue
        gap_type = GapType.BY_MISSING_STAGE.get(stage_name)
        if not gap_type:
            continue
        if coverage.get("stale_only"):
            # The stage WAS reached but every path to it is stale.
            completed = [s for s, c in stage_coverage.items()
                         if c["reached"]]
            gaps.append(_gap(
                policy, root_entity_id=root_id, stage=stage_name,
                gap_type=GapType.STALE_PATH,
                reason=(f"every path reaching required stage "
                        f"{stage_name!r} is stale"),
                blocking={"completed_stages": completed},
                blocking_ids=[stage_name]))
            continue
        # requiresEvidence stage reached-but-unevidenced is detected below;
        # here the stage is genuinely unreachable.
        completed = [s for s, c in stage_coverage.items() if c["reached"]]
        gaps.append(_gap(
            policy, root_entity_id=root_id, stage=stage_name,
            gap_type=gap_type,
            reason=(f"no valid path reaches required stage "
                    f"{stage_name!r}"),
            blocking={"completed_stages": completed}))

    # 2. requiresEvidence stage reached, but terminal evidence missing.
    for stage in policy.stages:
        if not stage.requires_evidence:
            continue
        coverage = stage_coverage.get(stage.name) or {}
        if not coverage.get("reached"):
            continue
        terminal_paths = [p for p in paths
                          if p["steps"]
                          and p["steps"][-1]["stage"] == stage.name]
        if terminal_paths and all(
                p["status"] == TracePathStatus.PARTIAL
                for p in terminal_paths):
            targets = sorted({p["target_entity_id"]
                              for p in terminal_paths})
            gaps.append(_gap(
                policy, root_entity_id=root_id, stage=stage.name,
                gap_type=GapType.MISSING_EVIDENCE,
                reason=(f"stage {stage.name!r} is reached but no terminal "
                        f"object carries current verified evidence"),
                blocking={"targets": targets}, blocking_ids=targets))

    # 3. Partial implementation only.
    if trace_result.get("partial_implementation_only"):
        gaps.append(_gap(
            policy, root_entity_id=root_id,
            stage=TraceStage.IMPLEMENTATION,
            gap_type=GapType.PARTIAL_IMPLEMENTATION_ONLY,
            reason=("implementation is reached only through "
                    "partially-implements relations")))

    # 4. Ambiguous targets.
    for ambiguity in trace_result.get("ambiguities") or []:
        gaps.append(_gap(
            policy, root_entity_id=root_id, stage=ambiguity["stage"],
            gap_type=GapType.AMBIGUOUS_TARGET,
            reason=(f"stage {ambiguity['stage']!r} is reachable from "
                    f"{ambiguity['from_entity_id']} only through inferred "
                    f"relations to {len(ambiguity['targets'])} distinct "
                    f"targets"),
            blocking={"from_entity_id": ambiguity["from_entity_id"],
                      "targets": ambiguity["targets"]},
            blocking_ids=list(ambiguity["targets"])))

    # 5. Stale paths whose (kind, target) has no current alternative.
    current = {(p["path_kind"], p["target_entity_id"]) for p in paths
               if p["status"] in (TracePathStatus.VERIFIED,
                                  TracePathStatus.PARTIAL,
                                  TracePathStatus.AMBIGUOUS)}
    seen_stale: set = set()
    for path in paths:
        if path["status"] != TracePathStatus.STALE:
            continue
        key = (path["path_kind"], path["target_entity_id"])
        if key in current or key in seen_stale:
            continue
        seen_stale.add(key)
        terminal_stage = path["steps"][-1]["stage"] if path["steps"] else ""
        gaps.append(_gap(
            policy, root_entity_id=root_id, stage=terminal_stage,
            gap_type=GapType.STALE_PATH,
            reason=(f"the only {path['path_kind']} path to "
                    f"{path['target_entity_id']} is stale"),
            blocking={"path_kind": path["path_kind"],
                      "target_entity_id": path["target_entity_id"]},
            blocking_ids=[path["path_kind"], path["target_entity_id"]]))

    # 6. Unsupported relations that would have bridged an unreached stage.
    unreached = {s for s, c in stage_coverage.items()
                 if c["required"] and not c["reached"]}
    seen_unsupported: set = set()
    for edge in trace_result.get("rejected_edges") or []:
        if edge["to_stage"] not in unreached:
            continue
        if edge["relation_id"] in seen_unsupported:
            continue
        seen_unsupported.add(edge["relation_id"])
        gaps.append(_gap(
            policy, root_entity_id=root_id, stage=edge["to_stage"],
            gap_type=GapType.UNSUPPORTED_RELATION,
            reason=(f"{edge['reason']} (relation {edge['relation_id']} "
                    f"from {edge['from_entity_id']} to "
                    f"{edge['to_entity_id']})"),
            blocking={"relation_id": edge["relation_id"],
                      "relation_type": edge["relation_type"]},
            blocking_ids=[edge["relation_id"]]))

    # 7. Authority gap (policy-gated; authority is never inferred).
    if policy.rules.require_authoritative_roots and \
            root["authority_status"] != "authoritative":
        gaps.append(_gap(
            policy, root_entity_id=root_id, stage=TraceStage.REQUIREMENT,
            gap_type=GapType.AUTHORITY_GAP,
            reason=(f"policy requires an authoritative requirement root; "
                    f"this root is {root['authority_status']!r}"),
            blocking={"authority_status": root["authority_status"]}))

    # 8. Orphan requirement: no valid path to any required
    #    implementation-ish stage.
    implementation_stages = {TraceStage.IMPLEMENTATION,
                             TraceStage.CONFIGURATION}
    required_impl = [s for s in policy.required_stages()
                     if s in implementation_stages]
    if required_impl and not any(
            stage_coverage.get(s, {}).get("reached")
            for s in implementation_stages):
        gaps.append(_gap(
            policy, root_entity_id=root_id,
            stage=required_impl[0],
            gap_type=GapType.ORPHAN_REQUIREMENT,
            reason="requirement has no valid path to any implementation "
                   "stage"))

    gaps.sort(key=lambda g: (g["gap_type"], g["stage"],
                             g["detection_fingerprint"]))
    return gaps


# ---------------------------------------------------------------------------
# Orphan queries (explicit, bounded, never "invalid")
# ---------------------------------------------------------------------------
def find_orphan_requirements(workspace_id: str, policy: TraceabilityPolicy,
                             *, limit: int = 500) -> Dict[str, Any]:
    """Active root-typed entities with no valid path to a required
    implementation stage."""
    from .engine import list_eligible_roots, trace_root
    ctx = _WalkContext(workspace_id, policy)
    orphans: List[Dict[str, Any]] = []
    roots = list_eligible_roots(ctx, limit=limit)
    for root in roots:
        result = trace_root(workspace_id, root, policy, ctx=ctx)
        coverage = result["stage_coverage"]
        reached_impl = any(
            coverage.get(s, {}).get("reached")
            for s in (TraceStage.IMPLEMENTATION, TraceStage.CONFIGURATION))
        if not reached_impl:
            orphans.append({
                "entity_id": root["id"],
                "canonical_key": root["canonical_key"],
                "display_name": root["display_name"],
                "orphan": True,
                "classification": "untraced",
                "reached_stages": [s for s, c in coverage.items()
                                   if c["reached"]],
            })
    return {"workspace_id": workspace_id, "orphans": orphans,
            "count": len(orphans), "roots_examined": len(roots),
            "limit": limit}


def find_orphan_code(workspace_id: str, policy: TraceabilityPolicy,
                     *, limit: int = 500) -> Dict[str, Any]:
    """Implementation-stage entities with no valid upstream requirement
    path. Framework/utility code is NOT a defect: the classification is
    ``untraced``, never ``invalid``."""
    orphans: List[Dict[str, Any]] = []
    examined = 0
    for entity_type in sorted(CODE_TRACE_TYPES):
        for entity in kg.list_entities(workspace_id,
                                       entity_type=entity_type,
                                       lifecycle_status="active",
                                       limit=limit):
            if examined >= limit:
                break
            examined += 1
            result = trace_code(workspace_id, entity, policy)
            if result["orphan"]:
                orphans.append({
                    "entity_id": entity["id"],
                    "entity_type": entity["entity_type"],
                    "canonical_key": entity["canonical_key"],
                    "orphan": True,
                    "classification": "untraced",
                })
    orphans.sort(key=lambda o: (o["canonical_key"], o["entity_id"]))
    return {"workspace_id": workspace_id, "orphans": orphans,
            "count": len(orphans), "entities_examined": examined,
            "limit": limit}


def find_orphan_tests(workspace_id: str, policy: TraceabilityPolicy,
                      *, limit: int = 500) -> Dict[str, Any]:
    """Test cases with no valid upstream requirement or implementation
    path. An orphan test is a fact, not automatically invalid."""
    orphans: List[Dict[str, Any]] = []
    examined = 0
    for entity in kg.list_entities(workspace_id, entity_type="test-case",
                                   lifecycle_status="active", limit=limit):
        examined += 1
        result = trace_test(workspace_id, entity, policy)
        if result["orphan"]:
            orphans.append({
                "entity_id": entity["id"],
                "entity_type": entity["entity_type"],
                "canonical_key": entity["canonical_key"],
                "orphan": True,
                "classification": "untraced",
            })
    orphans.sort(key=lambda o: (o["canonical_key"], o["entity_id"]))
    return {"workspace_id": workspace_id, "orphans": orphans,
            "count": len(orphans), "entities_examined": examined,
            "limit": limit}


def find_orphan_documents(workspace_id: str, *,
                          limit: int = 500) -> Dict[str, Any]:
    """Document entities carrying PROMOTED engineering claims but zero
    active canonical relations into the engineering graph."""
    orphans: List[Dict[str, Any]] = []
    examined = 0
    for entity in kg.list_entities(workspace_id, entity_type="document",
                                   lifecycle_status="active", limit=limit):
        examined += 1
        claims = kg.list_claims(workspace_id, entity_id=entity["id"],
                                lifecycle_status="active", limit=50)
        promoted = [c for c in claims
                    if c["origin"] == GraphOrigin.SEMANTIC_PROMOTION]
        if not promoted:
            continue
        relations = kg.list_relations(workspace_id, entity_id=entity["id"],
                                      lifecycle_status="active", limit=10)
        if not relations:
            orphans.append({
                "entity_id": entity["id"],
                "canonical_key": entity["canonical_key"],
                "promoted_claims": len(promoted),
                "orphan": True,
                "classification": "untraced",
            })
    orphans.sort(key=lambda o: (o["canonical_key"], o["entity_id"]))
    return {"workspace_id": workspace_id, "orphans": orphans,
            "count": len(orphans), "entities_examined": examined,
            "limit": limit}


__all__ = [
    "detection_fingerprint", "detect_root_gaps",
    "find_orphan_requirements", "find_orphan_code", "find_orphan_tests",
    "find_orphan_documents",
]
