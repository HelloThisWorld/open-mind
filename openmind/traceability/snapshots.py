"""Refresh orchestration: no-op detection, affected-root calculation,
per-root rebuild, gap reconciliation, orphan detection and the immutable
coverage snapshot.

INCREMENTAL DISCIPLINE (spec §18)
---------------------------------
Everything derived is stamped with (Knowledge Revision × policy checksum ×
TRACE_ENGINE_VERSION). A refresh where all three match the latest completed
run is a NO-OP unless forced. When only the revision advanced, the changed
objects since the prior run are collected (indexed reads), mapped to
affected requirement roots by bounded reverse traversal, and ONLY those
roots are rebuilt — unaffected roots' current paths and gaps are
revalidation-stamped into the new run. A policy or engine change affects
every root.

A refresh NEVER mints a Knowledge Revision: it is derived analysis, stamped
WITH the revision it analyzed.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Set

from ..knowledge import store as kg
from . import TRACE_ENGINE_VERSION
from .engine import (MAX_TRACE_DEPTH, _WalkContext, list_eligible_roots,
                     trace_root)
from .gaps import (detect_root_gaps, detection_fingerprint,
                   find_orphan_code, find_orphan_documents,
                   find_orphan_tests)
from .models import TraceabilityPolicy
from . import store
from .vocabularies import (GapSeverity, GapStatus, GapType, TraceRunStatus,
                           TraceStage)

MAX_ROOTS_PER_RUN = 2000
MAX_ORPHAN_SCAN = 500


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan_refresh(workspace_id: str, policy: TraceabilityPolicy, *,
                 scope: Optional[Dict[str, Any]] = None,
                 force: bool = False) -> Dict[str, Any]:
    """Deterministic dry-run: no-op / incremental / full, the affected
    roots, and why. Writes nothing."""
    current_revision = kg.current_revision_number(workspace_id)
    prior = store.latest_completed_run(workspace_id)
    plan: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "policy_name": policy.name,
        "policy_checksum": policy.checksum,
        "engine_version": TRACE_ENGINE_VERSION,
        "knowledge_revision": current_revision,
        "prior_run": ({"id": prior["id"],
                       "knowledge_revision": prior["knowledge_revision"],
                       "policy_checksum": prior["policy_checksum"],
                       "engine_version": prior["engine_version"],
                       "finished_at": prior["finished_at"]}
                      if prior else None),
        "scope": scope or {},
        "force": bool(force),
    }
    if scope and scope.get("requirement"):
        plan["mode"] = "scoped"
        plan["reason"] = "explicit requirement scope"
        return plan
    if prior and not force and \
            prior["knowledge_revision"] == current_revision and \
            prior["policy_checksum"] == policy.checksum and \
            prior["engine_version"] == TRACE_ENGINE_VERSION:
        plan["mode"] = "no-op"
        plan["reason"] = ("knowledge revision, policy checksum and engine "
                          "version are unchanged since the last completed "
                          "run")
        return plan
    if not prior or force or prior["policy_checksum"] != policy.checksum \
            or prior["engine_version"] != TRACE_ENGINE_VERSION:
        plan["mode"] = "full"
        plan["reason"] = ("forced" if force else
                          "no prior completed run" if not prior else
                          "policy checksum changed"
                          if prior["policy_checksum"] != policy.checksum
                          else "trace engine version changed")
        return plan
    seeds = store.changed_entity_ids_since(workspace_id,
                                           prior["knowledge_revision"])
    affected = affected_roots(workspace_id, policy, seeds)
    plan["mode"] = "incremental"
    plan["reason"] = (f"knowledge revision advanced from "
                      f"{prior['knowledge_revision']} to "
                      f"{current_revision}")
    plan["changed_objects"] = len(seeds)
    plan["affected_roots"] = sorted(affected)
    return plan


def affected_roots(workspace_id: str, policy: TraceabilityPolicy,
                   seed_entity_ids: List[str]) -> Set[str]:
    """Requirement roots affected by changes to *seed* entities: a changed
    root affects itself; any other changed entity affects the roots that
    can reach it — found by bounded multi-source reverse traversal over
    stage-legal edges."""
    if not seed_entity_ids:
        return set()
    ctx = _WalkContext(workspace_id, policy)
    roots: Set[str] = set()
    frontier: List[str] = []
    seen: Set[str] = set()
    for entity_id in seed_entity_ids:
        entity = ctx.entity(entity_id)
        if not entity:
            continue
        stage = ctx.stage_of(entity)
        if stage is None:
            continue
        if stage == TraceStage.REQUIREMENT:
            roots.add(entity_id)
        seen.add(entity_id)
        frontier.append(entity_id)

    depth_cap = min(policy.rules.maximum_depth, MAX_TRACE_DEPTH)
    from .engine import _stage_edges
    for _ in range(depth_cap):
        next_frontier: List[str] = []
        for entity_id in frontier:
            entity = ctx.entity(entity_id)
            if not entity:
                continue
            stage = ctx.stage_of(entity)
            if stage is None or stage == TraceStage.REQUIREMENT:
                continue
            hops, _rejected = _stage_edges(ctx, entity, stage,
                                           reverse=True)
            for hop in hops:
                other = hop["other"]
                if other["id"] in seen:
                    continue
                seen.add(other["id"])
                if hop["other_stage"] == TraceStage.REQUIREMENT:
                    roots.add(other["id"])
                else:
                    next_frontier.append(other["id"])
                if len(seen) > 4 * MAX_ROOTS_PER_RUN:
                    return roots
        frontier = next_frontier
        if not frontier:
            break
    return roots


# ---------------------------------------------------------------------------
# Gap reconciliation
# ---------------------------------------------------------------------------
def _acceptance_expired(gap: Dict[str, Any]) -> bool:
    expiry = str((gap.get("metadata") or {}).get("acceptance_expires_at")
                 or "")
    return bool(expiry) and expiry < _now()


def reconcile_root_gaps(workspace_id: str, run_id: str,
                        knowledge_revision: int, policy_checksum: str,
                        root_entity_id: str,
                        detected: List[Dict[str, Any]]) -> Dict[str, int]:
    """Match detected gaps against the root's existing current rows by
    detection fingerprint. Governance status survives while the facts are
    unchanged; an accepted gap past its expiry reopens; a dismissed gap
    whose facts changed no longer matches and a new open gap is created."""
    existing = store.list_gaps(workspace_id, root_entity_id=root_entity_id,
                               current_only=True, limit=1000)
    by_fingerprint = {g["detection_fingerprint"]: g for g in existing}
    counts = {"created": 0, "kept": 0, "resolved": 0, "reopened": 0,
              "suppressed": 0}
    seen: Set[str] = set()
    for gap in detected:
        fingerprint = gap["detection_fingerprint"]
        seen.add(fingerprint)
        row = by_fingerprint.get(fingerprint)
        if row is None:
            store.insert_gap(workspace_id, {
                **gap, "run_id": run_id,
                "knowledge_revision": knowledge_revision,
                "policy_checksum": policy_checksum,
                "status": GapStatus.OPEN,
            })
            counts["created"] += 1
            continue
        if row["status"] == GapStatus.ACCEPTED and \
                _acceptance_expired(row):
            metadata = dict(row.get("metadata") or {})
            metadata["reopened_reason"] = "acceptance-expired"
            store.update_gap(workspace_id, row["id"],
                             status=GapStatus.OPEN, run_id=run_id,
                             knowledge_revision=knowledge_revision,
                             metadata=metadata)
            counts["reopened"] += 1
            continue
        if row["status"] in (GapStatus.ACCEPTED, GapStatus.DISMISSED):
            store.update_gap(workspace_id, row["id"], run_id=run_id,
                             knowledge_revision=knowledge_revision)
            counts["suppressed"] += 1
            continue
        # open (or previously resolved and detected again -> reopen)
        fields: Dict[str, Any] = {"run_id": run_id,
                                  "knowledge_revision": knowledge_revision,
                                  "severity": gap["severity"],
                                  "reason": gap["reason"]}
        if row["status"] == GapStatus.RESOLVED:
            fields["status"] = GapStatus.OPEN
            fields["resolved_at"] = None
            counts["reopened"] += 1
        else:
            counts["kept"] += 1
        store.update_gap(workspace_id, row["id"], **fields)
    # Existing current rows no longer detected.
    for fingerprint, row in by_fingerprint.items():
        if fingerprint in seen:
            continue
        if row["status"] == GapStatus.DISMISSED:
            # Keep the dismissal auditable but out of the current set.
            store.update_gap(workspace_id, row["id"], stale_at=_now())
            continue
        if row["status"] in (GapStatus.OPEN, GapStatus.ACCEPTED):
            store.update_gap(workspace_id, row["id"],
                             status=GapStatus.RESOLVED,
                             resolved_at=_now(), run_id=run_id,
                             knowledge_revision=knowledge_revision)
            counts["resolved"] += 1
    return counts


def _orphan_gaps(workspace_id: str, policy: TraceabilityPolicy,
                 orphan_result: Dict[str, Any],
                 gap_type: str) -> List[Dict[str, Any]]:
    gaps = []
    for orphan in orphan_result["orphans"]:
        gaps.append({
            "root_entity_id": orphan["entity_id"],
            "stage": "",
            "gap_type": gap_type,
            "severity": policy.gap_severity(gap_type),
            "reason": f"{gap_type}: {orphan['canonical_key']} is untraced",
            "blocking_object": {"classification": "untraced"},
            "detection_fingerprint": detection_fingerprint(
                orphan["entity_id"], "", gap_type),
        })
    return gaps


# ---------------------------------------------------------------------------
# The refresh itself
# ---------------------------------------------------------------------------
def run_refresh(workspace_id: str, policy: TraceabilityPolicy, *,
                scope: Optional[Dict[str, Any]] = None,
                force: bool = False,
                progress: Optional[Callable[[str], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None
                ) -> Dict[str, Any]:
    """Execute one refresh. Returns {run, snapshot?, no_op?}."""
    def step(name: str) -> None:
        if progress:
            progress(name)

    def is_cancelled() -> bool:
        return bool(cancelled and cancelled())

    step("planning")
    plan = plan_refresh(workspace_id, policy, scope=scope, force=force)
    if plan["mode"] == "no-op":
        return {"no_op": True, "plan": plan, "run": None}
    current_revision = plan["knowledge_revision"]
    checksum = policy.checksum
    run = store.create_run(
        workspace_id, knowledge_revision=current_revision,
        policy_name=policy.name, policy_checksum=checksum,
        engine_version=TRACE_ENGINE_VERSION, scope=scope or {},
        status=TraceRunStatus.RUNNING)
    store.update_run(workspace_id, run["id"], started_at=_now())
    run_id = run["id"]
    try:
        step("selecting-roots")
        ctx = _WalkContext(workspace_id, policy)
        all_roots = list_eligible_roots(ctx, limit=MAX_ROOTS_PER_RUN)
        scoped_requirement = (scope or {}).get("requirement") or ""
        if scoped_requirement:
            all_roots = [r for r in all_roots
                         if r["id"] == scoped_requirement
                         or r["canonical_key"] == scoped_requirement]
        rebuild_ids: Set[str]
        if plan["mode"] == "incremental":
            rebuild_ids = set(plan.get("affected_roots") or [])
        else:
            rebuild_ids = {r["id"] for r in all_roots}
        rebuild_roots = [r for r in all_roots if r["id"] in rebuild_ids]
        reuse_roots = [r for r in all_roots
                       if r["id"] not in rebuild_ids]

        step("building-paths")
        # Stale the paths being rebuilt, then rebuild them.
        store.stale_paths_for_roots(workspace_id,
                                    [r["id"] for r in rebuild_roots])
        results: List[Dict[str, Any]] = []
        gap_lists: List[List[Dict[str, Any]]] = []
        truncated = False
        for root in rebuild_roots:
            if is_cancelled():
                store.update_run(workspace_id, run_id,
                                 status=TraceRunStatus.CANCELLED,
                                 finished_at=_now())
                return {"run": store.get_run(workspace_id, run_id),
                        "cancelled": True}
            result = trace_root(workspace_id, root, policy, ctx=ctx)
            truncated = truncated or result["truncated"]
            results.append(result)
        step("validating-paths")
        for result in results:
            rows = []
            for path in result["paths"]:
                evidence_rows = []
                seen_evidence: Set[str] = set()
                for step_row in path["steps"]:
                    for join in kg.relation_evidence(
                            step_row["relation_id"]):
                        if join["evidence_id"] in seen_evidence:
                            continue
                        seen_evidence.add(join["evidence_id"])
                        evidence_rows.append(
                            {"evidence_id": join["evidence_id"],
                             "role": "supporting"})
                rows.append({
                    "run_id": run_id,
                    "root_entity_id": path["root_entity_id"],
                    "target_entity_id": path["target_entity_id"],
                    "path_kind": path["path_kind"],
                    "status": path["status"],
                    "completeness": path["completeness"],
                    "confidence": path["confidence"],
                    "knowledge_revision": current_revision,
                    "policy_checksum": checksum,
                    "path_hash": path["path_hash"],
                    "metadata": {
                        "inferred_count": path["inferred_count"],
                        "reached_stages": path["reached_stages"],
                    },
                    "steps": path["steps"],
                    "evidence": evidence_rows[:50],
                })
            store.insert_paths(workspace_id, rows)
        # Revalidation-stamp the reused roots' current paths and gaps.
        reused_paths = store.restamp_paths_run(
            workspace_id, [r["id"] for r in reuse_roots], run_id,
            current_revision)
        store.restamp_gaps_run(workspace_id,
                               [r["id"] for r in reuse_roots], run_id,
                               current_revision)

        step("detecting-gaps")
        gap_counts = {"created": 0, "kept": 0, "resolved": 0,
                      "reopened": 0, "suppressed": 0}
        for result in results:
            detected = detect_root_gaps(policy, result)
            gap_lists.append(detected)
            counts = reconcile_root_gaps(
                workspace_id, run_id, current_revision, checksum,
                result["root"]["id"], detected)
            for key, value in counts.items():
                gap_counts[key] += value

        step("detecting-orphans")
        orphan_code = find_orphan_code(workspace_id, policy,
                                       limit=MAX_ORPHAN_SCAN)
        orphan_tests = find_orphan_tests(workspace_id, policy,
                                         limit=MAX_ORPHAN_SCAN)
        orphan_documents = find_orphan_documents(workspace_id,
                                                 limit=MAX_ORPHAN_SCAN)
        for orphan_result, gap_type in (
                (orphan_code, GapType.ORPHAN_CODE),
                (orphan_tests, GapType.ORPHAN_TEST),
                (orphan_documents, GapType.ORPHAN_DOCUMENT)):
            for gap in _orphan_gaps(workspace_id, policy, orphan_result,
                                    gap_type):
                counts = reconcile_root_gaps(
                    workspace_id, run_id, current_revision, checksum,
                    gap["root_entity_id"], [gap])
                for key, value in counts.items():
                    gap_counts[key] += value

        step("calculating-coverage")
        # Coverage is computed over CURRENT state: freshly rebuilt results
        # plus reconstructed summaries of the reused roots' current paths.
        for root in reuse_roots:
            results.append(_reconstruct_result(workspace_id, policy, root))
            gap_lists.append([
                g for g in store.list_gaps(
                    workspace_id, root_entity_id=root["id"],
                    status=GapStatus.OPEN, current_only=True, limit=200)])
        open_critical = len([
            g for g in store.list_gaps(workspace_id,
                                       status=GapStatus.OPEN,
                                       severity=GapSeverity.CRITICAL,
                                       current_only=True, limit=1000)])
        from .coverage import compute_coverage
        metrics = compute_coverage(
            policy, results, gap_lists,
            orphan_code_count=orphan_code["count"],
            orphan_test_count=orphan_tests["count"],
            open_critical_gaps=open_critical)

        step("persisting-snapshot")
        store.stale_current_snapshots(workspace_id)
        snapshot_id = store.insert_snapshot(
            workspace_id, run_id=run_id,
            knowledge_revision=current_revision, policy_name=policy.name,
            policy_checksum=checksum, engine_version=TRACE_ENGINE_VERSION,
            scope=scope or {}, metrics=metrics)

        summary = {
            "mode": plan["mode"],
            "roots_total": len(all_roots),
            "roots_rebuilt": len(rebuild_roots),
            "roots_reused": len(reuse_roots),
            "paths_reused": reused_paths,
            "gaps": gap_counts,
            "orphans": {
                "code": orphan_code["count"],
                "tests": orphan_tests["count"],
                "documents": orphan_documents["count"],
            },
            "truncated": truncated,
            "snapshot_id": snapshot_id,
        }
        status = TraceRunStatus.DONE
        store.update_run(workspace_id, run_id, status=status,
                         summary=summary, finished_at=_now())
        step("done")
        return {"run": store.get_run(workspace_id, run_id),
                "snapshot_id": snapshot_id, "plan": plan}
    except Exception as exc:
        store.update_run(workspace_id, run_id,
                         status=TraceRunStatus.FAILED, error=str(exc),
                         finished_at=_now())
        raise


def _reconstruct_result(workspace_id: str, policy: TraceabilityPolicy,
                        root: Dict[str, Any]) -> Dict[str, Any]:
    """A coverage-shaped summary of one REUSED root from its persisted
    current paths (no re-walk — that is the point of reuse)."""
    paths = store.list_paths(workspace_id, root_entity_id=root["id"],
                             current_only=True, limit=500)
    shaped = []
    reached: Set[str] = {TraceStage.REQUIREMENT}
    for path in paths:
        metadata = path.get("metadata") or {}
        stages = metadata.get("reached_stages") or []
        steps = store.path_steps(path["id"])
        shaped.append({
            "path_kind": path["path_kind"], "status": path["status"],
            "completeness": path["completeness"],
            "confidence": path["confidence"],
            "steps": steps,
            "reached_stages": stages,
            "target_entity_id": path["target_entity_id"],
        })
        if path["status"] in ("verified", "partial", "ambiguous"):
            reached.update(stages)
    stage_coverage = {}
    for stage in policy.stages:
        stage_coverage[stage.name] = {
            "required": stage.required,
            "reached": stage.name in reached,
            "verified": stage.name in reached,
            "stale_only": False,
        }
    return {
        "root": {
            "id": root["id"], "entity_type": root["entity_type"],
            "canonical_key": root["canonical_key"],
            "display_name": root["display_name"],
            "authority_status": root["authority_status"],
        },
        "paths": shaped,
        "stage_coverage": stage_coverage,
        "partial_implementation_only": False,
        "ambiguities": [],
    }


# ---------------------------------------------------------------------------
# Trace staleness reconciliation
# ---------------------------------------------------------------------------
def reconcile_trace_staleness(workspace_id: str) -> Dict[str, Any]:
    """Mark current paths whose relations/entities are no longer
    active-graph as stale (or broken when a referenced row is gone), and
    stale the current snapshot when anything moved. Cheap, local,
    model-free — safe to run after graph synchronization."""
    unusable = store.paths_with_unusable_relations(workspace_id)
    stale_ids = [u["path_id"] for u in unusable if u["reason"] == "stale"]
    broken_ids = [u["path_id"] for u in unusable
                  if u["reason"] == "broken"]
    changed = 0
    changed += store.mark_paths(workspace_id, stale_ids, status="stale")
    changed += store.mark_paths(workspace_id, broken_ids, status="broken")
    return {"workspace_id": workspace_id, "paths_staled": len(stale_ids),
            "paths_broken": len(broken_ids), "changed": changed > 0}


__all__ = [
    "MAX_ROOTS_PER_RUN", "plan_refresh", "affected_roots",
    "reconcile_root_gaps", "run_refresh", "reconcile_trace_staleness",
]
