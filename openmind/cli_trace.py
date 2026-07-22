"""CLI adapters for Phase 6 formal traceability and governed conflicts: the
``trace`` and ``conflict`` command groups.

The CONTRACT is exactly the parent CLI's: ``--json`` prints one object on
stdout, humans read stderr, no ANSI, the shared exit codes, bounded output,
no secrets, no automatic semantic-provider use (nothing in this module can
reach a provider — the whole trace/conflict plane is deterministic). Every
write takes explicit ``--actor`` and ``--note``; there is no flag anywhere
here that skips an eligibility or lifecycle rule.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Tuple

from .domain.errors import InvalidRequest, OperationTimeout


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _traceability():
    from .runtime import get_runtime
    return get_runtime().traceability


def _actor_note(args: argparse.Namespace) -> Tuple[str, str]:
    return str(getattr(args, "actor", "") or ""), \
        str(getattr(args, "note", "") or "")


def _source(verb: str) -> str:
    return f"cli:{verb}"


def _scope_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    scope: Dict[str, Any] = {}
    requirement = getattr(args, "requirement", None)
    if requirement:
        scope["requirement"] = str(requirement).strip()
    raw = getattr(args, "scope", None)
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError as exc:
            raise InvalidRequest(f"--scope is not valid JSON: {exc}")
        if not isinstance(extra, dict):
            raise InvalidRequest("--scope must be a JSON object")
        scope.update(extra)
    return scope


# ---------------------------------------------------------------------------
# trace policy
# ---------------------------------------------------------------------------
def cmd_policy_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().list_policies(args.workspace))

    def human(p: Dict[str, Any]) -> None:
        selected = (p.get("selected") or {}).get("policy_name") \
            or f"{p['default_policy']} (default)"
        print(f"active policy: {selected}")
        for policy in p["policies"]:
            mark = "ok " if policy["valid"] else "BAD"
            print(f"  [{mark}] {policy['name']:24} ({policy['source']})")
            for error in policy.get("errors", [])[:5]:
                print(f"        error: {error}")

    out.emit(payload, human)
    return 0, payload


def cmd_policy_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().get_policy(
        args.workspace, getattr(args, "policy", None)))

    def human(p: Dict[str, Any]) -> None:
        policy = p["policy"]
        print(f"{policy['name']} — {policy['title']}")
        print(f"checksum: {p['checksum']}")
        print(f"source: {p['source']}  active: {p['is_active']}")
        for stage in policy["stages"]:
            required = "required" if stage["required"] else "optional"
            print(f"  {stage['name']:14} [{required}] "
                  f"{', '.join(stage['entityTypes'])}")

    out.emit(payload, human)
    return 0, payload


def cmd_policy_validate(args, out) -> Tuple[int, Dict[str, Any]]:
    from pathlib import Path
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidRequest(f"cannot read policy file: {exc}")
    if args.file.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except Exception:
            raise InvalidRequest(
                "PyYAML is not installed — provide the policy as .json")
        try:
            document = yaml.safe_load(text)
        except Exception as exc:
            raise InvalidRequest(f"invalid YAML: {exc}")
    else:
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise InvalidRequest(f"invalid JSON: {exc}")
    payload = _ok(_traceability().validate_policy(args.workspace, document))

    def human(p: Dict[str, Any]) -> None:
        print("valid" if p["valid"] else "INVALID")
        for error in p.get("errors", []):
            print(f"  error: {error}")
        if p.get("checksum"):
            print(f"checksum: {p['checksum']}")

    out.emit(payload, human)
    return 0 if payload["valid"] else 1, payload


def cmd_policy_set(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().set_workspace_policy(
        args.workspace, policy_name=args.policy, actor=actor, note=note,
        source_command=_source("trace policy set")))

    def human(p: Dict[str, Any]) -> None:
        if p.get("unchanged"):
            print(f"unchanged: {p['policy_name']} was already active")
        else:
            print(f"active policy: {p['policy_name']}")
            print(f"staled {p['staled_paths']} paths, "
                  f"{p['staled_snapshots']} snapshots — run "
                  f"`openmind trace refresh` to rebuild")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# trace refresh
# ---------------------------------------------------------------------------
def cmd_refresh_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().plan_refresh(
        args.workspace, scope=_scope_from_args(args),
        force=bool(getattr(args, "force", False))))

    def human(p: Dict[str, Any]) -> None:
        print(f"mode: {p['mode']}  ({p['reason']})")
        if p.get("affected_roots") is not None:
            print(f"affected roots: {len(p['affected_roots'])}")

    out.emit(payload, human)
    return 0, payload


def cmd_refresh(args, out) -> Tuple[int, Dict[str, Any]]:
    wait = bool(getattr(args, "wait", False))
    timeout = float(getattr(args, "timeout", 600) or 600)
    if wait:
        result = _traceability().refresh(
            args.workspace, scope=_scope_from_args(args),
            force=bool(getattr(args, "force", False)), wait=True)
        payload = _ok(result)
    else:
        result = _traceability().refresh(
            args.workspace, scope=_scope_from_args(args),
            force=bool(getattr(args, "force", False)), wait=False)
        payload = _ok(result)
        if result.get("queued"):
            deadline = time.time() + timeout
            from . import db
            job_id = result["job"]["job_id"]
            while time.time() < deadline:
                job = db.get_job(job_id)
                if job and job["status"] in ("done", "failed"):
                    payload = _ok({"workspace_id": args.workspace,
                                   "job": job})
                    break
                time.sleep(0.5)
            else:
                raise OperationTimeout(
                    f"traceability refresh did not finish within "
                    f"{timeout:.0f}s (job {job_id} still running)")

    def human(p: Dict[str, Any]) -> None:
        if p.get("no_op"):
            print("no-op: knowledge revision, policy checksum and engine "
                  "version unchanged (use --force to rebuild)")
        elif p.get("run"):
            run = p["run"]
            print(f"run {run['id']}: {run['status']}")
            summary = run.get("summary") or {}
            if summary:
                print(f"  mode {summary.get('mode')}: "
                      f"{summary.get('roots_rebuilt', 0)} rebuilt, "
                      f"{summary.get('roots_reused', 0)} reused")
        elif p.get("job"):
            print(f"job {p['job']['job_id']}: {p['job']['status']}")

    out.emit(payload, human)
    return 0, payload


def cmd_runs(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().list_runs(
        args.workspace, status=getattr(args, "status", None),
        limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for run in p["runs"]:
            print(f"  {run['id']}  {run['status']:9} rev "
                  f"{run['knowledge_revision']:4}  {run['created_at']}")
        if not p["runs"]:
            print("no traceability runs")

    out.emit(payload, human)
    return 0, payload


def cmd_run_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"run": _traceability().get_run(args.workspace, args.run)})
    out.emit(payload, lambda p: print(json.dumps(p["run"], indent=2,
                                                 default=str)))
    return 0, payload


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------
def cmd_trace_requirement(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().trace_requirement(
        args.workspace, args.requirement,
        include_stale=bool(getattr(args, "include_stale", False)),
        max_paths=int(getattr(args, "max_paths", 10) or 10)))

    def human(p: Dict[str, Any]) -> None:
        root = p["root"]
        print(f"root: {root['canonical_key']} "
              f"[authority: {root['authority_status']}]")
        print(f"policy: {p['policy']['name']}  revision: "
              f"{p['knowledge_revision']}")
        for path in p["paths"]:
            print(f"  [{path['status']:9}] {path['path_kind']:32} "
                  f"completeness {path['completeness']:.2f} "
                  f"confidence {path['confidence']}")
        for gap in p.get("gaps", []):
            print(f"  gap: {gap['gap_type']} ({gap['severity']}) "
                  f"at {gap['stage']}")
        if p["truncated"]:
            print("  (truncated — limits reached)")

    out.emit(payload, human)
    return 0, payload


def cmd_trace_code(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().trace_code(
        args.workspace, args.entity,
        include_stale=bool(getattr(args, "include_stale", False))))

    def human(p: Dict[str, Any]) -> None:
        print(f"entity: {p['entity']['canonical_key']} "
              f"[{p['classification']}]")
        for requirement in p["requirements"]:
            print(f"  requirement: {requirement['canonical_key']}")
        for test in p["tests"]:
            print(f"  test: {test['canonical_key']}")
        if p["orphan"]:
            print("  orphan code (untraced — not invalid)")

    out.emit(payload, human)
    return 0, payload


def cmd_trace_test(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().trace_test(
        args.workspace, args.entity,
        include_stale=bool(getattr(args, "include_stale", False))))

    def human(p: Dict[str, Any]) -> None:
        print(f"entity: {p['entity']['canonical_key']}")
        for requirement in p["requirements"]:
            print(f"  verifies requirement: "
                  f"{requirement['canonical_key']}")
        for target in p["implementation_targets"]:
            print(f"  implementation: {target['canonical_key']}")
        if p["untraced"]:
            print("  untraced test (no requirement path)")

    out.emit(payload, human)
    return 0, payload


def cmd_trace_path(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"path": _traceability().get_trace_path(args.workspace,
                                                          args.trace)})

    def human(p: Dict[str, Any]) -> None:
        path = p["path"]
        print(f"{path['path_kind']} [{path['status']}] "
              f"completeness {path['completeness']:.2f}")
        for step in path["steps"]:
            print(f"  {step['ordinal']:2}. {step['stage']:14} "
                  f"{step['node_id']}  via {step['relation_type']} "
                  f"[{step['relation_state']}] "
                  f"evidence {step['evidence_status']}")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# coverage / gaps / orphans
# ---------------------------------------------------------------------------
def cmd_coverage(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().get_coverage(args.workspace))

    def human(p: Dict[str, Any]) -> None:
        if not p.get("snapshot"):
            print(p.get("reason", "no coverage snapshot"))
            return
        metrics = p["snapshot"]["metrics"]
        requirements = metrics["requirements"]
        print(f"status: {metrics['status']}  "
              f"(requirements: {requirements['total']})")
        for key in ("with_design", "with_interface", "with_implementation",
                    "with_tests", "with_test_results",
                    "with_current_evidence", "fully_traced",
                    "partially_traced", "untraced"):
            ratio = requirements[key]
            pct = (f"{ratio['percentage']:.1f}%"
                   if ratio["percentage"] is not None else "n/a")
            print(f"  {key:24} {ratio['count']:4}  {pct}")

    out.emit(payload, human)
    return 0, payload


def cmd_gaps(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().list_gaps(
        args.workspace, gap_type=getattr(args, "type", None),
        status=getattr(args, "status", None),
        root_entity_id=getattr(args, "root", None),
        severity=getattr(args, "severity", None),
        limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for gap in p["gaps"]:
            print(f"  {gap['id']}  [{gap['severity']:8}] "
                  f"{gap['gap_type']:28} {gap['status']:9} "
                  f"root {gap['root_entity_id']}")
        print(f"{p['count']} shown, {p['open_total']} open total")

    out.emit(payload, human)
    return 0, payload


def cmd_gap_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"gap": _traceability().get_gap(args.workspace,
                                                  args.gap)})
    out.emit(payload, lambda p: print(json.dumps(p["gap"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_gap_resolve(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().resolve_gap(
        args.workspace, args.gap, actor=actor, note=note,
        engine_exception=str(getattr(args, "engine_exception", "") or ""),
        source_command=_source("trace gap-resolve")))
    out.emit(payload, lambda p: print(
        f"gap {p['gap']['id']}: {p['gap']['status']}"))
    return 0, payload


def cmd_gap_accept(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().accept_gap(
        args.workspace, args.gap, actor=actor, note=note,
        expires_at=str(getattr(args, "expires", "") or ""),
        source_command=_source("trace gap-accept")))
    out.emit(payload, lambda p: print(
        f"gap {p['gap']['id']}: {p['gap']['status']}"))
    return 0, payload


def cmd_gap_dismiss(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().dismiss_gap(
        args.workspace, args.gap, actor=actor, note=note,
        source_command=_source("trace gap-dismiss")))
    out.emit(payload, lambda p: print(
        f"gap {p['gap']['id']}: {p['gap']['status']}"))
    return 0, payload


def cmd_gap_reopen(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().reopen_gap(
        args.workspace, args.gap, actor=actor, note=note,
        source_command=_source("trace gap-reopen")))
    out.emit(payload, lambda p: print(
        f"gap {p['gap']['id']}: {p['gap']['status']}"))
    return 0, payload


def _cmd_orphans(kind: str):
    def command(args, out) -> Tuple[int, Dict[str, Any]]:
        service = _traceability()
        method = {
            "requirements": service.find_orphan_requirements,
            "code": service.find_orphan_code,
            "tests": service.find_orphan_tests,
            "documents": service.find_orphan_documents,
        }[kind]
        payload = _ok(method(args.workspace, limit=args.limit))

        def human(p: Dict[str, Any]) -> None:
            for orphan in p["orphans"]:
                print(f"  {orphan['entity_id']}  "
                      f"{orphan.get('canonical_key', '')}  "
                      f"[{orphan['classification']}]")
            print(f"{p['count']} orphan {kind}")

        out.emit(payload, human)
        return 0, payload
    return command


# ---------------------------------------------------------------------------
# conflict commands
# ---------------------------------------------------------------------------
def cmd_conflict_scan_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().plan_conflict_scan(args.workspace))

    def human(p: Dict[str, Any]) -> None:
        for plan in p["detectors"]:
            print(f"  {plan['detector_name']:24} "
                  f"{plan['comparable_facts']:5} facts, "
                  f"{plan['comparison_groups']:4} groups")
            for omission in plan.get("omissions", [])[:3]:
                print(f"    omission: {omission}")

    out.emit(payload, human)
    return 0, payload


def cmd_conflict_scan(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, _ = _actor_note(args)
    wait = bool(getattr(args, "wait", False))
    result = _traceability().scan_conflicts(args.workspace, actor=actor,
                                            wait=wait)
    if not wait and result.get("queued"):
        timeout = float(getattr(args, "timeout", 600) or 600)
        deadline = time.time() + timeout
        from . import db
        job_id = result["job"]["job_id"]
        while time.time() < deadline:
            job = db.get_job(job_id)
            if job and job["status"] in ("done", "failed"):
                result = {"workspace_id": args.workspace, "job": job}
                break
            time.sleep(0.5)
        else:
            raise OperationTimeout(
                f"conflict scan did not finish within {timeout:.0f}s")
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        if p.get("job"):
            print(f"job {p['job']['job_id']}: {p['job']['status']}")
            return
        print(f"scan: {p['status']}")
        print(f"  created {len(p.get('created', []))}, superseded "
              f"{len(p.get('superseded', []))}, unchanged "
              f"{len(p.get('observed_unchanged', []))}, suppressed "
              f"{len(p.get('suppressed', []))}")
        for error in p.get("detector_errors", []):
            print(f"  detector error: {error['detector']}: "
                  f"{error['error']}")

    out.emit(payload, human)
    return 0, payload


def cmd_conflict_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_traceability().list_conflicts(
        args.workspace, status=getattr(args, "status", None),
        category=getattr(args, "category", None),
        origin=getattr(args, "origin", None),
        severity=getattr(args, "severity", None),
        limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for conflict in p["conflicts"]:
            print(f"  {conflict['id']}  [{conflict['severity']:8}] "
                  f"{conflict['category']:22} {conflict['status']:13} "
                  f"{conflict['title'][:50]}")
        print(f"{p['count']} shown, {p['open_total']} open total")

    out.emit(payload, human)
    return 0, payload


def cmd_conflict_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"conflict": _traceability().get_conflict(
        args.workspace, args.conflict)})
    out.emit(payload, lambda p: print(json.dumps(p["conflict"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_conflict_promotion_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"plan": _traceability().plan_conflict_promotion(
        args.workspace, args.candidate)})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"expected action: {plan['expected_action']}")
        for reason in plan.get("blocking_reasons", []):
            print(f"  blocked: {reason}")

    out.emit(payload, human)
    return 0, payload


def cmd_conflict_promote(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_traceability().promote_conflict_candidate(
        args.workspace, args.candidate, actor=actor, note=note,
        source_command=_source("conflict promote")))

    def human(p: Dict[str, Any]) -> None:
        print(f"promotion: {p['status']}")
        if p.get("conflict"):
            print(f"  conflict {p['conflict']['id']} "
                  f"[{p['conflict']['status']}]")

    out.emit(payload, human)
    return 0, payload


def _cmd_conflict_governance(verb: str):
    def command(args, out) -> Tuple[int, Dict[str, Any]]:
        actor, note = _actor_note(args)
        service = _traceability()
        kwargs: Dict[str, Any] = {"actor": actor, "note": note,
                                  "source_command":
                                      _source(f"conflict {verb}")}
        if verb == "accept-risk":
            kwargs["expires_at"] = str(getattr(args, "expires", "") or "")
            kwargs["follow_up"] = str(getattr(args, "follow_up", "") or "")
            result = service.accept_conflict_risk(args.workspace,
                                                  args.conflict, **kwargs)
        elif verb == "resolve":
            refs: List[Dict[str, Any]] = []
            for evidence_id in getattr(args, "evidence", None) or []:
                refs.append({"evidence_id": str(evidence_id).strip()})
            kwargs["resolution_type"] = str(
                getattr(args, "resolution", "") or "")
            kwargs["evidence"] = refs
            result = service.resolve_conflict(args.workspace,
                                              args.conflict, **kwargs)
        elif verb == "review":
            result = service.start_conflict_review(args.workspace,
                                                   args.conflict, **kwargs)
        elif verb == "dismiss":
            result = service.dismiss_conflict(args.workspace,
                                              args.conflict, **kwargs)
        else:
            result = service.reopen_conflict(args.workspace,
                                             args.conflict, **kwargs)
        payload = _ok(result)
        out.emit(payload, lambda p: print(
            f"conflict {p['conflict']['id']}: {p['conflict']['status']} "
            f"(revision {p['knowledge_revision']})"))
        return 0, payload
    return command


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def _ws(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True, help="workspace id")


def _actor_note_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True,
                        help="who is making this governance decision "
                             "(recorded verbatim, never inferred)")
    parser.add_argument("--note", required=True,
                        help="why (bounded; recorded on the decision)")


def _paging(parser: argparse.ArgumentParser, limit: int = 100) -> None:
    parser.add_argument("--limit", type=int, default=limit, metavar="N")
    parser.add_argument("--offset", type=int, default=0, metavar="N")


def register(sub: argparse._SubParsersAction,
             common: argparse.ArgumentParser) -> None:
    """Attach the Phase 6 ``trace`` and ``conflict`` command groups."""
    # -- trace ---------------------------------------------------------------
    trace = sub.add_parser("trace", parents=[common],
                           help="formal, policy-driven requirement "
                                "traceability (not generic graph paths)")
    trace_sub = trace.add_subparsers(dest="trace_command",
                                     metavar="<subcommand>")

    policy = trace_sub.add_parser("policy", parents=[common],
                                  help="traceability policies")
    policy_sub = policy.add_subparsers(dest="policy_command",
                                       metavar="<subcommand>")
    p_list = policy_sub.add_parser("list", parents=[common],
                                   help="built-in + organization policies "
                                        "(invalid ones listed with errors)")
    _ws(p_list)
    p_list.set_defaults(func=cmd_policy_list)
    p_show = policy_sub.add_parser("show", parents=[common],
                                   help="one policy (default: the active "
                                        "one)")
    _ws(p_show)
    p_show.add_argument("--policy", help="policy name (default: active)")
    p_show.set_defaults(func=cmd_policy_show)
    p_validate = policy_sub.add_parser("validate", parents=[common],
                                       help="validate a policy file "
                                            "(never selects it)")
    _ws(p_validate)
    p_validate.add_argument("--file", required=True,
                            help="policy .json/.yaml file")
    p_validate.set_defaults(func=cmd_policy_validate)
    p_set = policy_sub.add_parser("set", parents=[common],
                                  help="select the workspace's active "
                                       "policy (invalidates snapshots; "
                                       "refresh required)")
    _ws(p_set)
    p_set.add_argument("--policy", required=True)
    _actor_note_flags(p_set)
    p_set.set_defaults(func=cmd_policy_set)
    policy.set_defaults(func=None, _parser=policy)

    t_refresh_plan = trace_sub.add_parser(
        "refresh-plan", parents=[common],
        help="dry-run: no-op / incremental / full and the affected roots")
    _ws(t_refresh_plan)
    t_refresh_plan.add_argument("--requirement",
                                help="limit to one requirement root")
    t_refresh_plan.add_argument("--scope", help="scope JSON object")
    t_refresh_plan.add_argument("--force", action="store_true")
    t_refresh_plan.set_defaults(func=cmd_refresh_plan)

    t_refresh = trace_sub.add_parser(
        "refresh", parents=[common],
        help="rebuild trace paths, gaps and the coverage snapshot "
             "(deterministic, provider-free; unchanged graph = no-op)")
    _ws(t_refresh)
    t_refresh.add_argument("--requirement",
                           help="limit to one requirement root")
    t_refresh.add_argument("--scope", help="scope JSON object")
    t_refresh.add_argument("--force", action="store_true")
    t_refresh.add_argument("--wait", action="store_true",
                           help="run synchronously in this process")
    t_refresh.add_argument("--timeout", type=float, default=600)
    t_refresh.set_defaults(func=cmd_refresh)

    t_runs = trace_sub.add_parser("runs", parents=[common],
                                  help="list traceability runs")
    _ws(t_runs)
    t_runs.add_argument("--status")
    _paging(t_runs, limit=50)
    t_runs.set_defaults(func=cmd_runs)
    t_run = trace_sub.add_parser("run", parents=[common],
                                 help="one traceability run")
    _ws(t_run)
    t_run.add_argument("--run", required=True, help="run id (trun_...)")
    t_run.set_defaults(func=cmd_run_show)

    t_requirement = trace_sub.add_parser(
        "requirement", parents=[common],
        help="formal requirement trace under the active policy")
    _ws(t_requirement)
    t_requirement.add_argument("--requirement", required=True,
                               help="requirement entity id (ent_...)")
    t_requirement.add_argument("--include-stale", dest="include_stale",
                               action="store_true")
    t_requirement.add_argument("--max-paths", dest="max_paths", type=int,
                               default=10)
    t_requirement.set_defaults(func=cmd_trace_requirement)

    t_code = trace_sub.add_parser("code", parents=[common],
                                  help="reverse code trace")
    _ws(t_code)
    t_code.add_argument("--entity", required=True)
    t_code.add_argument("--include-stale", dest="include_stale",
                        action="store_true")
    t_code.set_defaults(func=cmd_trace_code)

    t_test = trace_sub.add_parser("test", parents=[common],
                                  help="reverse test trace")
    _ws(t_test)
    t_test.add_argument("--entity", required=True)
    t_test.add_argument("--include-stale", dest="include_stale",
                        action="store_true")
    t_test.set_defaults(func=cmd_trace_test)

    t_path = trace_sub.add_parser("path", parents=[common],
                                  help="one persisted trace path")
    _ws(t_path)
    t_path.add_argument("--trace", required=True,
                        help="trace path id (tr_...)")
    t_path.set_defaults(func=cmd_trace_path)

    t_coverage = trace_sub.add_parser("coverage", parents=[common],
                                      help="latest coverage snapshot")
    _ws(t_coverage)
    t_coverage.set_defaults(func=cmd_coverage)

    t_gaps = trace_sub.add_parser("gaps", parents=[common],
                                  help="traceability gaps (bounded)")
    _ws(t_gaps)
    t_gaps.add_argument("--type", help="gap type filter")
    t_gaps.add_argument("--status", help="open/resolved/accepted/"
                                         "dismissed/stale")
    t_gaps.add_argument("--root", help="root entity id filter")
    t_gaps.add_argument("--severity")
    _paging(t_gaps)
    t_gaps.set_defaults(func=cmd_gaps)

    t_gap_show = trace_sub.add_parser("gap-show", parents=[common],
                                      help="one gap")
    _ws(t_gap_show)
    t_gap_show.add_argument("--gap", required=True, help="gap id (tg_...)")
    t_gap_show.set_defaults(func=cmd_gap_show)

    t_gap_resolve = trace_sub.add_parser(
        "gap-resolve", parents=[common],
        help="explicit resolution (refused while still detected unless "
             "--engine-exception)")
    _ws(t_gap_resolve)
    t_gap_resolve.add_argument("--gap", required=True)
    t_gap_resolve.add_argument("--engine-exception",
                               dest="engine_exception",
                               help="documented engine-exception reason")
    _actor_note_flags(t_gap_resolve)
    t_gap_resolve.set_defaults(func=cmd_gap_resolve)

    t_gap_accept = trace_sub.add_parser(
        "gap-accept", parents=[common],
        help="accept an intentional gap (optional expiry)")
    _ws(t_gap_accept)
    t_gap_accept.add_argument("--gap", required=True)
    t_gap_accept.add_argument("--expires",
                              help="ISO timestamp; expired acceptance "
                                   "reopens")
    _actor_note_flags(t_gap_accept)
    t_gap_accept.set_defaults(func=cmd_gap_accept)

    t_gap_dismiss = trace_sub.add_parser(
        "gap-dismiss", parents=[common],
        help="dismiss a false positive (suppression fingerprint stored)")
    _ws(t_gap_dismiss)
    t_gap_dismiss.add_argument("--gap", required=True)
    _actor_note_flags(t_gap_dismiss)
    t_gap_dismiss.set_defaults(func=cmd_gap_dismiss)

    t_gap_reopen = trace_sub.add_parser("gap-reopen", parents=[common],
                                        help="reopen a gap")
    _ws(t_gap_reopen)
    t_gap_reopen.add_argument("--gap", required=True)
    _actor_note_flags(t_gap_reopen)
    t_gap_reopen.set_defaults(func=cmd_gap_reopen)

    for kind in ("requirements", "code", "tests", "documents"):
        orphan_parser = trace_sub.add_parser(
            f"orphans-{kind}", parents=[common],
            help=f"orphan {kind} (untraced, never 'invalid')")
        _ws(orphan_parser)
        orphan_parser.add_argument("--limit", type=int, default=500,
                                   metavar="N")
        orphan_parser.set_defaults(func=_cmd_orphans(kind))
    trace.set_defaults(func=None, _parser=trace)

    # -- conflict ------------------------------------------------------------
    conflict = sub.add_parser("conflict", parents=[common],
                              help="governed engineering conflicts "
                                   "(deterministic detection + explicit "
                                   "promotion)")
    conflict_sub = conflict.add_subparsers(dest="conflict_command",
                                           metavar="<subcommand>")

    c_scan_plan = conflict_sub.add_parser(
        "scan-plan", parents=[common],
        help="dry-run: what each deterministic detector would examine")
    _ws(c_scan_plan)
    c_scan_plan.set_defaults(func=cmd_conflict_scan_plan)

    c_scan = conflict_sub.add_parser(
        "scan", parents=[common],
        help="deterministic conflict scan (no provider call; identical "
             "re-detection creates nothing)")
    _ws(c_scan)
    c_scan.add_argument("--actor", default="",
                        help="recorded on scan-created governance rows")
    c_scan.add_argument("--note", default="", help="unused; accepted for "
                                                   "symmetry")
    c_scan.add_argument("--wait", action="store_true",
                        help="run synchronously in this process")
    c_scan.add_argument("--timeout", type=float, default=600)
    c_scan.set_defaults(func=cmd_conflict_scan)

    c_list = conflict_sub.add_parser("list", parents=[common],
                                     help="list conflicts (bounded)")
    _ws(c_list)
    c_list.add_argument("--status")
    c_list.add_argument("--category")
    c_list.add_argument("--origin")
    c_list.add_argument("--severity")
    _paging(c_list)
    c_list.set_defaults(func=cmd_conflict_list)

    c_show = conflict_sub.add_parser("show", parents=[common],
                                     help="one conflict with objects, "
                                          "evidence and decisions")
    _ws(c_show)
    c_show.add_argument("--conflict", required=True,
                        help="conflict id (ecf_...)")
    c_show.set_defaults(func=cmd_conflict_show)

    c_promotion_plan = conflict_sub.add_parser(
        "promotion-plan", parents=[common],
        help="conflict-candidate promotion dry-run (no write)")
    _ws(c_promotion_plan)
    c_promotion_plan.add_argument("--candidate", required=True,
                                  help="conflict candidate id (sx_...)")
    c_promotion_plan.set_defaults(func=cmd_conflict_promotion_plan)

    c_promote = conflict_sub.add_parser(
        "promote", parents=[common],
        help="promote one confirmed, active, verified conflict candidate")
    _ws(c_promote)
    c_promote.add_argument("--candidate", required=True)
    _actor_note_flags(c_promote)
    c_promote.set_defaults(func=cmd_conflict_promote)

    c_review = conflict_sub.add_parser("review", parents=[common],
                                       help="open -> under-review")
    _ws(c_review)
    c_review.add_argument("--conflict", required=True)
    _actor_note_flags(c_review)
    c_review.set_defaults(func=_cmd_conflict_governance("review"))

    c_accept = conflict_sub.add_parser(
        "accept-risk", parents=[common],
        help="accept the risk (optional expiry; expired risks reopen)")
    _ws(c_accept)
    c_accept.add_argument("--conflict", required=True)
    c_accept.add_argument("--expires", help="ISO timestamp")
    c_accept.add_argument("--follow-up", dest="follow_up",
                          help="follow-up reference")
    _actor_note_flags(c_accept)
    c_accept.set_defaults(func=_cmd_conflict_governance("accept-risk"))

    c_resolve = conflict_sub.add_parser(
        "resolve", parents=[common],
        help="resolve (requires resolution type + evidence; never "
             "rewrites claims)")
    _ws(c_resolve)
    c_resolve.add_argument("--conflict", required=True)
    c_resolve.add_argument("--resolution", required=True,
                           help="left-correct / right-correct / "
                                "both-updated / superseded / "
                                "false-positive / other")
    c_resolve.add_argument("--evidence", action="append", metavar="EVID",
                           help="supporting evidence id (repeatable)")
    _actor_note_flags(c_resolve)
    c_resolve.set_defaults(func=_cmd_conflict_governance("resolve"))

    c_dismiss = conflict_sub.add_parser(
        "dismiss", parents=[common],
        help="dismiss a false positive (suppression fingerprint stored)")
    _ws(c_dismiss)
    c_dismiss.add_argument("--conflict", required=True)
    _actor_note_flags(c_dismiss)
    c_dismiss.set_defaults(func=_cmd_conflict_governance("dismiss"))

    c_reopen = conflict_sub.add_parser("reopen", parents=[common],
                                       help="reopen a conflict")
    _ws(c_reopen)
    c_reopen.add_argument("--conflict", required=True)
    _actor_note_flags(c_reopen)
    c_reopen.set_defaults(func=_cmd_conflict_governance("reopen"))
    conflict.set_defaults(func=None, _parser=conflict)


__all__ = ["register"]
