"""The analysis pipeline: bounded map/reduce over checkpointed targets.

    1. re-authorize (policy gate — before any content is touched)
    2. extraction targets, one at a time:
         cache lookup -> [budget precheck -> provider -> ledger] ->
         local schema validation -> evidence verification -> dedup ->
         ONE transactional candidate commit -> target checkpoint
    3. deterministic pair generation (relations, then conflicts) -> the same
       loop over pair-batch targets
    4. summary

FAILURE ISOLATION, EXACTLY AS SPECIFIED
---------------------------------------
* a target's own failure (timeout, malformed output, failed validation)
  marks THAT target failed and the run continues;
* authentication / configuration / policy failures abort the run — they
  cannot heal per-target and retrying would spray identical errors;
* budget exhaustion stops creating provider requests, preserves everything
  completed, marks remaining targets ``skipped`` with the budget reason and
  the run ``partial`` with ``budget_exhausted`` — never "done";
* pause/terminate checkpoints run BETWEEN targets: completed targets stay
  complete, and a resumed run re-enters exactly here and skips them.

Rejected provider output is kept only as a bounded diagnostic on the target
row — never the raw response.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import db
from . import ANALYZER_VERSION, cache, context as context_mod, pairs as pairs_mod
from . import prompts, schemas, store
from .errors import (ProviderAuthenticationError, ProviderConfigurationError,
                     ProviderPolicyBlocked, ProviderResponseValidationError,
                     ProviderStructuredOutputError, SemanticBudgetExceeded,
                     SemanticError)
from .models import (ModelTier, SemanticRequest, SemanticRunStatus,
                     TargetStatus)
from .policy import authorize
from .providers import registry
from .tasks import TaskDefinition, require_task
from .usage import BudgetTracker, estimate_cost
from .verifier import (verify_candidate, verify_conflict, verify_relation)

#: Bounded diagnostic length stored for a failed/rejected target.
MAX_TARGET_ERROR_CHARS = 500


class _RunAbort(Exception):
    """Internal: abort the whole run with a status + reason."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def execute_run(run_id: str, workspace_id: str, *,
                checkpoint: Optional[Callable[[], None]] = None,
                log: Optional[Callable[[str], None]] = None,
                progress: Optional[Callable[..., None]] = None,
                provider_transport: Any = None) -> Dict[str, Any]:
    """Execute (or resume) one analysis run to a terminal status.

    *checkpoint* raises the job engine's pause/terminate signals between
    targets; *provider_transport* is the test seam handed through to the
    provider adapter. Returns the stored summary.
    """
    say = log or (lambda _m: None)
    tick = progress or (lambda **_k: None)
    run = store.get_run(workspace_id, run_id)
    if not run:
        raise SemanticError(f"analysis run not found: {run_id!r}")

    policy = store.get_policy(workspace_id)
    force = bool((run.get("scope") or {}).get("force"))
    # The gate re-reads stored policy itself — a job payload cannot loosen it.
    auth = authorize(workspace_id, task_type="analysis-run",
                     profile_name=run.get("provider_profile") or None,
                     policy=policy)
    profile = auth["profile"]
    provider = registry.get_provider(profile.kind)
    classification = auth["decision"]["classification"]

    lens = (store.get_lens(workspace_id, run["lens_id"])
            if run.get("lens_id") else store.get_active_lens(workspace_id))
    lens_hash = cache.lens_definition_hash(lens)

    tracker = BudgetTracker(workspace_id, run.get("budget") or {})
    store.update_run(run_id, status=SemanticRunStatus.RUNNING,
                     started_at=run.get("started_at") or db.now(),
                     provider_kind=profile.kind, error="")

    counters = {
        "targets_total": 0, "targets_done": 0, "targets_cached": 0,
        "targets_failed": 0, "targets_skipped": 0,
        "candidates_created": 0, "candidates_rejected": 0,
        "relations_created": 0, "conflicts_created": 0,
        "provider_requests": 0, "cache_hits": 0,
        "pairs_dropped": 0,
    }
    budget_stop: Optional[str] = None
    outcome_status = SemanticRunStatus.DONE
    outcome_error = ""

    def _persist_progress() -> None:
        store.update_run(run_id, progress=dict(counters))
        tick(**counters)

    try:
        task_set = [str(t) for t in (run.get("task_set") or [])]
        unit_tasks = [require_task(t) for t in task_set
                      if require_task(t).granularity in ("segment",
                                                         "revision")]
        run_relations = "relation-candidate-analysis" in task_set
        run_conflicts = "conflict-candidate-analysis" in task_set

        # ------------------------------------------------------------------
        # Phase 1: per-segment / per-revision extraction
        # ------------------------------------------------------------------
        del unit_tasks  # target rows carry the task; the list was a check
        targets = store.list_targets(run_id)
        pending = [t for t in targets
                   if t["status"] not in TargetStatus.COMPLETED
                   and require_task(t["task_type"]).granularity != "pair"]
        counters["targets_total"] = len(targets)
        say(f"[semantic] {len(pending)} extraction target(s) to process "
            f"({len(targets) - len(pending)} already complete)")

        for target in pending:
            if checkpoint:
                checkpoint()
            if budget_stop:
                store.update_target(target["id"],
                                    status=TargetStatus.SKIPPED,
                                    error=budget_stop[:MAX_TARGET_ERROR_CHARS])
                counters["targets_skipped"] += 1
                continue
            try:
                budget_stop = _process_extraction_target(
                    workspace_id, run, target, profile=profile,
                    provider=provider, policy=policy, tracker=tracker,
                    lens_hash=lens_hash, classification=classification,
                    force=force, counters=counters,
                    provider_transport=provider_transport)
            except _RunAbort:
                raise
            _persist_progress()

        # ------------------------------------------------------------------
        # Phase 2 + 3: bounded pair analysis (relations, then conflicts)
        # ------------------------------------------------------------------
        for task_type, enabled, generator in (
                ("relation-candidate-analysis", run_relations,
                 pairs_mod.relation_pairs),
                ("conflict-candidate-analysis", run_conflicts,
                 pairs_mod.conflict_pairs)):
            if not enabled or budget_stop:
                continue
            generated = generator(workspace_id)
            counters["pairs_dropped"] += int(generated.get("dropped") or 0)
            batches = _batch_pairs(generated["pairs"])
            say(f"[semantic] {task_type}: {len(generated['pairs'])} pair(s) "
                f"in {len(batches)} request(s)"
                + (f", {generated['dropped']} dropped by bounds"
                   if generated.get("dropped") else ""))
            for batch in batches:
                if checkpoint:
                    checkpoint()
                if budget_stop:
                    break
                budget_stop = _process_pair_target(
                    workspace_id, run, task_type, batch, profile=profile,
                    provider=provider, policy=policy, tracker=tracker,
                    lens_hash=lens_hash, classification=classification,
                    force=force, counters=counters,
                    provider_transport=provider_transport)
                _persist_progress()

        if budget_stop:
            outcome_status = SemanticRunStatus.PARTIAL
            outcome_error = "budget_exhausted"
    except _RunAbort as abort:
        outcome_status = abort.status
        outcome_error = abort.reason[:MAX_TARGET_ERROR_CHARS]
    except (ProviderPolicyBlocked, ProviderAuthenticationError,
            ProviderConfigurationError) as exc:
        outcome_status = SemanticRunStatus.FAILED
        outcome_error = f"{exc.code}: {exc.message}"[:MAX_TARGET_ERROR_CHARS]
    except BaseException:
        # Pause/terminate signals and unexpected errors: leave the run
        # resumable; the job layer records its own status.
        _persist_progress()
        raise

    # ----------------------------------------------------------------------
    # Summary — an honest one: unprocessed AND failed targets both demote the
    # run to PARTIAL; "done" means every target genuinely completed.
    # ----------------------------------------------------------------------
    all_targets = store.list_targets(run_id)
    remaining = [t for t in all_targets
                 if t["status"] == TargetStatus.PENDING]
    failed = [t for t in all_targets
              if t["status"] == TargetStatus.FAILED]
    if outcome_status == SemanticRunStatus.DONE and (remaining or failed):
        outcome_status = SemanticRunStatus.PARTIAL
        outcome_error = outcome_error or (
            f"{len(failed)} target(s) failed" if failed
            else "unprocessed targets remain")
    usage_totals = store.usage_totals(run_id)
    summary = {
        "status": outcome_status,
        "reason": outcome_error,
        "counters": dict(counters),
        "usage": usage_totals,
        "budget": tracker.snapshot(),
        "unprocessed_targets": [t["id"] for t in remaining][:100],
        "failed_targets": [t["id"] for t in failed][:100],
        "cache_hits": counters["cache_hits"],
        "lens_id": (lens or {}).get("id"),
    }
    store.update_run(run_id, status=outcome_status, error=outcome_error,
                     summary=summary, progress=dict(counters),
                     finished_at=db.now(),
                     model_name=profile.model_for_tier(
                         run.get("model_tier") or ModelTier.STANDARD))
    say(f"[semantic] run {run_id}: {outcome_status}"
        + (f" ({outcome_error})" if outcome_error else ""))
    return summary


# ---------------------------------------------------------------------------
# Extraction targets
# ---------------------------------------------------------------------------
def _process_extraction_target(workspace_id: str, run: Dict[str, Any],
                               target: Dict[str, Any], *, profile, provider,
                               policy: Dict[str, Any],
                               tracker: BudgetTracker, lens_hash: str,
                               classification: str, force: bool,
                               counters: Dict[str, int],
                               provider_transport: Any) -> Optional[str]:
    """Process one target. Returns a budget-stop reason or None."""
    task = require_task(target["task_type"])
    tier = run.get("model_tier") or task.default_model_tier
    revision = db.get_revision(workspace_id, target["revision_id"])
    asset = (db.get_asset(workspace_id, revision["asset_id"])
             if revision else None)
    if not revision or not asset:
        _fail_target(target, counters, "revision or asset no longer exists")
        return None

    if task.granularity == "revision":
        ctx = context_mod.build_revision_context(
            workspace_id, target["revision_id"], asset=asset)
    else:
        ctx = context_mod.build_segment_context(
            workspace_id, target["revision_id"], target["segment_id"],
            asset=asset)
    if ctx is None:
        store.update_target(target["id"], status=TargetStatus.SKIPPED,
                            error="no recoverable evidence content")
        counters["targets_skipped"] += 1
        return None

    packet = prompts.build_input_packet(task, ctx["untrusted"],
                                        ctx["context"])
    allowed_ids = frozenset(packet["allowedEvidenceIds"])
    ev_hashes = cache.evidence_content_hashes(
        workspace_id, packet["allowedEvidenceIds"])
    prompt_hash = prompts.prompt_hash(task)
    model_name = profile.model_for_tier(tier) or ""
    cache_key = cache.compute_cache_key(
        provider_kind=profile.kind, model_name=model_name,
        task_type=task.task_type, task_version=task.task_version,
        prompt_hash=prompt_hash, schema_version=task.schema_version,
        lens_hash=lens_hash,
        evidence_ids=packet["allowedEvidenceIds"],
        evidence_hashes=ev_hashes, options={"tier": tier})

    cached_output = cache.lookup(policy, cache_key, force=force)
    round_info: Dict[str, Any] = {}
    if cached_output is not None:
        counters["cache_hits"] += 1
        validated, provider_hint = cached_output, "cache"
    else:
        outcome = _provider_round(
            workspace_id, run, target, task, tier, packet, tracker,
            profile=profile, provider=provider,
            classification=classification, counters=counters,
            estimated_tokens=int(ctx["estimated_tokens"]),
            provider_transport=provider_transport, round_info=round_info)
        if isinstance(outcome, str):        # budget stop reason
            return outcome
        if outcome is None:                 # target-level failure, recorded
            return None
        validated, provider_hint = outcome, "provider"
        cache.put(policy, cache_key, provider_kind=profile.kind,
                  model_name=model_name, task_type=task.task_type,
                  prompt_hash=prompt_hash,
                  schema_version=task.schema_version,
                  input_hash=target.get("input_hash", ""),
                  output=validated)
    # Provenance prefers the model the RESPONSE reported (a mock or local
    # profile may configure no model name at all).
    model_name = round_info.get("model") or model_name or profile.kind

    # -- verification + dedup + one transactional commit -------------------
    existing = store.existing_candidate_keys(workspace_id,
                                             target["revision_id"])
    rows: List[Dict[str, Any]] = []
    rejected = 0
    diagnostics: List[str] = []
    for cand in validated.get("candidates") or []:
        verdict = verify_candidate(
            workspace_id, cand, allowed_types=task.allowed_candidate_types,
            allowed_evidence_ids=allowed_ids,
            resolver=context_mod.resolve_evidence_text)
        if not verdict.accepted:
            rejected += 1
            diagnostics.append(
                f"{cand.get('candidateType')}:{verdict.rejection_reason}")
            continue
        dedup_key = (cand["candidateType"], cand.get("stableKey", ""),
                     cand.get("title", ""))
        if dedup_key in existing:
            continue
        existing[dedup_key] = "pending"
        rows.append({
            "run_id": run["id"], "target_id": target["id"],
            "revision_id": target["revision_id"],
            "candidate_kind": task.candidate_kind,
            "candidate_type": cand["candidateType"],
            "stable_key": cand.get("stableKey", ""),
            "title": cand.get("title", ""),
            "statement": cand.get("statement", ""),
            "payload": {"attributes": cand.get("attributes") or {},
                        "reason": cand.get("reason", ""),
                        "source": provider_hint},
            "model_confidence_hint": cand.get("confidenceHint", ""),
            "confidence": verdict.confidence,
            "evidence_status": verdict.evidence_status,
            "task_version": task.task_version,
            "prompt_version": task.prompt_version,
            "analyzer_version": ANALYZER_VERSION,
            "model_name": model_name,
            "evidence": [{"evidence_id": ev["evidence_id"],
                          "quote": ev["quote"],
                          "quote_hash": ev["quote_hash"]}
                         for ev in verdict.verified_evidence],
        })
    if rows:
        store.insert_candidates(workspace_id, rows)
    counters["candidates_created"] += len(rows)
    counters["candidates_rejected"] += rejected
    status = (TargetStatus.CACHED if provider_hint == "cache"
              else TargetStatus.DONE)
    if status == TargetStatus.CACHED:
        counters["targets_cached"] += 1
    else:
        counters["targets_done"] += 1
    store.update_target(
        target["id"], status=status,
        result_hash=_result_hash(validated),
        error=("; ".join(diagnostics)[:MAX_TARGET_ERROR_CHARS]
               if diagnostics else ""))
    return None


# ---------------------------------------------------------------------------
# Pair targets (relations / conflicts)
# ---------------------------------------------------------------------------
def _pair_packet(workspace_id: str, task: TaskDefinition,
                 batch: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The packet for one pair batch: each side's evidence text (bounded)
    plus the pair descriptors as structural context."""
    untrusted: List[Dict[str, Any]] = []
    seen_evidence: set = set()

    def _add_side(ref: Dict[str, str]) -> bool:
        if ref["kind"] == "candidate":
            cand = store.get_candidate(workspace_id, ref["id"])
            if not cand:
                return False
            for ev in (cand.get("evidence") or [])[:2]:
                if ev["evidence_id"] in seen_evidence:
                    continue
                text = context_mod.resolve_evidence_text(
                    workspace_id, ev["evidence_id"])
                if text:
                    seen_evidence.add(ev["evidence_id"])
                    untrusted.append({"evidenceId": ev["evidence_id"],
                                      "locator": {},
                                      "text": text})
            return True
        if ref["kind"] == "symbol":
            segment = db.get_segment(workspace_id, ref["id"])
            if not segment:
                return False
            ev = db.get_evidence_for_segment(workspace_id, segment["id"])
            if ev and ev["id"] not in seen_evidence:
                text = context_mod.resolve_evidence_text(workspace_id,
                                                         ev["id"])
                if text:
                    seen_evidence.add(ev["id"])
                    untrusted.append({"evidenceId": ev["id"], "locator": {},
                                      "text": text})
            return True
        return True

    usable_pairs = [p for p in batch
                    if _add_side(p["sourceRef"]) and _add_side(p["targetRef"])]
    if not usable_pairs or not untrusted:
        return None
    packet = prompts.build_input_packet(
        task, untrusted,
        {"pairs": [{"sourceRef": p["sourceRef"],
                    "targetRef": p["targetRef"],
                    "signal": p["signal"], "sharedKey": p["sharedKey"]}
                   for p in usable_pairs]})
    return {"packet": packet, "pairs": usable_pairs,
            "estimated_tokens": context_mod.estimate_tokens(
                json.dumps(packet))}


def _batch_pairs(pairs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    return [pairs[i:i + pairs_mod.PAIR_BATCH]
            for i in range(0, len(pairs), pairs_mod.PAIR_BATCH)]


def _process_pair_target(workspace_id: str, run: Dict[str, Any],
                         task_type: str, batch: List[Dict[str, Any]], *,
                         profile, provider, policy: Dict[str, Any],
                         tracker: BudgetTracker, lens_hash: str,
                         classification: str, force: bool,
                         counters: Dict[str, int],
                         provider_transport: Any) -> Optional[str]:
    task = require_task(task_type)
    tier = run.get("model_tier") or task.default_model_tier
    batch_key = pairs_mod.pair_batch_key(batch)
    target_ids = store.create_targets(run["id"], [{
        "revision_id": "", "segment_id": f"pairs:{batch_key}",
        "task_type": task_type}])
    target = next(t for t in store.list_targets(run["id"])
                  if t["id"] == target_ids[0])
    counters["targets_total"] = len(store.list_targets(run["id"]))
    if target["status"] in TargetStatus.COMPLETED:
        return None

    built = _pair_packet(workspace_id, task, batch)
    if built is None:
        store.update_target(target["id"], status=TargetStatus.SKIPPED,
                            error="no recoverable evidence for any pair")
        counters["targets_skipped"] += 1
        return None
    packet = built["packet"]
    allowed_ids = frozenset(packet["allowedEvidenceIds"])
    signal_by_pair = {
        (p["sourceRef"]["kind"], p["sourceRef"]["id"], p["sourceRef"]["key"],
         p["targetRef"]["kind"], p["targetRef"]["id"], p["targetRef"]["key"]):
        p["signal"] for p in built["pairs"]}

    prompt_hash = prompts.prompt_hash(task)
    model_name = profile.model_for_tier(tier) or ""
    ev_hashes = cache.evidence_content_hashes(
        workspace_id, packet["allowedEvidenceIds"])
    cache_key = cache.compute_cache_key(
        provider_kind=profile.kind, model_name=model_name,
        task_type=task.task_type, task_version=task.task_version,
        prompt_hash=prompt_hash, schema_version=task.schema_version,
        lens_hash=lens_hash, evidence_ids=packet["allowedEvidenceIds"],
        evidence_hashes=ev_hashes,
        options={"tier": tier, "pairs": batch_key})

    cached_output = cache.lookup(policy, cache_key, force=force)
    if cached_output is not None:
        counters["cache_hits"] += 1
        validated, from_cache = cached_output, True
    else:
        outcome = _provider_round(
            workspace_id, run, target, task, tier, packet, tracker,
            profile=profile, provider=provider,
            classification=classification, counters=counters,
            estimated_tokens=int(built["estimated_tokens"]),
            provider_transport=provider_transport)
        if isinstance(outcome, str):
            return outcome
        if outcome is None:
            return None
        validated, from_cache = outcome, False
        cache.put(policy, cache_key, provider_kind=profile.kind,
                  model_name=model_name, task_type=task.task_type,
                  prompt_hash=prompt_hash,
                  schema_version=task.schema_version,
                  input_hash=target.get("input_hash", "") or batch_key,
                  output=validated)

    # -- verify + persist ---------------------------------------------------
    diagnostics: List[str] = []
    if task_type == "relation-candidate-analysis":
        rows = []
        for rel in validated.get("relations") or []:
            pair_id = (rel["sourceRef"]["kind"], rel["sourceRef"]["id"],
                       rel["sourceRef"]["key"], rel["targetRef"]["kind"],
                       rel["targetRef"]["id"], rel["targetRef"]["key"])
            signal = signal_by_pair.get(pair_id)
            if signal is None:
                diagnostics.append("relation names a pair that was not "
                                   "requested; dropped")
                counters["candidates_rejected"] += 1
                continue
            verdict = verify_relation(
                workspace_id, rel, allowed_evidence_ids=allowed_ids,
                resolver=context_mod.resolve_evidence_text,
                pair_signal=signal)
            if not verdict.accepted:
                diagnostics.append(f"relation:{verdict.rejection_reason}")
                counters["candidates_rejected"] += 1
                continue
            rows.append({
                "run_id": run["id"], "target_id": target["id"],
                "relation_type": rel["relationType"],
                "source_ref": rel["sourceRef"],
                "target_ref": rel["targetRef"],
                "source_candidate_id": (rel["sourceRef"]["id"]
                                        if rel["sourceRef"]["kind"] ==
                                        "candidate" else None),
                "target_candidate_id": (rel["targetRef"]["id"]
                                        if rel["targetRef"]["kind"] ==
                                        "candidate" else None),
                "reason": rel.get("reason", ""),
                "model_confidence_hint": rel.get("confidenceHint", ""),
                "confidence": verdict.confidence,
                "evidence_status": verdict.evidence_status,
                "payload": {"signal": signal},
                "evidence": [{"evidence_id": ev["evidence_id"],
                              "quote": ev["quote"],
                              "quote_hash": ev["quote_hash"]}
                             for ev in verdict.verified_evidence],
            })
        if rows:
            store.insert_relations(workspace_id, rows)
        counters["relations_created"] += len(rows)
    else:
        rows = []
        for conf in validated.get("conflicts") or []:
            verdict = verify_conflict(
                workspace_id, conf, allowed_evidence_ids=allowed_ids,
                resolver=context_mod.resolve_evidence_text)
            if not verdict.accepted:
                diagnostics.append(f"conflict:{verdict.rejection_reason}")
                counters["candidates_rejected"] += 1
                continue
            refs = conf.get("refs") or []
            candidate_refs = [r["id"] for r in refs
                              if r.get("kind") == "candidate" and r.get("id")]
            rows.append({
                "run_id": run["id"], "target_id": target["id"],
                "category": conf["category"], "refs": refs,
                "left_candidate_id": (candidate_refs[0]
                                      if candidate_refs else None),
                "right_candidate_id": (candidate_refs[1]
                                       if len(candidate_refs) > 1 else None),
                "explanation": conf.get("explanation", ""),
                "model_confidence_hint": conf.get("confidenceHint", ""),
                "confidence": verdict.confidence,
                "evidence_status": verdict.evidence_status,
                "payload": {"reason": conf.get("reason", "")},
                "evidence": [{"evidence_id": ev["evidence_id"],
                              "quote": ev["quote"],
                              "quote_hash": ev["quote_hash"]}
                             for ev in verdict.verified_evidence],
            })
        if rows:
            store.insert_conflicts(workspace_id, rows)
        counters["conflicts_created"] += len(rows)

    status = TargetStatus.CACHED if from_cache else TargetStatus.DONE
    if from_cache:
        counters["targets_cached"] += 1
    else:
        counters["targets_done"] += 1
    store.update_target(
        target["id"], status=status, result_hash=_result_hash(validated),
        error=("; ".join(diagnostics)[:MAX_TARGET_ERROR_CHARS]
               if diagnostics else ""))
    return None


# ---------------------------------------------------------------------------
# The provider round (shared by both target kinds)
# ---------------------------------------------------------------------------
def _provider_round(workspace_id: str, run: Dict[str, Any],
                    target: Dict[str, Any], task: TaskDefinition, tier: str,
                    packet: Dict[str, Any], tracker: BudgetTracker, *,
                    profile, provider, classification: str,
                    counters: Dict[str, int], estimated_tokens: int,
                    provider_transport: Any,
                    round_info: Optional[Dict[str, Any]] = None):
    """One budgeted provider request. Returns the VALIDATED output dict, a
    budget-stop string, or None after recording a target-level failure."""
    try:
        tracker.precheck(estimated_input_tokens=estimated_tokens,
                         max_output_tokens=task.max_output_tokens, tier=tier)
    except SemanticBudgetExceeded as exc:
        store.update_target(target["id"], status=TargetStatus.SKIPPED,
                            error=str(exc)[:MAX_TARGET_ERROR_CHARS])
        counters["targets_skipped"] += 1
        return f"budget_exhausted: {exc.message}"

    request = SemanticRequest(
        request_id=f"sqr_{uuid.uuid4().hex[:12]}",
        workspace_id=workspace_id, analysis_run_id=run["id"],
        task_type=task.task_type, model_tier=tier,
        system_instructions=prompts.system_instructions(task),
        input_packet=packet, schema_name=task.schema_name,
        schema_version=task.schema_version,
        prompt_version=task.prompt_version,
        max_output_tokens=task.max_output_tokens,
        timeout=profile.timeout,
        idempotency_key=f"{run['id']}:{target['id']}:{target['attempt']}",
        classification=classification)
    schema = schemas.get_schema(task.schema_name)
    store.update_target(target["id"], attempt=int(target["attempt"]) + 1)

    try:
        response = provider.generate_structured(
            request, schema, profile, transport=provider_transport)
    except (ProviderAuthenticationError, ProviderConfigurationError,
            ProviderPolicyBlocked):
        raise                                   # aborts the run (cannot heal)
    except SemanticError as exc:
        store.record_usage({
            "run_id": run["id"], "target_id": target["id"],
            "request_id": request.request_id,
            "provider_profile": profile.name, "provider_kind": profile.kind,
            "model_name": profile.model_for_tier(tier) or "",
            "task_type": task.task_type, "status": f"failed:{exc.code}",
            "retry_count": int(exc.details.get("retry_count") or 0)
            if exc.details else 0,
        })
        _fail_target(target, counters, f"{exc.code}: {exc.message}")
        return None

    counters["provider_requests"] += 1
    if round_info is not None:
        round_info["model"] = response.model
    cost, currency, cost_source = estimate_cost(
        profile.kind, response.model, response.input_tokens,
        response.output_tokens, response.cached_tokens)
    store.record_usage({
        "run_id": run["id"], "target_id": target["id"],
        "request_id": request.request_id,
        "provider_profile": profile.name, "provider_kind": profile.kind,
        "model_name": response.model, "task_type": task.task_type,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cached_tokens": response.cached_tokens,
        "estimated_cost": cost, "currency": currency,
        "cost_source": cost_source,
        "latency_ms": response.latency_ms,
        "retry_count": response.retry_count, "status": "ok",
        "request_hash": request.idempotency_key,
        "response_hash": response.raw_response_hash,
    })
    tracker.record(tier=tier, estimated_input_tokens=estimated_tokens,
                   input_tokens=response.input_tokens,
                   output_tokens=response.output_tokens,
                   estimated_cost=cost)

    try:
        return schemas.validate_output(task.schema_name,
                                       response.structured_output,
                                       task.allowed_candidate_types)
    except (ProviderResponseValidationError,
            ProviderStructuredOutputError) as exc:
        _fail_target(target, counters,
                     f"rejected output: {exc.code}: {exc.message}")
        return None


def _fail_target(target: Dict[str, Any], counters: Dict[str, int],
                 reason: str) -> None:
    store.update_target(target["id"], status=TargetStatus.FAILED,
                        error=reason[:MAX_TARGET_ERROR_CHARS])
    counters["targets_failed"] += 1


def _result_hash(validated: Dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(validated, sort_keys=True)
                          .encode("utf-8")).hexdigest()


__all__ = ["execute_run"]
