"""Deterministic analysis planning. ZERO provider calls, ever.

The planner turns "analyze this workspace" into an explicit, bounded target
list — and everything it reports is computed locally:

* only ACTIVE assets and their CURRENT revisions; document revisions must
  have a usable parse (``parsed``/``partial``); removed assets excluded;
* per task: only supported asset types and supported, content-bearing
  segment types (containers never analyzed);
* the ACTIVE Project Lens (when one exists) narrows semantic planning —
  which tasks run, over which asset types / roles / block types. It touches
  nothing else;
* token figures are chars/4 ESTIMATES and labelled so;
* cache-hit counts are a plan-time estimate matched on the target's input
  hash (the runner's real key is stricter — see ``cache.py``);
* whatever is excluded is listed WITH its reason, bounded.

The returned plan is both the ``--json`` dry-run answer and the input
``start_analysis`` persists as the run's target list.
"""
from __future__ import annotations

import fnmatch
import hashlib
from typing import Any, Dict, List, Optional, Sequence

from .. import db
from ..domain.types import DocumentBlockType, DocumentParseStatus
from . import context as context_mod
from . import prompts, store
from . import ANALYZER_VERSION
from .models import ModelTier
from .tasks import TaskDefinition, require_task

#: Hard cap on one run's planned targets; beyond it targets are excluded
#: with an explicit reason, never silently dropped.
MAX_PLAN_TARGETS = 5_000
#: Bounded exclusions listed verbatim in the plan (the rest is a count).
MAX_LISTED_EXCLUSIONS = 50
#: Deterministic per-target token estimates (chars/4 applied to a nominal
#: content size) — labelled estimated everywhere they surface.
_SEGMENT_EST_TOKENS = 700
_REVISION_EST_TOKENS = 1_600
_PAIR_EST_TOKENS = 1_200


def target_input_hash(revision_id: str, segment_key: str,
                      content_hash: str, task: TaskDefinition) -> str:
    payload = "|".join((revision_id, segment_key, content_hash,
                        task.task_type, task.task_version,
                        task.prompt_version, ANALYZER_VERSION))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lens_task_rule(lens: Optional[Dict[str, Any]],
                    task_type: str) -> Dict[str, Any]:
    """What the active lens says about one task: ``{listed, asset_types,
    roles, block_types}``. With no lens (or a lens without semanticTasks)
    everything is allowed."""
    definition = (lens or {}).get("definition") or {}
    entries = definition.get("semanticTasks") or []
    if not entries:
        return {"listed": True, "asset_types": None, "roles": None,
                "block_types": None}
    for entry in entries:
        if str(entry.get("task") or "") == task_type:
            roles = [str(r) for r in (entry.get("includeRoles") or [])]
            role_globs: List[str] = []
            for role in (definition.get("roles") or []):
                if not roles or str(role.get("name") or "") in roles:
                    role_globs.extend(str(g) for g in
                                      (role.get("pathGlobs") or []))
            return {
                "listed": True,
                "asset_types": set(entry.get("includeAssetTypes") or []) or None,
                "roles": role_globs if roles else None,
                "block_types": set(entry.get("includeBlockTypes") or []) or None,
            }
    return {"listed": False, "asset_types": None, "roles": None,
            "block_types": None}


def _matches_roles(logical_key: str, role_globs: Optional[List[str]]) -> bool:
    if role_globs is None:
        return True
    key = (logical_key or "").replace("\\", "/").lower()
    return any(fnmatch.fnmatch(key, g.lower()) for g in role_globs)


def _scope_allows(scope: Dict[str, Any], asset: Dict[str, Any],
                  is_document: bool) -> Optional[str]:
    """None when allowed, else the exclusion reason."""
    kind = str(scope.get("kind") or "all")
    if kind == "documents" and not is_document:
        return "scope=documents excludes non-document assets"
    if kind == "code" and is_document:
        return "scope=code excludes document assets"
    asset_ids = scope.get("assets") or []
    if asset_ids and asset["id"] not in asset_ids:
        return "outside the requested asset list"
    return None


def plan_analysis(workspace_id: str, *, task_types: Sequence[str],
                  scope: Optional[Dict[str, Any]] = None,
                  model_tier: str = "",
                  lens: Optional[Dict[str, Any]] = None,
                  policy: Optional[Dict[str, Any]] = None,
                  budgets: Optional[Dict[str, Any]] = None,
                  force: bool = False) -> Dict[str, Any]:
    """The deterministic dry-run plan (spec §19). Performs no provider call
    and writes nothing."""
    scope = dict(scope or {"kind": "all"})
    policy = policy or store.get_policy(workspace_id)
    tasks = [require_task(t) for t in task_types]

    assets = db.list_assets(workspace_id, state="active", limit=100_000)
    revision_ids: set = set()
    targets: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    excluded_total = 0
    evidence_count = 0

    def exclude(reason: str, **ref: str) -> None:
        nonlocal excluded_total
        excluded_total += 1
        if len(excluded) < MAX_LISTED_EXCLUSIONS:
            excluded.append({**ref, "reason": reason})

    pair_tasks = [t for t in tasks if t.granularity == "pair"]
    unit_tasks = [t for t in tasks if t.granularity in ("segment", "revision")]

    for asset in assets:
        revision_id = asset.get("current_revision_id")
        if not revision_id:
            exclude("asset has no current revision", asset=asset["id"])
            continue
        parse = db.get_document_parse(workspace_id, revision_id)
        is_document = parse is not None
        if is_document and parse["status"] not in DocumentParseStatus.USABLE:
            exclude(f"document parse status {parse['status']!r} is not "
                    f"usable", asset=asset["id"])
            continue
        scope_reason = _scope_allows(scope, asset, is_document)
        if scope_reason:
            exclude(scope_reason, asset=asset["id"])
            continue

        segments = None      # loaded lazily, once per revision
        for task in unit_tasks:
            rule = _lens_task_rule(lens, task.task_type)
            if not rule["listed"]:
                exclude(f"task {task.task_type} not in the active lens",
                        asset=asset["id"], task=task.task_type)
                continue
            allowed_types = (rule["asset_types"]
                             if rule["asset_types"] is not None
                             else task.supported_asset_types)
            if asset["asset_type"] not in allowed_types:
                continue
            if not _matches_roles(asset["logical_key"], rule["roles"]):
                exclude("outside the active lens's included roles",
                        asset=asset["id"], task=task.task_type)
                continue

            if task.granularity == "revision":
                revision_ids.add(revision_id)
                targets.append({
                    "revision_id": revision_id, "segment_id": "",
                    "task_type": task.task_type,
                    "input_hash": target_input_hash(
                        revision_id, "", asset.get("id", ""), task),
                    "estimated_tokens": _REVISION_EST_TOKENS,
                    "tier": model_tier or task.default_model_tier,
                })
                continue

            if segments is None:
                segments = db.list_segments(workspace_id, revision_id,
                                            limit=10_000)
                evidence_count += len(db.evidence_ids_for_revision(
                    workspace_id, revision_id))
            for segment in segments:
                stype = segment.get("segment_type", "")
                if stype in DocumentBlockType.CONTAINERS:
                    continue
                if stype not in task.supported_segment_types:
                    continue
                if rule["block_types"] is not None and \
                        stype not in rule["block_types"]:
                    continue
                if not segment.get("content_hash"):
                    continue
                if len(targets) >= MAX_PLAN_TARGETS:
                    exclude("plan target cap reached",
                            revision=revision_id, task=task.task_type)
                    continue
                revision_ids.add(revision_id)
                targets.append({
                    "revision_id": revision_id,
                    "segment_id": segment["id"],
                    "task_type": task.task_type,
                    "input_hash": target_input_hash(
                        revision_id, segment.get("segment_key", ""),
                        segment.get("content_hash", ""), task),
                    "estimated_tokens": _SEGMENT_EST_TOKENS,
                    "tier": model_tier or task.default_model_tier,
                })

    # Pair tasks: real pairs are generated AFTER extraction; the plan reports
    # a bounded deterministic estimate from what exists today.
    pair_estimates: Dict[str, int] = {}
    for task in pair_tasks:
        existing = store.count_candidates(workspace_id,
                                          lifecycle_status="active")
        from .pairs import MAX_RELATION_PAIRS, MAX_CONFLICT_PAIRS, PAIR_BATCH
        cap = (MAX_CONFLICT_PAIRS
               if task.task_type == "conflict-candidate-analysis"
               else MAX_RELATION_PAIRS)
        est_pairs = min(cap, max(0, existing))
        est_requests = (est_pairs + PAIR_BATCH - 1) // PAIR_BATCH
        pair_estimates[task.task_type] = est_requests

    # -- cache-hit estimate (input-hash match; the runner's key is stricter)
    cache_hits = 0
    if policy.get("local_cache_enabled", True) and not force:
        for target in targets:
            if store.cache_find_by_input(target["task_type"],
                                         target["input_hash"]):
                cache_hits += 1

    strong_requests = sum(
        1 for t in targets if t["tier"] == ModelTier.STRONG)
    strong_requests += sum(
        pair_estimates.get(t.task_type, 0) for t in pair_tasks
        if (model_tier or t.default_model_tier) == ModelTier.STRONG)
    estimated_requests = (len(targets) - cache_hits
                          + sum(pair_estimates.values()))
    estimated_input_tokens = sum(t["estimated_tokens"] for t in targets)
    estimated_input_tokens += sum(pair_estimates.values()) * _PAIR_EST_TOKENS

    # -- policy + budget dry-run ------------------------------------------
    from .errors import SemanticError
    from .policy import authorize, effective_budgets
    try:
        auth = authorize(workspace_id, task_type="plan", policy=policy)
        policy_result: Dict[str, Any] = {"allowed": True,
                                         **auth["decision"]}
    except SemanticError as exc:
        policy_result = {"allowed": False, "code": exc.code,
                         "reason": exc.message}
    merged_budgets = effective_budgets(policy, budgets)
    violations: List[str] = []
    def _cap_check(key: str, value: float) -> None:
        cap = merged_budgets.get(key)
        if cap is not None and value > float(cap):
            violations.append(f"{key}: estimated {value} exceeds {cap}")
    _cap_check("max_requests_per_run", estimated_requests)
    _cap_check("max_total_input_tokens_per_run", estimated_input_tokens)
    _cap_check("max_strong_model_requests_per_run", strong_requests)

    return {
        "workspace_id": workspace_id,
        "tasks": [t.task_type for t in tasks],
        "scope": scope,
        "asset_count": len(assets),
        "revision_count": len(revision_ids),
        "target_count": len(targets),
        "evidence_count": evidence_count,
        "estimated_input_tokens": estimated_input_tokens,
        "token_estimate_basis": "estimated (local chars/4 heuristic, not "
                                "provider-billed usage)",
        "estimated_request_count": estimated_requests,
        "strong_model_request_count": strong_requests,
        "pair_request_estimates": pair_estimates,
        "cache_hit_count": cache_hits,
        "policy_result": policy_result,
        "budget_result": {"ok": not violations, "violations": violations,
                          "budgets": merged_budgets},
        "excluded_count": excluded_total,
        "excluded": excluded,
        "lens_id": (lens or {}).get("id"),
        "provider_calls_made": 0,
        "targets": targets,
    }


__all__ = ["plan_analysis", "target_input_hash", "MAX_PLAN_TARGETS"]
