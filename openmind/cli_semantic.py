"""CLI adapters for the Phase 4 semantic plane: ``provider``, ``semantic``
and ``lens`` command groups.

Separate from :mod:`openmind.cli` for the same reason the semantic plane is a
package and not a jobs.py appendix — but the CONTRACT is exactly the parent
CLI's: ``--json`` prints one object on stdout, humans read stderr, no ANSI,
the shared exit codes, bounded output, and never a secret (there is no
``--api-key`` flag anywhere here; keys live in environment variables that
profiles reference by NAME).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional, Tuple

from .domain.errors import InvalidRequest

# Budget CLI flags -> policy budget keys. One visible table, so the CLI and
# the policy vocabulary cannot drift silently.
_BUDGET_FLAGS = {
    "max_requests": "max_requests_per_run",
    "max_input_tokens": "max_total_input_tokens_per_run",
    "max_output_tokens": "max_total_output_tokens_per_run",
    "max_strong_requests": "max_strong_model_requests_per_run",
    "max_request_input_tokens": "max_input_tokens_per_request",
    "max_request_output_tokens": "max_output_tokens_per_request",
    "max_cost": "max_estimated_cost_per_run",
    "max_daily_requests": "max_daily_requests",
    "max_daily_input_tokens": "max_daily_input_tokens",
    "max_daily_output_tokens": "max_daily_output_tokens",
}


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _budgets_from_args(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    budgets: Dict[str, Any] = {}
    if getattr(args, "budget_file", None):
        try:
            with open(args.budget_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise InvalidRequest(f"cannot read --budget-file: {exc}",
                                 details={"path": args.budget_file})
        if not isinstance(data, dict):
            raise InvalidRequest("--budget-file must contain a JSON object")
        budgets.update(data)
    for flag, key in _BUDGET_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            budgets[key] = value
    return budgets or None


def _scope_from_args(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    scope: Dict[str, Any] = {}
    if getattr(args, "scope", None):
        clean = str(args.scope).strip().lower()
        if clean not in ("documents", "code", "all"):
            raise InvalidRequest(
                f"unknown scope: {args.scope!r} (documents/code/all)")
        scope["kind"] = clean
    if getattr(args, "asset", None):
        scope["assets"] = list(args.asset)
    if getattr(args, "revision", None):
        scope["revisions"] = list(args.revision)
    return scope or None


def _tasks_from_args(args: argparse.Namespace):
    raw = getattr(args, "tasks", None)
    if not raw:
        return None
    return [t.strip() for t in str(raw).split(",") if t.strip()]


# ---------------------------------------------------------------------------
# provider commands
# ---------------------------------------------------------------------------
def cmd_provider_list(args, out) -> Tuple[int, Dict[str, Any]]:
    from .semantic.providers import profiles
    records = profiles.list_profiles()
    payload = _ok({"providers": records, "count": len(records)})

    def human(p: Dict[str, Any]) -> None:
        if not p["providers"]:
            print("no provider profiles configured "
                  f"({profiles.providers_file()})")
        for rec in p["providers"]:
            state = "enabled" if rec["enabled"] else "disabled"
            valid = "ok" if rec["validation"]["ok"] else "INVALID"
            print(f"  {rec['name']:20} {rec['kind']:14} {state:9} {valid}")
            for err in rec["validation"]["errors"]:
                print(f"      error:   {err}")
            for warn in rec["validation"]["warnings"]:
                print(f"      warning: {warn}")

    out.emit(payload, human)
    return 0, payload


def cmd_provider_configure(args, out) -> Tuple[int, Dict[str, Any]]:
    from .semantic.models import ProviderProfile
    from .semantic.providers import profiles
    existing = profiles.get_profile(args.name)
    models = dict(existing.models) if existing else {}
    for tier, value in (("fast", args.fast_model),
                        ("standard", args.standard_model),
                        ("strong", args.strong_model)):
        if value is not None:
            models[tier] = value
    metadata = dict(existing.metadata) if existing else {}
    if getattr(args, "azure_api_version", None):
        metadata["api_version"] = args.azure_api_version
    profile = ProviderProfile(
        name=args.name,
        kind=(args.kind or (existing.kind if existing else "")),
        endpoint=(args.endpoint if args.endpoint is not None
                  else (existing.endpoint if existing else "")),
        api_key_env=(args.api_key_env if args.api_key_env is not None
                     else (existing.api_key_env if existing else "")),
        models=models,
        max_data_classification=(
            args.max_classification if args.max_classification is not None
            else (existing.max_data_classification if existing
                  else "internal")),
        timeout=(args.timeout if args.timeout is not None
                 else (existing.timeout if existing else 120.0)),
        max_retries=(args.max_retries if args.max_retries is not None
                     else (existing.max_retries if existing else 2)),
        enabled=(False if args.disable
                 else True if args.enable
                 else (existing.enabled if existing else True)),
        metadata=metadata)
    record = profiles.upsert_profile(profile)
    payload = _ok({"provider": record})
    if not record["validation"]["ok"]:
        out.warn("profile saved but is not yet usable: "
                 + "; ".join(record["validation"]["errors"]))
    for warn in record["validation"]["warnings"]:
        out.warn(warn)
    out.emit(payload, lambda p: print(p["provider"]["name"]))
    return 0, payload


def cmd_provider_show(args, out) -> Tuple[int, Dict[str, Any]]:
    from .semantic.providers import profiles
    profile = profiles.require_profile(args.name)
    record = profile.as_dict()
    record["validation"] = profiles.validate_profile(profile).as_dict()
    record["expected_host"] = profiles.expected_host(profile)
    payload = _ok({"provider": record})

    def human(p: Dict[str, Any]) -> None:
        rec = p["provider"]
        print(f"{rec['name']}  ({rec['kind']}, "
              f"{'enabled' if rec['enabled'] else 'disabled'})")
        print(f"  endpoint:       {rec['endpoint'] or '(official host)'}")
        print(f"  host pinned to: {rec['expected_host'] or '(none)'}")
        print(f"  api key env:    {rec['api_key_env'] or '(none)'} "
              f"(value read at call time; never stored)")
        for tier in ("fast", "standard", "strong"):
            print(f"  model.{tier:9} {rec['models'].get(tier) or '(unset)'}")
        print(f"  max classification: {rec['max_data_classification']}")
        print(f"  valid: {rec['validation']['ok']}")
        for err in rec["validation"]["errors"]:
            print(f"    error:   {err}")
        for warn in rec["validation"]["warnings"]:
            print(f"    warning: {warn}")

    out.emit(payload, human)
    return 0, payload


def cmd_provider_validate(args, out) -> Tuple[int, Dict[str, Any]]:
    from .semantic.providers import profiles
    profile = profiles.require_profile(args.name)
    validation = profiles.validate_profile(profile)
    payload = _ok({"provider": args.name,
                   "validation": validation.as_dict(),
                   "network_calls_made": 0})
    out.emit(payload, lambda p: print(
        "valid" if p["validation"]["ok"] else
        "invalid: " + "; ".join(p["validation"]["errors"])))
    return (0 if validation.ok else 1), payload


def cmd_provider_test(args, out) -> Tuple[int, Dict[str, Any]]:
    from .semantic.providers import profiles
    profile = profiles.require_profile(args.name)
    validation = profiles.validate_profile(profile)
    if not validation.ok:
        payload = {"ok": False,
                   "error": {"code": "provider_configuration_error",
                             "message": "; ".join(validation.errors)},
                   "validation": validation.as_dict()}
        out.emit_error(payload["error"])
        return 2, payload
    if not args.live:
        payload = _ok({"provider": args.name, "live": False,
                       "validation": validation.as_dict(),
                       "note": "configuration test only; pass --live for an "
                               "explicit provider call (no project content "
                               "is ever sent)"})
        out.emit(payload, lambda p: print("configuration ok (dry)"))
        return 0, payload
    from .semantic.providers import live_test
    result = live_test.ping(profile)
    payload = _ok({"provider": args.name, "live": True, **result})
    out.emit(payload, lambda p: print(
        f"live test {'ok' if p.get('reachable') else 'FAILED'}: "
        f"{p.get('detail', '')}"))
    return (0 if result.get("reachable") else 1), payload


def cmd_provider_remove(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    from .semantic import store as semantic_store
    from .semantic.providers import profiles
    if profiles.get_profile(args.name) is None:
        raise InvalidRequest(f"unknown provider profile: {args.name!r}")
    if not args.force:
        dependents = []
        for workspace in get_runtime().workspaces.list():
            policy = semantic_store.get_policy(workspace["id"])
            if policy.get("provider_profile") == args.name:
                dependents.append(workspace["id"])
        if dependents:
            raise InvalidRequest(
                f"provider {args.name!r} is selected by workspace "
                f"polic{'ies' if len(dependents) > 1 else 'y'} "
                f"{', '.join(dependents)}; pass --force to remove anyway",
                details={"workspaces": dependents})
    removed = profiles.remove_profile(args.name)
    payload = _ok({"removed": args.name if removed else ""})
    out.emit(payload, lambda p: print(p["removed"] or "(nothing removed)"))
    return 0, payload


# ---------------------------------------------------------------------------
# semantic commands
# ---------------------------------------------------------------------------
def cmd_semantic_policy_show(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    policy = get_runtime().semantic.get_policy(args.workspace)
    payload = _ok({"policy": policy})

    def human(p: Dict[str, Any]) -> None:
        pol = p["policy"]
        print(f"workspace {pol['workspace_id']}")
        print(f"  classification: {pol['data_classification']}")
        print(f"  allow remote:   {pol['allow_remote']}")
        print(f"  provider:       {pol['provider_profile'] or '(none)'}")
        print(f"  local cache:    {pol['local_cache_enabled']}")
        for key, value in sorted((pol.get('budgets') or {}).items()):
            print(f"  budget {key}: {value}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_policy_set(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    allow_remote: Optional[bool] = None
    if args.allow_remote:
        allow_remote = True
    elif args.no_remote:
        allow_remote = False
    cache: Optional[bool] = None
    if args.local_cache:
        cache = True
    elif args.no_local_cache:
        cache = False
    policy = get_runtime().semantic.set_policy(
        args.workspace,
        data_classification=args.classification,
        allow_remote=allow_remote,
        provider_profile=args.provider,
        local_cache_enabled=cache,
        budgets=_budgets_from_args(args))
    payload = _ok({"policy": policy})
    out.emit(payload, lambda p: print(
        f"{p['policy']['data_classification']} remote="
        f"{p['policy']['allow_remote']} provider="
        f"{p['policy']['provider_profile'] or '-'}"))
    return 0, payload


def cmd_semantic_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    plan = get_runtime().semantic.plan_analysis(
        args.workspace, task_types=_tasks_from_args(args),
        scope=_scope_from_args(args),
        provider_profile=args.provider or "",
        model_tier=args.model_tier or "",
        budgets=_budgets_from_args(args), force=bool(args.force))
    payload = _ok({"plan": plan})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"plan for {plan['workspace_id']}: {plan['target_count']} "
              f"target(s) over {plan['revision_count']} revision(s)")
        print(f"  tasks:              {', '.join(plan['tasks'])}")
        print(f"  estimated requests: {plan['estimated_request_count']} "
              f"({plan['strong_model_request_count']} strong)")
        print(f"  estimated input:    ~{plan['estimated_input_tokens']} "
              f"tokens (estimated)")
        print(f"  cache hits:         {plan['cache_hit_count']}")
        print(f"  policy:             "
              f"{'allowed' if plan['policy_result'].get('allowed') else plan['policy_result'].get('reason', 'blocked')}")
        print(f"  budget:             "
              f"{'ok' if plan['budget_result']['ok'] else '; '.join(plan['budget_result']['violations'])}")
        if plan["excluded_count"]:
            print(f"  excluded:           {plan['excluded_count']} "
                  f"(first {len(plan['excluded'])} listed)")
            for entry in plan["excluded"][:10]:
                print(f"    - {entry.get('reason')}")
        print("  provider calls made by planning: 0")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_analyze(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    if args.wait:
        out.say("Starting the job worker...")
    result = get_runtime().semantic.start_analysis(
        args.workspace, task_types=_tasks_from_args(args),
        scope=_scope_from_args(args),
        provider_profile=args.provider or "",
        model_tier=args.model_tier or "",
        budgets=_budgets_from_args(args), force=bool(args.force),
        wait=bool(args.wait), timeout=args.timeout)
    run = result.get("run") or {}
    status = run.get("status", "")
    payload = _ok(result)
    exit_code = 0
    if args.wait and status not in ("done",):
        payload["ok"] = status == "partial"
        if status == "partial":
            out.warn(f"run {result['run_id']} is PARTIAL: "
                     f"{run.get('error', '')} — completed work is kept; "
                     f"resume with 'semantic resume'")
        else:
            payload["error"] = {"code": "run_" + (status or "unknown"),
                                "message": f"analysis run ended with status "
                                           f"{status!r}: {run.get('error', '')}"}
            out.warn(payload["error"]["message"])
            exit_code = 4

    def human(p: Dict[str, Any]) -> None:
        r = p.get("run") or {}
        print(f"run {p.get('run_id')} [{r.get('status', 'queued')}] "
              f"job {p.get('job_id')}")
        counters = (r.get("summary") or {}).get("counters") or {}
        if counters:
            print(f"  candidates: {counters.get('candidates_created', 0)} "
                  f"created, {counters.get('candidates_rejected', 0)} "
                  f"rejected; cache hits {counters.get('cache_hits', 0)}; "
                  f"provider requests "
                  f"{counters.get('provider_requests', 0)}")

    out.emit(payload, human)
    return exit_code, payload


def cmd_semantic_resume(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    if args.wait:
        out.say("Starting the job worker...")
    result = get_runtime().semantic.resume_analysis(
        args.workspace, args.run, wait=bool(args.wait), timeout=args.timeout)
    payload = _ok(result)
    out.emit(payload, lambda p: print(
        f"run {p['run_id']} [{(p.get('run') or {}).get('status', '')}]"))
    return 0, payload


def cmd_semantic_runs(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().semantic.list_runs(
        args.workspace, limit=args.limit, status=args.status)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        for run in p["runs"]:
            print(f"  {run['id']}  {run['status']:9} "
                  f"{','.join(run.get('task_set') or [])[:48]:50} "
                  f"{run['created_at']}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_show(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    run = get_runtime().semantic.get_run(args.workspace, args.run)
    payload = _ok({"run": run})

    def human(p: Dict[str, Any]) -> None:
        r = p["run"]
        print(f"{r['id']}  [{r['status']}]  {r.get('run_type')}")
        print(f"  provider: {r.get('provider_profile')} "
              f"({r.get('provider_kind')})")
        print(f"  tasks:    {', '.join(r.get('task_set') or [])}")
        print(f"  targets:  {r.get('targets')}")
        usage = r.get("usage") or {}
        print(f"  usage:    {usage.get('requests', 0)} request(s), "
              f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
              f"cost={usage.get('estimated_cost')}")
        if r.get("error"):
            print(f"  note:     {r['error']}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_candidates(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().semantic.list_candidates(
        args.workspace, candidate_kind=args.kind, candidate_type=args.type,
        review_status=args.review_status,
        lifecycle_status=args.lifecycle_status, run_id=args.run,
        limit=args.limit, offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} of {p['total']} candidate(s):")
        for cand in p["candidates"]:
            print(f"  {cand['id']}  {cand['candidate_type']:22} "
                  f"{cand['confidence']:6} {cand['evidence_status']:18} "
                  f"{cand['review_status']:10} {cand['lifecycle_status']:6} "
                  f"{(cand['stable_key'] or cand['title'])[:40]}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_candidate(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    candidate = get_runtime().semantic.get_candidate(args.workspace,
                                                     args.candidate)
    payload = _ok({"candidate": candidate})

    def human(p: Dict[str, Any]) -> None:
        c = p["candidate"]
        print(f"{c['id']}  {c['candidate_type']}  [{c['status']}]")
        print(f"  statement:  {c['statement'][:200]}")
        print(f"  key/title:  {c['stable_key'] or '-'} / {c['title'] or '-'}")
        print(f"  confidence: {c['confidence']} (model hint: "
              f"{c['model_confidence_hint'] or '-'}; local verification "
              f"decides)")
        print(f"  evidence:   {c['evidence_status']}, "
              f"{len(c.get('evidence') or [])} citation(s)")
        for ev in c.get("evidence") or []:
            print(f"    {ev['evidence_id']}: \"{ev['quote'][:80]}\"")
        print(f"  review:     {c['review_status']}"
              + (f" — {c['review_note']}" if c.get("review_note") else ""))
        print(f"  lifecycle:  {c['lifecycle_status']}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_review(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    semantic = get_runtime().semantic
    kind = args.kind or "candidate"
    review = {"candidate": semantic.review_candidate,
              "relation": semantic.review_relation_candidate,
              "conflict": semantic.review_conflict_candidate}[kind]
    record = review(args.workspace, args.candidate, decision=args.decision,
                    note=args.note or "", reviewer=args.reviewer or "cli")
    payload = _ok({"candidate": record})
    out.emit(payload, lambda p: print(
        f"{p['candidate']['id']} -> {p['candidate']['review_status']} "
        f"(still a candidate, not canonical truth)"))
    return 0, payload


def cmd_semantic_relations(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().semantic.list_relation_candidates(
        args.workspace, relation_type=args.type,
        review_status=args.review_status,
        lifecycle_status=args.lifecycle_status, limit=args.limit,
        offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} relation candidate(s):")
        for rel in p["relations"]:
            src = rel["source_ref"].get("key") or rel["source_ref"].get("id")
            dst = rel["target_ref"].get("key") or rel["target_ref"].get("id")
            print(f"  {rel['id']}  {rel['relation_type']:22} "
                  f"{rel['confidence']:6} {src} -> {dst}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_conflicts(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().semantic.list_conflict_candidates(
        args.workspace, category=args.category,
        review_status=args.review_status,
        lifecycle_status=args.lifecycle_status, limit=args.limit,
        offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} conflict CANDIDATE(s) — possible disagreements, "
              f"never confirmed conflicts:")
        for conf in p["conflicts"]:
            print(f"  {conf['id']}  {conf['category']:22} "
                  f"{conf['confidence']:6} {conf['explanation'][:80]}")

    out.emit(payload, human)
    return 0, payload


def cmd_semantic_usage(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().semantic.get_usage(args.workspace, args.run)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        totals = p["totals"]
        print(f"run {p['run_id']}: {totals['requests']} request(s)")
        print(f"  input tokens:  {totals['input_tokens']}")
        print(f"  output tokens: {totals['output_tokens']}")
        print(f"  cached tokens: {totals['cached_tokens']}")
        cost = totals["estimated_cost"]
        print(f"  est. cost:     "
              f"{cost if cost is not None else 'unknown (no configured price)'}")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# lens commands
# ---------------------------------------------------------------------------
def cmd_lens_list(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().lenses.list_lenses(args.workspace,
                                              source=args.source)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        for lens in p["lenses"]:
            stored = "stored" if lens.get("stored") else "virtual"
            print(f"  {str(lens.get('id') or lens.get('file', '')):28} "
                  f"{lens['source']:13} {lens.get('status', '-'):12} "
                  f"{stored:8} {lens['name']}")

    out.emit(payload, human)
    return 0, payload


def cmd_lens_show(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.get_lens(args.workspace, args.lens)
    payload = _ok({"lens": lens})

    def human(p: Dict[str, Any]) -> None:
        lens = p["lens"]
        print(f"{lens.get('id', '-')}  {lens['name']}  [{lens.get('status')}]"
              f"  source={lens['source']}")
        validation = lens.get("validation") or {}
        print(f"  validation: {validation.get('result', '-')}")
        for err in validation.get("errors") or []:
            print(f"    error:   {err}")
        for warn in validation.get("warnings") or []:
            print(f"    warning: {warn}")
        metrics = validation.get("metrics") or {}
        if metrics:
            print(f"  coverage: {metrics.get('asset_coverage')} "
                  f"overlap: {metrics.get('role_overlap')} "
                  f"identifier hits: {metrics.get('identifier_hits')}")

    out.emit(payload, human)
    return 0, payload


def cmd_lens_induce_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    plan = get_runtime().lenses.plan_induction(
        args.workspace, provider_profile=args.provider or "")
    payload = _ok({"plan": plan})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"{plan['sample_count']} sample(s), "
              f"{plan['segment_count']} segment(s), "
              f"~{plan['estimated_tokens']} tokens (estimated)")
        print(f"  omitted: {plan['omitted']}")
        print(f"  policy: {plan['policy_result']}")
        print("  provider calls made by planning: 0")

    out.emit(payload, human)
    return 0, payload


def cmd_lens_induce(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    if not getattr(args, "workspace", None):
        raise InvalidRequest("--workspace is required "
                             "(or use 'lens induce plan')")
    if args.wait:
        out.say("Starting the job worker...")
    result = get_runtime().lenses.start_induction(
        args.workspace, provider_profile=args.provider or "",
        wait=bool(args.wait), timeout=args.timeout)
    payload = _ok(result)
    exit_code = 0
    if args.wait and (result.get("run") or {}).get("status") != "done":
        payload["ok"] = False
        payload["error"] = {
            "code": "induction_failed",
            "message": (result.get("run") or {}).get("error", "")
                       or "induction did not complete"}
        out.warn(payload["error"]["message"])
        exit_code = 4

    def human(p: Dict[str, Any]) -> None:
        lens = p.get("lens")
        if lens:
            print(f"induced lens {lens['id']} [{lens['status']}] — "
                  f"provisional until validated, approved and activated")
        else:
            print(f"induction run {p.get('run_id')} "
                  f"[{(p.get('run') or {}).get('status', 'queued')}]")

    out.emit(payload, human)
    return exit_code, payload


def cmd_lens_validate(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.validate(args.workspace, args.lens)
    payload = _ok({"lens": lens})
    result = (lens.get("validation") or {}).get("result", "")
    out.emit(payload, lambda p: print(result))
    return (0 if result != "invalid" else 1), payload


def cmd_lens_approve(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.approve(args.workspace, args.lens)
    payload = _ok({"lens": lens})
    out.emit(payload, lambda p: print(f"{p['lens']['id']} approved "
                                      f"(activation is a separate step)"))
    return 0, payload


def cmd_lens_reject(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.reject(args.workspace, args.lens,
                                       reason=args.reason or "")
    payload = _ok({"lens": lens})
    out.emit(payload, lambda p: print(f"{p['lens']['id']} rejected"))
    return 0, payload


def cmd_lens_activate(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.activate(args.workspace, args.lens)
    payload = _ok({"lens": lens})
    out.emit(payload, lambda p: print(
        f"{p['lens']['id']} active (influences semantic planning only)"))
    return 0, payload


def cmd_lens_deactivate(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.deactivate(args.workspace, args.lens)
    payload = _ok({"lens": lens})
    out.emit(payload, lambda p: print(f"{p['lens']['id']} -> "
                                      f"{p['lens']['status']}"))
    return 0, payload


def cmd_lens_import(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    lens = get_runtime().lenses.import_organization_lens(args.workspace,
                                                         args.name)
    payload = _ok({"lens": lens})
    out.emit(payload, lambda p: print(f"{p['lens']['id']} imported "
                                      f"[{p['lens']['status']}]"))
    return 0, payload


def cmd_lens_export(args, out) -> Tuple[int, Dict[str, Any]]:
    from .runtime import get_runtime
    result = get_runtime().lenses.export_lens(args.workspace, args.lens,
                                              path=args.output or "")
    payload = _ok(result)
    out.emit(payload, lambda p: print(p.get("written_to")
                                      or json.dumps(p["definition"])[:200]))
    return 0, payload


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def _budget_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--budget-file", dest="budget_file", metavar="PATH",
                        help="JSON file of budget overrides")
    for flag in _BUDGET_FLAGS:
        parser.add_argument("--" + flag.replace("_", "-"), dest=flag,
                            type=float, default=None, metavar="N",
                            help=f"budget: {_BUDGET_FLAGS[flag]}")


def register(sub, common) -> None:
    """Attach the provider/semantic/lens groups to the parent CLI's
    subparsers. Called by :func:`openmind.cli.build_parser`."""
    # -- provider -----------------------------------------------------------
    provider = sub.add_parser("provider", parents=[common],
                              help="machine-local semantic provider profiles")
    provider_sub = provider.add_subparsers(dest="provider_command",
                                           metavar="<subcommand>")
    p_list = provider_sub.add_parser("list", parents=[common],
                                     help="list profiles with validation")
    p_list.set_defaults(func=cmd_provider_list)

    # allow_abbrev=False is load-bearing: with abbreviation on, argparse
    # would accept `--api-key <value>` as a PREFIX of --api-key-env and a
    # pasted secret would be stored as if it were a variable name.
    p_conf = provider_sub.add_parser(
        "configure", parents=[common], allow_abbrev=False,
        help="create or update a profile (never accepts a raw API key)")
    p_conf.add_argument("--name", required=True, help="profile name (slug)")
    p_conf.add_argument("--kind", help="local-openai | openai | anthropic | "
                                       "azure-openai | mock")
    p_conf.add_argument("--endpoint", help="explicit endpoint URL "
                        "(required for local-openai and azure-openai)")
    p_conf.add_argument("--api-key-env", dest="api_key_env",
                        help="NAME of the environment variable holding the "
                             "key (the value itself is never stored)")
    p_conf.add_argument("--fast-model", dest="fast_model")
    p_conf.add_argument("--standard-model", dest="standard_model")
    p_conf.add_argument("--strong-model", dest="strong_model")
    p_conf.add_argument("--max-classification", dest="max_classification",
                        help="public | internal | confidential | restricted")
    p_conf.add_argument("--timeout", type=float)
    p_conf.add_argument("--max-retries", dest="max_retries", type=int)
    p_conf.add_argument("--azure-api-version", dest="azure_api_version",
                        help="Azure OpenAI api_version (azure-openai only)")
    toggle = p_conf.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    p_conf.set_defaults(func=cmd_provider_configure)

    p_show = provider_sub.add_parser("show", parents=[common],
                                     help="show one profile")
    p_show.add_argument("--name", required=True)
    p_show.set_defaults(func=cmd_provider_show)

    p_val = provider_sub.add_parser(
        "validate", parents=[common],
        help="validate configuration (no network call)")
    p_val.add_argument("--name", required=True)
    p_val.set_defaults(func=cmd_provider_validate)

    p_test = provider_sub.add_parser(
        "test", parents=[common],
        help="test a profile; --live makes one explicit provider call "
             "(no project content is sent)")
    p_test.add_argument("--name", required=True)
    p_test.add_argument("--live", action="store_true")
    p_test.set_defaults(func=cmd_provider_test)

    p_rm = provider_sub.add_parser("remove", parents=[common],
                                   help="remove a profile")
    p_rm.add_argument("--name", required=True)
    p_rm.add_argument("--force", action="store_true",
                      help="remove even when a workspace policy selects it")
    p_rm.set_defaults(func=cmd_provider_remove)
    provider.set_defaults(func=None, _parser=provider)

    # -- semantic -----------------------------------------------------------
    semantic = sub.add_parser("semantic", parents=[common],
                              help="explicit, policy-gated semantic analysis")
    semantic_sub = semantic.add_subparsers(dest="semantic_command",
                                           metavar="<subcommand>")

    policy = semantic_sub.add_parser("policy", parents=[common],
                                     help="workspace semantic policy")
    policy_sub = policy.add_subparsers(dest="policy_command",
                                       metavar="<subcommand>")
    pol_show = policy_sub.add_parser("show", parents=[common])
    pol_show.add_argument("--workspace", required=True)
    pol_show.set_defaults(func=cmd_semantic_policy_show)
    pol_set = policy_sub.add_parser("set", parents=[common])
    pol_set.add_argument("--workspace", required=True)
    pol_set.add_argument("--classification",
                         help="public | internal | confidential | restricted")
    remote = pol_set.add_mutually_exclusive_group()
    remote.add_argument("--allow-remote", dest="allow_remote",
                        action="store_true")
    remote.add_argument("--no-remote", dest="no_remote", action="store_true")
    pol_set.add_argument("--provider", help="provider profile name")
    cache_group = pol_set.add_mutually_exclusive_group()
    cache_group.add_argument("--local-cache", dest="local_cache",
                             action="store_true")
    cache_group.add_argument("--no-local-cache", dest="no_local_cache",
                             action="store_true")
    _budget_flags(pol_set)
    pol_set.set_defaults(func=cmd_semantic_policy_set)
    policy.set_defaults(func=None, _parser=policy)

    def _analysis_args(parser, with_wait: bool) -> None:
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--tasks", help="comma-separated task types")
        parser.add_argument("--scope", help="documents | code | all")
        parser.add_argument("--asset", action="append", metavar="ASSET_ID",
                            help="restrict to this asset (repeatable)")
        parser.add_argument("--provider", help="provider profile override")
        parser.add_argument("--model-tier", dest="model_tier",
                            help="fast | standard | strong")
        parser.add_argument("--force", action="store_true",
                            help="bypass the local semantic cache")
        _budget_flags(parser)
        if with_wait:
            parser.add_argument("--wait", action="store_true")
            parser.add_argument("--timeout", type=float, default=3600.0,
                                metavar="SECONDS")

    s_plan = semantic_sub.add_parser("plan", parents=[common],
                                     help="dry-run plan (no provider call)")
    _analysis_args(s_plan, with_wait=False)
    s_plan.set_defaults(func=cmd_semantic_plan)

    s_analyze = semantic_sub.add_parser("analyze", parents=[common],
                                        help="run semantic analysis")
    _analysis_args(s_analyze, with_wait=True)
    s_analyze.set_defaults(func=cmd_semantic_analyze)

    s_resume = semantic_sub.add_parser("resume", parents=[common],
                                       help="resume an unfinished run")
    s_resume.add_argument("--workspace", required=True)
    s_resume.add_argument("--run", required=True)
    s_resume.add_argument("--wait", action="store_true")
    s_resume.add_argument("--timeout", type=float, default=3600.0)
    s_resume.set_defaults(func=cmd_semantic_resume)

    s_runs = semantic_sub.add_parser("runs", parents=[common],
                                     help="list analysis runs")
    s_runs.add_argument("--workspace", required=True)
    s_runs.add_argument("--limit", type=int, default=50)
    s_runs.add_argument("--status")
    s_runs.set_defaults(func=cmd_semantic_runs)

    s_show = semantic_sub.add_parser("show", parents=[common],
                                     help="show one analysis run")
    s_show.add_argument("--workspace", required=True)
    s_show.add_argument("--run", required=True)
    s_show.set_defaults(func=cmd_semantic_show)

    s_cands = semantic_sub.add_parser("candidates", parents=[common],
                                      help="list semantic candidates")
    s_cands.add_argument("--workspace", required=True)
    s_cands.add_argument("--type", help="candidate type filter")
    s_cands.add_argument("--kind", help="classification | "
                         "engineering-concept | revision-status")
    s_cands.add_argument("--review-status", dest="review_status")
    s_cands.add_argument("--lifecycle-status", dest="lifecycle_status")
    s_cands.add_argument("--run")
    s_cands.add_argument("--limit", type=int, default=100)
    s_cands.add_argument("--offset", type=int, default=0)
    s_cands.set_defaults(func=cmd_semantic_candidates)

    s_cand = semantic_sub.add_parser("candidate", parents=[common],
                                     help="show one candidate + evidence")
    s_cand.add_argument("--workspace", required=True)
    s_cand.add_argument("--candidate", required=True)
    s_cand.set_defaults(func=cmd_semantic_candidate)

    s_review = semantic_sub.add_parser(
        "review", parents=[common],
        help="review a candidate (confirm/reject/reset)")
    s_review.add_argument("--workspace", required=True)
    s_review.add_argument("--candidate", required=True)
    s_review.add_argument("--kind", choices=["candidate", "relation",
                                             "conflict"],
                          default="candidate")
    s_review.add_argument("--decision", required=True,
                          choices=["confirm", "reject", "reset"])
    s_review.add_argument("--note", help="bounded review note")
    s_review.add_argument("--reviewer", help="caller-supplied identifier")
    s_review.set_defaults(func=cmd_semantic_review)

    s_rels = semantic_sub.add_parser("relations", parents=[common],
                                     help="list relation candidates")
    s_rels.add_argument("--workspace", required=True)
    s_rels.add_argument("--type")
    s_rels.add_argument("--review-status", dest="review_status")
    s_rels.add_argument("--lifecycle-status", dest="lifecycle_status")
    s_rels.add_argument("--limit", type=int, default=100)
    s_rels.add_argument("--offset", type=int, default=0)
    s_rels.set_defaults(func=cmd_semantic_relations)

    s_confs = semantic_sub.add_parser("conflicts", parents=[common],
                                      help="list conflict candidates")
    s_confs.add_argument("--workspace", required=True)
    s_confs.add_argument("--category")
    s_confs.add_argument("--review-status", dest="review_status")
    s_confs.add_argument("--lifecycle-status", dest="lifecycle_status")
    s_confs.add_argument("--limit", type=int, default=100)
    s_confs.add_argument("--offset", type=int, default=0)
    s_confs.set_defaults(func=cmd_semantic_conflicts)

    s_usage = semantic_sub.add_parser("usage", parents=[common],
                                      help="one run's usage ledger")
    s_usage.add_argument("--workspace", required=True)
    s_usage.add_argument("--run", required=True)
    s_usage.set_defaults(func=cmd_semantic_usage)
    semantic.set_defaults(func=None, _parser=semantic)

    # -- lens ---------------------------------------------------------------
    lens = sub.add_parser("lens", parents=[common],
                          help="Adaptive Project Lenses")
    lens_sub = lens.add_subparsers(dest="lens_command",
                                   metavar="<subcommand>")
    l_list = lens_sub.add_parser("list", parents=[common],
                                 help="list lenses (stored + built-in + "
                                      "organization files)")
    l_list.add_argument("--workspace", required=True)
    l_list.add_argument("--source",
                        choices=["builtin", "organization", "induced"])
    l_list.set_defaults(func=cmd_lens_list)

    l_show = lens_sub.add_parser("show", parents=[common],
                                 help="show one lens with its validation")
    l_show.add_argument("--workspace", required=True)
    l_show.add_argument("--lens", required=True)
    l_show.set_defaults(func=cmd_lens_show)

    l_induce = lens_sub.add_parser(
        "induce", parents=[common],
        help="propose a lens from bounded samples (strong model)")
    induce_sub = l_induce.add_subparsers(dest="induce_command",
                                         metavar="<subcommand>")
    li_plan = induce_sub.add_parser("plan", parents=[common],
                                    help="deterministic sample plan "
                                         "(no provider call)")
    li_plan.add_argument("--workspace", required=True)
    li_plan.add_argument("--provider")
    li_plan.set_defaults(func=cmd_lens_induce_plan)
    l_induce.add_argument("--workspace")
    l_induce.add_argument("--provider")
    l_induce.add_argument("--wait", action="store_true")
    l_induce.add_argument("--timeout", type=float, default=3600.0)
    l_induce.set_defaults(func=cmd_lens_induce, _parser=l_induce)

    for name, func, help_text, extra in (
            ("validate", cmd_lens_validate,
             "run deterministic whole-corpus validation", ()),
            ("approve", cmd_lens_approve,
             "explicitly approve a validated lens", ()),
            ("reject", cmd_lens_reject, "reject a lens", ("reason",)),
            ("activate", cmd_lens_activate,
             "activate a lens (semantic planning only)", ()),
            ("deactivate", cmd_lens_deactivate,
             "deactivate the active lens", ())):
        p = lens_sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--workspace", required=True)
        p.add_argument("--lens", required=True)
        for arg in extra:
            p.add_argument("--" + arg)
        p.set_defaults(func=func)

    l_import = lens_sub.add_parser("import", parents=[common],
                                   help="import an organization lens file")
    l_import.add_argument("--workspace", required=True)
    l_import.add_argument("--name", required=True,
                          help="lens name or filename in the lens directory")
    l_import.set_defaults(func=cmd_lens_import)

    l_export = lens_sub.add_parser("export", parents=[common],
                                   help="export a lens definition as JSON")
    l_export.add_argument("--workspace", required=True)
    l_export.add_argument("--lens", required=True)
    l_export.add_argument("--output", help="write to this file")
    l_export.set_defaults(func=cmd_lens_export)
    lens.set_defaults(func=None, _parser=lens)
