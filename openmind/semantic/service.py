"""SemanticAnalysisService — the application service for the semantic plane.

Exposed as ``runtime.semantic`` / ``ServiceContainer.semantic`` and shared by
the CLI, REST and (read-only subset) MCP adapters. Every method is
workspace-scoped: the workspace is validated first (typed
:class:`WorkspaceNotFound`), then every read goes through the scoped store
queries — a candidate id from workspace A resolves to nothing through
workspace B.

Analysis is explicit: nothing here is called by ingestion. ``plan`` writes
nothing and calls no provider; ``start`` creates the run + targets and hands
them to the SAME single job worker every other background verb uses;
``resume`` re-enqueues an existing run and completed targets stay completed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from .. import db
from ..domain.errors import InvalidRequest, WorkspaceNotFound
from . import ANALYZER_VERSION, planner, store
from .errors import AnalysisRunNotFound, CandidateNotFound
from .models import (DataClassification, ModelTier, ReviewDecision,
                     ReviewStatus, SemanticRunStatus)
from .policy import authorize, effective_budgets, validate_budgets
from .tasks import ANALYSIS_TASK_TYPES, DEFAULT_TASK_TYPES, require_task
from .prompts import prompt_hash as compute_prompt_hash

MAX_REVIEW_NOTE_CHARS = 2_000
MAX_LIST_LIMIT = 500

_REVIEW_STATUS_BY_DECISION = {
    ReviewDecision.CONFIRM: ReviewStatus.CONFIRMED,
    ReviewDecision.REJECT: ReviewStatus.REJECTED,
    ReviewDecision.RESET: ReviewStatus.UNREVIEWED,
}


class SemanticAnalysisService:
    """Use cases over the Phase 4 semantic plane."""

    def __init__(self, workspaces: Any, jobs: Any,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        self._ensure_worker = ensure_worker

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    @staticmethod
    def _bound(limit: Any, hard: int = MAX_LIST_LIMIT) -> int:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = hard
        return max(1, min(limit, hard))

    @staticmethod
    def _clean_tasks(task_types: Optional[List[str]]) -> List[str]:
        names = [str(t).strip().lower() for t in (task_types or []) if
                 str(t).strip()]
        if not names:
            names = list(DEFAULT_TASK_TYPES)
        for name in names:
            task = require_task(name)
            if task.task_type not in ANALYSIS_TASK_TYPES:
                raise InvalidRequest(
                    f"task {name!r} is not runnable through semantic "
                    f"analysis (lens induction has its own commands)",
                    details={"available": sorted(ANALYSIS_TASK_TYPES)})
        seen: List[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        return seen

    # -- policy -------------------------------------------------------------
    def get_policy(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return store.get_policy(workspace_id)

    def set_policy(self, workspace_id: str, *,
                   data_classification: Optional[str] = None,
                   allow_remote: Optional[bool] = None,
                   provider_profile: Optional[str] = None,
                   local_cache_enabled: Optional[bool] = None,
                   task_models: Optional[Dict[str, str]] = None,
                   budgets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Update the workspace policy. Unset arguments keep the stored (or
        default) value; classification and budget keys are validated against
        their closed vocabularies."""
        self._require_workspace(workspace_id)
        current = store.get_policy(workspace_id)
        if data_classification is not None:
            clean = str(data_classification).strip().lower()
            if clean not in DataClassification.VALUES:
                raise InvalidRequest(
                    f"unknown data classification: {data_classification!r}",
                    details={"allowed": sorted(DataClassification.VALUES)})
            current["data_classification"] = clean
        if allow_remote is not None:
            current["allow_remote"] = bool(allow_remote)
        if provider_profile is not None:
            current["provider_profile"] = str(provider_profile).strip()
        if local_cache_enabled is not None:
            current["local_cache_enabled"] = bool(local_cache_enabled)
        if task_models is not None:
            for task_name in task_models:
                require_task(task_name)
            current["task_models"] = {str(k): str(v)
                                      for k, v in task_models.items()}
        if budgets is not None:
            merged = dict(current.get("budgets") or {})
            merged.update(budgets)
            current["budgets"] = validate_budgets(
                {k: v for k, v in merged.items()})
        return store.set_policy(
            workspace_id,
            data_classification=current["data_classification"],
            allow_remote=current["allow_remote"],
            provider_profile=current["provider_profile"],
            local_cache_enabled=current["local_cache_enabled"],
            task_models=current.get("task_models") or {},
            budgets=current.get("budgets") or {})

    # -- planning and execution --------------------------------------------
    def plan_analysis(self, workspace_id: str, *,
                      task_types: Optional[List[str]] = None,
                      scope: Optional[Dict[str, Any]] = None,
                      provider_profile: str = "", model_tier: str = "",
                      budgets: Optional[Dict[str, Any]] = None,
                      force: bool = False,
                      include_targets: bool = False) -> Dict[str, Any]:
        """The deterministic dry-run. Zero provider calls, zero writes."""
        self._require_workspace(workspace_id)
        tasks = self._clean_tasks(task_types)
        if model_tier and model_tier not in ModelTier.VALUES:
            raise InvalidRequest(f"unknown model tier: {model_tier!r}",
                                 details={"allowed": sorted(ModelTier.VALUES)})
        policy = store.get_policy(workspace_id)
        if provider_profile:
            policy = dict(policy)
            policy["provider_profile"] = provider_profile
        lens = store.get_active_lens(workspace_id)
        plan = planner.plan_analysis(
            workspace_id, task_types=tasks, scope=scope,
            model_tier=model_tier, lens=lens, policy=policy,
            budgets=budgets, force=force)
        if not include_targets:
            plan = {k: v for k, v in plan.items() if k != "targets"}
        return plan

    def start_analysis(self, workspace_id: str, *,
                       task_types: Optional[List[str]] = None,
                       scope: Optional[Dict[str, Any]] = None,
                       provider_profile: str = "", model_tier: str = "",
                       budgets: Optional[Dict[str, Any]] = None,
                       force: bool = False, wait: bool = False,
                       timeout: float = 3600.0) -> Dict[str, Any]:
        """Plan, gate, persist the run + targets, enqueue the job."""
        self._require_workspace(workspace_id)
        tasks = self._clean_tasks(task_types)
        policy = store.get_policy(workspace_id)
        profile_name = (provider_profile or policy.get("provider_profile")
                        or "").strip()
        # The gate runs BEFORE anything is written or serialized; it raises
        # the typed policy/config/auth error on refusal.
        auth = authorize(workspace_id, task_type="start-analysis",
                         profile_name=profile_name or None, policy=policy)
        profile = auth["profile"]
        merged_budgets = effective_budgets(policy, budgets)

        lens = store.get_active_lens(workspace_id)
        plan = planner.plan_analysis(
            workspace_id, task_types=tasks, scope=scope,
            model_tier=model_tier, lens=lens, policy=policy,
            budgets=budgets, force=force)
        prompt_versions = {t: require_task(t).prompt_version for t in tasks}
        input_hash = hashlib.sha256(json.dumps(
            [t["input_hash"] for t in plan["targets"]],
            sort_keys=True).encode("utf-8")).hexdigest()

        run = store.create_run(
            workspace_id, run_type="analysis",
            scope={**(scope or {"kind": "all"}), "force": bool(force)},
            provider_profile=profile.name, provider_kind=profile.kind,
            model_tier=model_tier or "", task_set=tasks,
            task_version=json.dumps(
                {t: require_task(t).task_version for t in tasks},
                sort_keys=True),
            prompt_set_version=json.dumps(prompt_versions, sort_keys=True),
            analyzer_version=ANALYZER_VERSION, input_hash=input_hash,
            budget=merged_budgets, lens_id=(lens or {}).get("id"),
            status=SemanticRunStatus.QUEUED)
        store.create_targets(run["id"], plan["targets"])

        job = self._enqueue(workspace_id, run["id"], tasks, scope,
                            profile.name, model_tier, budgets, force)
        result: Dict[str, Any] = {
            "workspace_id": workspace_id, "run_id": run["id"],
            "job_id": job["job_id"],
            "plan": {k: v for k, v in plan.items() if k != "targets"},
            "waited": False,
        }
        if wait:
            outcome = self._jobs.wait_for_terminal(job["job_id"],
                                                   timeout=timeout)
            result["waited"] = True
            result["job_status"] = outcome.status
            result["completed"] = outcome.completed
            result["waited_seconds"] = round(outcome.waited_seconds, 3)
        result["run"] = self.get_run(workspace_id, run["id"])
        return result

    def _enqueue(self, workspace_id: str, run_id: str, tasks: List[str],
                 scope: Optional[Dict[str, Any]], profile: str, tier: str,
                 budgets: Optional[Dict[str, Any]], force: bool
                 ) -> Dict[str, Any]:
        if self._ensure_worker:
            self._ensure_worker()
        from .. import jobs as jobs_engine
        return jobs_engine.enqueue_semantic_analysis(workspace_id, {
            "analysis_run_id": run_id, "workspace_id": workspace_id,
            "task_types": tasks, "scope": scope or {"kind": "all"},
            "provider_profile": profile, "model_tier": tier,
            "budget_overrides": budgets or {}, "force": bool(force),
        })

    def resume_analysis(self, workspace_id: str, run_id: str, *,
                        wait: bool = False,
                        timeout: float = 3600.0) -> Dict[str, Any]:
        """Re-enqueue an unfinished run. Completed targets stay completed;
        the runner skips them and reuses cached results."""
        self._require_workspace(workspace_id)
        run = store.get_run(workspace_id, run_id)
        if not run:
            raise AnalysisRunNotFound(f"analysis run not found: {run_id!r}",
                                      details={"run_id": run_id})
        if run["status"] in (SemanticRunStatus.DONE,
                             SemanticRunStatus.CANCELLED):
            raise InvalidRequest(
                f"run {run_id} is {run['status']} and cannot be resumed",
                details={"run_id": run_id, "status": run["status"]})
        store.update_run(run_id, status=SemanticRunStatus.QUEUED, error="")
        scope = run.get("scope") or {}
        job = self._enqueue(workspace_id, run_id, list(run.get("task_set")
                                                       or []),
                            scope, run.get("provider_profile", ""),
                            run.get("model_tier", ""), None,
                            bool(scope.get("force")))
        result: Dict[str, Any] = {"workspace_id": workspace_id,
                                  "run_id": run_id, "job_id": job["job_id"],
                                  "waited": False}
        if wait:
            outcome = self._jobs.wait_for_terminal(job["job_id"],
                                                   timeout=timeout)
            result["waited"] = True
            result["job_status"] = outcome.status
            result["completed"] = outcome.completed
        result["run"] = self.get_run(workspace_id, run_id)
        return result

    # -- runs ---------------------------------------------------------------
    def list_runs(self, workspace_id: str, limit: int = 50,
                  offset: int = 0,
                  status: Optional[str] = None) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        runs = store.list_runs(workspace_id, limit=self._bound(limit),
                               offset=max(0, int(offset)), status=status)
        return {"workspace_id": workspace_id, "runs": runs,
                "count": len(runs)}

    def get_run(self, workspace_id: str, run_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        run = store.get_run(workspace_id, run_id)
        if not run:
            raise AnalysisRunNotFound(f"analysis run not found: {run_id!r}",
                                      details={"run_id": run_id})
        run["targets"] = store.target_status_counts(run_id)
        run["usage"] = store.usage_totals(run_id)
        return run

    # -- candidates ---------------------------------------------------------
    def list_candidates(self, workspace_id: str, *,
                        candidate_kind: Optional[str] = None,
                        candidate_type: Optional[str] = None,
                        review_status: Optional[str] = None,
                        lifecycle_status: Optional[str] = None,
                        run_id: Optional[str] = None, limit: int = 100,
                        offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        # Counts must not report candidates whose sources moved on — the
        # cheap, indexed reconciliation runs first (spec §22).
        store.reconcile_staleness(workspace_id)
        rows = store.list_candidates(
            workspace_id, candidate_kind=candidate_kind,
            candidate_type=candidate_type, review_status=review_status,
            lifecycle_status=lifecycle_status, run_id=run_id,
            limit=self._bound(limit), offset=max(0, int(offset)))
        total = store.count_candidates(
            workspace_id, candidate_kind=candidate_kind,
            candidate_type=candidate_type, review_status=review_status,
            lifecycle_status=lifecycle_status, run_id=run_id)
        return {"workspace_id": workspace_id, "candidates": rows,
                "count": len(rows), "total": total}

    def get_candidate(self, workspace_id: str,
                      candidate_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_candidate(workspace_id, candidate_id)
        if not row:
            raise CandidateNotFound(
                f"semantic candidate not found: {candidate_id!r}",
                details={"candidate_id": candidate_id})
        return row

    def review_candidate(self, workspace_id: str, candidate_id: str, *,
                         decision: str, note: str = "",
                         reviewer: str = "") -> Dict[str, Any]:
        return self._review("candidate", workspace_id, candidate_id,
                            decision=decision, note=note, reviewer=reviewer)

    # -- relation candidates -------------------------------------------------
    def list_relation_candidates(self, workspace_id: str, *,
                                 relation_type: Optional[str] = None,
                                 review_status: Optional[str] = None,
                                 lifecycle_status: Optional[str] = None,
                                 run_id: Optional[str] = None,
                                 limit: int = 100,
                                 offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        store.reconcile_staleness(workspace_id)
        rows = store.list_relations(
            workspace_id, relation_type=relation_type,
            review_status=review_status, lifecycle_status=lifecycle_status,
            run_id=run_id, limit=self._bound(limit),
            offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "relations": rows,
                "count": len(rows)}

    def get_relation_candidate(self, workspace_id: str,
                               relation_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_relation(workspace_id, relation_id)
        if not row:
            raise CandidateNotFound(
                f"relation candidate not found: {relation_id!r}",
                details={"candidate_id": relation_id})
        return row

    def review_relation_candidate(self, workspace_id: str, relation_id: str,
                                  *, decision: str, note: str = "",
                                  reviewer: str = "") -> Dict[str, Any]:
        return self._review("relation", workspace_id, relation_id,
                            decision=decision, note=note, reviewer=reviewer)

    # -- conflict candidates --------------------------------------------------
    def list_conflict_candidates(self, workspace_id: str, *,
                                 category: Optional[str] = None,
                                 review_status: Optional[str] = None,
                                 lifecycle_status: Optional[str] = None,
                                 run_id: Optional[str] = None,
                                 limit: int = 100,
                                 offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        store.reconcile_staleness(workspace_id)
        rows = store.list_conflicts(
            workspace_id, category=category, review_status=review_status,
            lifecycle_status=lifecycle_status, run_id=run_id,
            limit=self._bound(limit), offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "conflicts": rows,
                "count": len(rows)}

    def get_conflict_candidate(self, workspace_id: str,
                               conflict_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_conflict(workspace_id, conflict_id)
        if not row:
            raise CandidateNotFound(
                f"conflict candidate not found: {conflict_id!r}",
                details={"candidate_id": conflict_id})
        return row

    def review_conflict_candidate(self, workspace_id: str, conflict_id: str,
                                  *, decision: str, note: str = "",
                                  reviewer: str = "") -> Dict[str, Any]:
        return self._review("conflict", workspace_id, conflict_id,
                            decision=decision, note=note, reviewer=reviewer)

    # -- shared review -------------------------------------------------------
    def _review(self, kind: str, workspace_id: str, entity_id: str, *,
                decision: str, note: str, reviewer: str) -> Dict[str, Any]:
        """Reviewing changes candidate METADATA only (spec §33): confirmation
        marks human suitability-for-promotion, it creates no canonical
        entity, and a stale confirmed candidate stays stale."""
        self._require_workspace(workspace_id)
        clean = str(decision or "").strip().lower()
        if clean not in ReviewDecision.VALUES:
            raise InvalidRequest(
                f"unknown review decision: {decision!r}",
                details={"allowed": sorted(ReviewDecision.VALUES)})
        note = str(note or "")
        if len(note) > MAX_REVIEW_NOTE_CHARS:
            raise InvalidRequest(
                f"review note exceeds {MAX_REVIEW_NOTE_CHARS} characters",
                details={"chars": len(note)})
        applied = store.apply_review(
            workspace_id, kind, entity_id,
            review_status=_REVIEW_STATUS_BY_DECISION[clean],
            review_note=note, reviewer=str(reviewer or "")[:200])
        if not applied:
            raise CandidateNotFound(
                f"{kind} candidate not found: {entity_id!r}",
                details={"candidate_id": entity_id})
        getter = {"candidate": self.get_candidate,
                  "relation": self.get_relation_candidate,
                  "conflict": self.get_conflict_candidate}[kind]
        return getter(workspace_id, entity_id)

    # -- usage + staleness ---------------------------------------------------
    def get_usage(self, workspace_id: str, run_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        run = store.get_run(workspace_id, run_id)
        if not run:
            raise AnalysisRunNotFound(f"analysis run not found: {run_id!r}",
                                      details={"run_id": run_id})
        return {"workspace_id": workspace_id, "run_id": run_id,
                "totals": store.usage_totals(run_id),
                "requests": store.list_usage(run_id)}

    def reconcile_staleness(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = store.reconcile_staleness(workspace_id)
        return {"workspace_id": workspace_id, **result}


__all__ = ["SemanticAnalysisService", "MAX_REVIEW_NOTE_CHARS"]
