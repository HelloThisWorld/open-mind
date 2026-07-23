"""CLI adapters for Phase 7 Git change intelligence and overlays: the ``git``,
``overlay``, ``pr`` and ``impact`` command groups.

Same CONTRACT as the parent CLI: ``--json`` prints one object on stdout, humans
read stderr, no ANSI, shared exit codes, bounded output, no secrets, no Git
mutations, no provider calls. Every operation here is a Git READER or an overlay
read/lifecycle op — nothing mutates Git or the canonical Base Workspace.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Tuple


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _git():
    from .runtime import get_runtime
    return get_runtime().git


def _overlays():
    from .runtime import get_runtime
    return get_runtime().overlays


def _ws(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True, help="workspace id")


def _repo_specs(args) -> List[Dict[str, str]]:
    """Parse repeated --repo-range "git:key=base..head" into repo specs, or a
    single --repository/--base/--head triple."""
    specs: List[Dict[str, str]] = []
    for rr in getattr(args, "repo_range", None) or []:
        if "=" not in rr:
            from .domain.errors import InvalidRequest
            raise InvalidRequest(f"--repo-range must be 'git:key=base..head': {rr!r}")
        key, rng = rr.split("=", 1)
        if ".." not in rng:
            from .domain.errors import InvalidRequest
            raise InvalidRequest(f"--repo-range range must be 'base..head': {rng!r}")
        base, head = rng.split("..", 1)
        specs.append({"repository": key.strip(), "base": base.strip(),
                      "head": head.strip(), "target_branch": base.strip()})
    if getattr(args, "repository", None):
        specs.append({"repository": args.repository,
                      "base": getattr(args, "base", "") or "",
                      "head": getattr(args, "head", "") or "",
                      "target_branch": getattr(args, "base", "") or ""})
    return specs


# ---------------------------------------------------------------------------
# git repositories / status
# ---------------------------------------------------------------------------
def cmd_git_repositories(args, out) -> Tuple[int, Dict[str, Any]]:
    svc = _git()
    if getattr(args, "discover", False):
        payload = _ok(svc.discover_repositories(args.workspace))
    else:
        payload = _ok(svc.list_repositories(args.workspace))

    def human(p):
        print(f"{p['count']} repository(ies):")
        for r in p["repositories"]:
            print(f"  {r['repositoryKey']:24} root={r['relativeRoot']} "
                  f"fmt={r['objectFormat']} branch={r['defaultBranch']}")
    out.emit(payload, human)
    return 0, payload


def cmd_git_repository(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().get_repository(args.workspace, args.repository))
    out.emit(payload, lambda p: print(json.dumps(p["repository"], indent=2)))
    return 0, payload


def cmd_git_status(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().get_repository_status(args.workspace, args.repository))

    def human(p):
        s = p["status"]
        print(f"{p['repository']['repositoryKey']}: head={s['headCommit'][:12]} "
              f"branch={s['branch']} clean={s['clean']} "
              f"detached={s['detachedHead']} shallow={s['shallow']}")
    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# git baseline
# ---------------------------------------------------------------------------
def cmd_baseline_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().plan_baseline(args.workspace,
                                       getattr(args, "repository", None)))

    def human(p):
        print(f"canCapture: {p['canCapture']}")
        for plan in p["plans"]:
            print(f"  {plan['repository']}: coherent={plan['coherent']}")
            for reason in plan["reasons"]:
                print(f"      blocked: {reason}")
    out.emit(payload, human)
    return 0, payload


def cmd_baseline_capture(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().capture_baseline(
        args.workspace, getattr(args, "repository", None),
        actor=getattr(args, "actor", "") or ""))

    def human(p):
        print(f"captured: {len(p['captured'])}  blocked: {len(p['blocked'])}")
        for c in p["captured"]:
            b = c["baseline"]
            print(f"  {c['repository']}: commit={b['commit_sha'][:12]} "
                  f"kr={b['knowledge_revision']} "
                  f"{'(idempotent)' if c.get('idempotent') else ''}")
        for b in p["blocked"]:
            print(f"  BLOCKED {b['repository']}: {', '.join(b['reasons'])}")
    out.emit(payload, human)
    return 0, payload


def cmd_baseline_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().get_baseline(args.workspace, args.baseline))
    out.emit(payload, lambda p: print(json.dumps(p["baseline"], indent=2)))
    return 0, payload


def cmd_baseline_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_git().list_baselines(args.workspace,
                                        getattr(args, "repository", None)))

    def human(p):
        print(f"{p['count']} baseline(s):")
        for b in p["baselines"]:
            print(f"  {b['id']} commit={b['commit_sha'][:12]} "
                  f"kr={b['knowledge_revision']} at {b['created_at']}")
    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# overlay
# ---------------------------------------------------------------------------
def cmd_overlay_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().plan_overlay(
        args.workspace, kind=args.kind, repositories=_repo_specs(args)))

    def human(p):
        print(f"kind={p['kind']} valid={p['valid']}")
        for r in p["repositories"]:
            print(f"  {r['repository']}: base={r['baseCommit'][:12]} "
                  f"head={r['headCommit'][:12]} mergeBase={r['mergeBase'][:12]}")
        for e in p["errors"]:
            print(f"  ERROR {e['repository']}: {e['error']}")
    out.emit(payload, human)
    return 0, payload


def cmd_overlay_create(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().create_overlay(
        args.workspace, kind=args.kind, repositories=_repo_specs(args),
        name=getattr(args, "name", "") or "",
        allow_partial_repositories=getattr(args, "allow_partial_repositories",
                                           False)))
    _emit_overlay(payload, out)
    return 0, payload


def cmd_overlay_refresh(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().refresh_overlay(args.workspace, args.overlay))
    _emit_overlay(payload, out)
    return 0, payload


def cmd_overlay_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().list_overlays(args.workspace,
                                            state=getattr(args, "state", None)))

    def human(p):
        print(f"{p['count']} overlay(s):")
        for o in p["overlays"]:
            print(f"  {o['id']} {o['kind']:12} state={o['state']:8} "
                  f"rev={o['overlayRevision']} risk="
                  f"{o.get('summary', {}).get('overallRisk', '?')}")
    out.emit(payload, human)
    return 0, payload


def cmd_overlay_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().get_overlay(args.workspace, args.overlay))
    out.emit(payload, lambda p: print(json.dumps(p["overlay"], indent=2)))
    return 0, payload


def cmd_overlay_files(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().list_overlay_files(
        args.workspace, args.overlay,
        change_type=getattr(args, "change_type", None)))

    def human(p):
        print(f"{p['count']} file(s):")
        for f in p["files"]:
            print(f"  {f['changeType']:11} {f['newPath'] or f['oldPath']}")
    out.emit(payload, human)
    return 0, payload


def cmd_overlay_file(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().get_overlay_file(args.workspace, args.overlay,
                                               args.file))
    out.emit(payload, lambda p: print(json.dumps(p, indent=2)))
    return 0, payload


def cmd_overlay_search(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().search_overlay(args.workspace, args.overlay,
                                             args.query,
                                             limit=getattr(args, "limit", 10)))
    out.emit(payload, lambda p: print(json.dumps(p, indent=2)))
    return 0, payload


def cmd_overlay_evidence(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().get_overlay_evidence(args.workspace, args.overlay,
                                                   args.evidence))
    out.emit(payload, lambda p: print(json.dumps(p["evidence"], indent=2)))
    return 0, payload


def cmd_overlay_impact(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().get_impact_report(args.workspace, args.overlay))

    def human(p):
        r = p["report"]
        print(f"report schema {r['schemaVersion']} hash {p['reportHash'][:16]}")
        print(f"  files: {r['fileSummary']['total']}  risk: "
              f"{r['riskSummary']['overallRisk']}")
        print(f"  impacted requirements: {len(r['impactedRequirements'])}  "
              f"conflict impacts: {len(r['conflictImpact'])}")
    out.emit(payload, human)
    return 0, payload


def _list_cmd(method_name: str, key: str):
    def cmd(args, out) -> Tuple[int, Dict[str, Any]]:
        payload = _ok(getattr(_overlays(), method_name)(args.workspace,
                                                        args.overlay))
        out.emit(payload, lambda p: print(json.dumps(p.get(key, p), indent=2)))
        return 0, payload
    return cmd


cmd_overlay_requirements = _list_cmd("list_impacted_requirements", "requirements")
cmd_overlay_tests = _list_cmd("list_impacted_tests", "tests")
cmd_overlay_traces = _list_cmd("list_trace_impacts", "traceImpacts")
cmd_overlay_gaps = _list_cmd("list_gap_impacts", "gapImpact")
cmd_overlay_conflicts = _list_cmd("list_conflict_impacts", "conflictImpacts")


def cmd_overlay_close(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().close_overlay(args.workspace, args.overlay))
    _emit_overlay(payload, out)
    return 0, payload


def cmd_overlay_abandon(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().abandon_overlay(args.workspace, args.overlay))
    _emit_overlay(payload, out)
    return 0, payload


def cmd_overlay_delete(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().delete_overlay(args.workspace, args.overlay))
    out.emit(payload, lambda p: print(f"deleted: {p['deleted']}"))
    return 0, payload


def cmd_overlay_reconcile(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().reconcile_overlay(
        args.workspace, args.overlay, actor=getattr(args, "actor", "") or "",
        note=getattr(args, "note", "") or ""))

    def human(p):
        print(f"reconciled: {p['reconciled']}")
        if not p["reconciled"]:
            print(f"  reason: {p.get('reason', '')}")
        else:
            print(f"  state: {p['state']}  mergedKnowledgeRevision: "
                  f"{p.get('mergedKnowledgeRevision')}")
    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# pr convenience
# ---------------------------------------------------------------------------
def _pr_specs(args) -> List[Dict[str, str]]:
    return [{"repository": args.repository, "base": args.base,
             "head": args.head, "target_branch": args.base}]


def _pr_options(args) -> Dict[str, Any]:
    opts: Dict[str, Any] = {}
    for k in ("pr_number", "title", "url", "author"):
        v = getattr(args, k, None)
        if v:
            opts[k] = v
    return {"pr": opts} if opts else {}


def cmd_pr_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().plan_overlay(args.workspace, kind="pr",
                                           repositories=_pr_specs(args)))
    out.emit(payload, lambda p: print(json.dumps(p, indent=2)))
    return 0, payload


def cmd_pr_analyze(args, out) -> Tuple[int, Dict[str, Any]]:
    name = getattr(args, "title", None) or (
        f"PR {args.pr_number}" if getattr(args, "pr_number", None) else "pr")
    payload = _ok(_overlays().create_overlay(
        args.workspace, kind="pr", repositories=_pr_specs(args), name=name,
        options=_pr_options(args)))
    _emit_overlay(payload, out)
    return 0, payload


def cmd_pr_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().get_overlay(args.workspace, args.overlay))
    out.emit(payload, lambda p: print(json.dumps(p["overlay"], indent=2)))
    return 0, payload


# ---------------------------------------------------------------------------
# impact packet
# ---------------------------------------------------------------------------
def cmd_impact_export(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_overlays().export_impact_packet(args.workspace, args.overlay,
                                                   args.output))

    def human(p):
        print(f"exported to {p['output']} (partial={p['partial']})")
        print(f"  files hashed: {len(p['manifest']['fileHashes'])}")
    out.emit(payload, human)
    return 0, payload


def cmd_impact_verify(args, out) -> Tuple[int, Dict[str, Any]]:
    from .impact_verify import verify_packet
    ok, summary = verify_packet(args.path)
    payload = {"ok": ok, **summary}

    def human(p):
        print(f"packet ok: {p['ok']}")
        for e in p.get("errors", []):
            print(f"  error: {e}")
    out.emit(payload, human)
    return (0 if ok else 1), payload


def _emit_overlay(payload, out) -> None:
    def human(p):
        o = p.get("overlay", {})
        print(f"overlay {o.get('id')} kind={o.get('kind')} "
              f"state={o.get('state')} rev={o.get('overlayRevision')}")
        if "refreshed" in p:
            print(f"  refreshed={p['refreshed']} "
                  f"reason={p.get('reason', '')}")
        if p.get("summary"):
            s = p["summary"]
            print(f"  files={s.get('files')} segments={s.get('segments')} "
                  f"risk={s.get('overallRisk')}")
    out.emit(payload, human)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def register(sub: argparse._SubParsersAction,
             common: argparse.ArgumentParser) -> None:
    """Attach the Phase 7 ``git``, ``overlay``, ``pr`` and ``impact`` groups."""
    # -- git ----------------------------------------------------------------
    git = sub.add_parser("git", parents=[common],
                         help="local Git repositories and canonical baselines "
                              "(read-only; never mutates Git)")
    git_sub = git.add_subparsers(dest="git_command", metavar="<subcommand>")

    g_repos = git_sub.add_parser("repositories", parents=[common],
                                 help="list (or --discover) git repositories")
    _ws(g_repos)
    g_repos.add_argument("--discover", action="store_true",
                         help="discover repositories under registered paths")
    g_repos.set_defaults(func=cmd_git_repositories)

    g_repo = git_sub.add_parser("repository", parents=[common],
                                help="show one repository")
    _ws(g_repo)
    g_repo.add_argument("--repository", required=True, help="repository key/id")
    g_repo.set_defaults(func=cmd_git_repository)

    g_status = git_sub.add_parser("status", parents=[common],
                                  help="repository HEAD/branch/clean status")
    _ws(g_status)
    g_status.add_argument("--repository", required=True)
    g_status.set_defaults(func=cmd_git_status)

    g_baseline = git_sub.add_parser("baseline", parents=[common],
                                    help="canonical git baselines")
    baseline_sub = g_baseline.add_subparsers(dest="baseline_command",
                                             metavar="<subcommand>")
    b_plan = baseline_sub.add_parser("plan", parents=[common],
                                     help="dry-run baseline coherence check")
    _ws(b_plan)
    b_plan.add_argument("--repository", help="limit to one repository")
    b_plan.set_defaults(func=cmd_baseline_plan)
    b_cap = baseline_sub.add_parser("capture", parents=[common],
                                    help="capture a coherent baseline")
    _ws(b_cap)
    b_cap.add_argument("--repository", help="limit to one repository")
    b_cap.add_argument("--actor", default="", help="who captured it")
    b_cap.set_defaults(func=cmd_baseline_capture)
    b_show = baseline_sub.add_parser("show", parents=[common],
                                     help="show one baseline")
    _ws(b_show)
    b_show.add_argument("--baseline", required=True)
    b_show.set_defaults(func=cmd_baseline_show)
    b_list = baseline_sub.add_parser("list", parents=[common],
                                     help="list baselines")
    _ws(b_list)
    b_list.add_argument("--repository", help="limit to one repository")
    b_list.set_defaults(func=cmd_baseline_list)
    g_baseline.set_defaults(func=None, _parser=g_baseline)
    git.set_defaults(func=None, _parser=git)

    # -- overlay ------------------------------------------------------------
    overlay = sub.add_parser("overlay", parents=[common],
                            help="isolated Git overlays (branch/PR/commit-range/"
                                 "working-tree/change-set); provisional, "
                                 "read-only, never touches canonical data")
    overlay_sub = overlay.add_subparsers(dest="overlay_command",
                                        metavar="<subcommand>")

    def _repo_flags(p, with_range=True):
        p.add_argument("--repository", help="repository key (single-repo form)")
        p.add_argument("--base", help="base ref")
        p.add_argument("--head", help="head ref")
        if with_range:
            p.add_argument("--repo-range", action="append", dest="repo_range",
                           help="git:key=base..head (repeatable, change-set)")

    o_plan = overlay_sub.add_parser("plan", parents=[common],
                                    help="validate an overlay without creating it")
    _ws(o_plan)
    o_plan.add_argument("--kind", required=True,
                        choices=["commit-range", "branch", "pr",
                                 "working-tree", "change-set"])
    _repo_flags(o_plan)
    o_plan.set_defaults(func=cmd_overlay_plan)

    o_create = overlay_sub.add_parser("create", parents=[common],
                                      help="build an overlay + impact report")
    _ws(o_create)
    o_create.add_argument("--kind", required=True,
                          choices=["commit-range", "branch", "pr",
                                   "working-tree", "change-set"])
    _repo_flags(o_create)
    o_create.add_argument("--name", default="")
    o_create.add_argument("--wait", action="store_true",
                          help="accepted for symmetry; the build is synchronous")
    o_create.add_argument("--allow-partial-repositories", action="store_true",
                          dest="allow_partial_repositories")
    o_create.set_defaults(func=cmd_overlay_create)

    for name, fn, extra in [
        ("refresh", cmd_overlay_refresh, None),
        ("show", cmd_overlay_show, None),
        ("impact", cmd_overlay_impact, None),
        ("requirements", cmd_overlay_requirements, None),
        ("tests", cmd_overlay_tests, None),
        ("traces", cmd_overlay_traces, None),
        ("gaps", cmd_overlay_gaps, None),
        ("conflicts", cmd_overlay_conflicts, None),
        ("close", cmd_overlay_close, None),
        ("abandon", cmd_overlay_abandon, None),
        ("delete", cmd_overlay_delete, None),
    ]:
        p = overlay_sub.add_parser(name, parents=[common])
        _ws(p)
        p.add_argument("--overlay", required=True)
        p.set_defaults(func=fn)

    o_files = overlay_sub.add_parser("files", parents=[common],
                                     help="list changed files")
    _ws(o_files)
    o_files.add_argument("--overlay", required=True)
    o_files.add_argument("--change-type", dest="change_type")
    o_files.set_defaults(func=cmd_overlay_files)

    o_file = overlay_sub.add_parser("file", parents=[common],
                                    help="show one changed file + its segments")
    _ws(o_file)
    o_file.add_argument("--overlay", required=True)
    o_file.add_argument("--file", required=True)
    o_file.set_defaults(func=cmd_overlay_file)

    o_search = overlay_sub.add_parser("search", parents=[common],
                                      help="composed overlay search")
    _ws(o_search)
    o_search.add_argument("--overlay", required=True)
    o_search.add_argument("--query", required=True)
    o_search.add_argument("--limit", type=int, default=10)
    o_search.set_defaults(func=cmd_overlay_search)

    o_ev = overlay_sub.add_parser("evidence", parents=[common],
                                  help="show one overlay evidence citation")
    _ws(o_ev)
    o_ev.add_argument("--overlay", required=True)
    o_ev.add_argument("--evidence", required=True)
    o_ev.set_defaults(func=cmd_overlay_evidence)

    o_recon = overlay_sub.add_parser("reconcile", parents=[common],
                                     help="explicit post-merge reconciliation "
                                          "(never runs a git merge)")
    _ws(o_recon)
    o_recon.add_argument("--overlay", required=True)
    o_recon.add_argument("--actor", default="")
    o_recon.add_argument("--note", default="")
    o_recon.add_argument("--wait", action="store_true")
    o_recon.set_defaults(func=cmd_overlay_reconcile)

    o_del_list = overlay_sub.add_parser("list", parents=[common],
                                        help="list overlays")
    _ws(o_del_list)
    o_del_list.add_argument("--state")
    o_del_list.set_defaults(func=cmd_overlay_list)
    overlay.set_defaults(func=None, _parser=overlay)

    # -- pr -----------------------------------------------------------------
    pr = sub.add_parser("pr", parents=[common],
                       help="local PR overlays using locally available refs "
                            "(no GitHub fetch, no comments)")
    pr_sub = pr.add_subparsers(dest="pr_command", metavar="<subcommand>")

    def _pr_flags(p):
        _ws(p)
        p.add_argument("--repository", required=True)
        p.add_argument("--base", required=True)
        p.add_argument("--head", required=True)
        p.add_argument("--pr-number", dest="pr_number")
        p.add_argument("--title")
        p.add_argument("--url")
        p.add_argument("--author")

    pr_plan = pr_sub.add_parser("plan", parents=[common],
                                help="validate a local PR overlay plan")
    _pr_flags(pr_plan)
    pr_plan.set_defaults(func=cmd_pr_plan)
    pr_analyze = pr_sub.add_parser("analyze", parents=[common],
                                   help="build a local PR overlay + report")
    _pr_flags(pr_analyze)
    pr_analyze.add_argument("--wait", action="store_true")
    pr_analyze.set_defaults(func=cmd_pr_analyze)
    pr_show = pr_sub.add_parser("show", parents=[common], help="show a pr overlay")
    _ws(pr_show)
    pr_show.add_argument("--overlay", required=True)
    pr_show.set_defaults(func=cmd_pr_show)
    pr.set_defaults(func=None, _parser=pr)

    # -- impact -------------------------------------------------------------
    impact = sub.add_parser("impact", parents=[common],
                           help="Change Impact Packet export/verification")
    impact_sub = impact.add_subparsers(dest="impact_command",
                                       metavar="<subcommand>")
    i_export = impact_sub.add_parser("export", parents=[common],
                                     help="export a Change Impact Packet Draft")
    _ws(i_export)
    i_export.add_argument("--overlay", required=True)
    i_export.add_argument("--output", required=True, help="output directory")
    i_export.set_defaults(func=cmd_impact_export)
    i_verify = impact_sub.add_parser("verify", parents=[common],
                                     help="verify a Change Impact Packet")
    i_verify.add_argument("path", help="packet directory")
    i_verify.set_defaults(func=cmd_impact_verify)
    impact.set_defaults(func=None, _parser=impact)


__all__ = ["register"]
