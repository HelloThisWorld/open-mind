"""The OpenMind command-line interface.

    python -m openmind.cli --help

One runtime, four front ends: this CLI, the MCP server, the FastAPI app and the
tests all go through :func:`openmind.runtime.get_runtime`, so a workspace
created here is the same workspace the UI shows, and a job enqueued here is
executed by the same worker.

MACHINE-READABLE MODE
---------------------
``--json`` prints exactly ONE JSON object on stdout and nothing else. Every
human message, warning and progress line goes to stderr. No ANSI escapes are
ever emitted, in either mode. A failure still prints a JSON object —
``{"ok": false, "error": {"code": ..., "message": ...}}`` — so the output stays
parseable when the exit code is non-zero.

EXIT CODES
----------
    0  success
    1  the operation completed but the domain result is a failure
       (doctor found a problem; the workspace or job does not exist)
    2  invalid arguments or configuration
    3  a required runtime dependency or backend is unavailable
    4  job or execution failure
    5  timeout or cancellation

argparse's own usage errors also exit 2, which matches.

WHY argparse
------------
The contract above is a handful of subcommands, five global flags and six exit
codes. The standard library covers all of it, and a dependency added only to
save a few lines of parser wiring would be a dependency to audit, pin and
install on every platform in CI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain.errors import (DependencyUnavailable, InvalidRequest,
                            OpenMindError, OperationTimeout)
from .version import RUNTIME_VERSION

EXIT_OK = 0
EXIT_DOMAIN_FAILURE = 1
EXIT_INVALID_USAGE = 2
EXIT_DEPENDENCY = 3
EXIT_JOB_FAILURE = 4
EXIT_TIMEOUT = 5

PROG = "openmind"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class Output:
    """Routes human text to stderr and the machine-readable result to stdout.

    In JSON mode stdout carries exactly one object, emitted once at the end.
    That is what lets a caller do ``openmind status --json | jq`` without
    filtering progress noise, and why every ``say``/``warn`` goes to stderr
    even in human mode — the two streams keep the same meaning in both modes.
    """

    def __init__(self, as_json: bool = False, quiet: bool = False,
                 verbose: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.verbose = verbose

    def say(self, message: str) -> None:
        """Human progress. Suppressed by --quiet, never on stdout."""
        if not self.quiet and not self.as_json:
            print(message, file=sys.stderr, flush=True)

    def detail(self, message: str) -> None:
        """Extra diagnostics, only with --verbose."""
        if self.verbose:
            print(message, file=sys.stderr, flush=True)

    def warn(self, message: str) -> None:
        """A warning. Survives --quiet (it is not progress chatter), but is
        still stderr so it never contaminates JSON output."""
        if not self.as_json or self.verbose:
            print(f"warning: {message}", file=sys.stderr, flush=True)

    def emit(self, payload: Dict[str, Any],
             human: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """Print the final result."""
        if self.as_json:
            print(json.dumps(payload, indent=2, default=str), flush=True)
        elif human is not None and not self.quiet:
            human(payload)

    def emit_error(self, error: Dict[str, Any]) -> None:
        if self.as_json:
            print(json.dumps({"ok": False, "error": error}, indent=2, default=str),
                  flush=True)
        else:
            print(f"error: {error.get('message', 'unknown error')}",
                  file=sys.stderr, flush=True)
            details = error.get("details") or {}
            available = details.get("available")
            if available:
                print("  available: " + ", ".join(str(a) for a in available),
                      file=sys.stderr, flush=True)


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _abs_path(path: str) -> str:
    """Resolve a user-supplied path to an absolute, forward-slash form.

    The CLI resolves relative paths against the current directory because a
    stored ``./fixtures/sample-repo`` would otherwise be re-resolved against
    whatever directory the job worker happens to run in. This absolute path is
    stored in the MACHINE-LOCAL sidecar only — it never reaches the portable
    database or an exported artifact.
    """
    from . import walker
    return walker.norm(os.path.abspath(os.path.expanduser(path)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Non-destructive runtime diagnostics."""
    from .runtime import get_runtime

    runtime = get_runtime()
    report = runtime.health.report()
    payload = _ok(report.as_dict())
    payload["runtime"] = runtime.info()

    def human(_p: Dict[str, Any]) -> None:
        print(f"OpenMind {report.version}")
        print(f"  data dir:       {runtime.info()['data_dir']}")
        print(f"  schema version: {runtime.info()['schema_version']}")
        print()
        for check in report.checks:
            mark = {"ok": "  ok  ", "warn": " warn ", "error": "ERROR "}.get(
                check.status, "  ?   ")
            print(f"[{mark}] {check.name}: {check.detail}")
        print()
        failures, warnings = report.failures(), report.warnings()
        if failures:
            print(f"{len(failures)} problem(s) found.")
        elif warnings:
            print(f"No problems. {len(warnings)} warning(s) — "
                  f"optional features are degraded or unconfigured.")
        else:
            print("All checks passed.")

    out.emit(payload, human)
    # Warnings never fail doctor: a missing optional local model must not make
    # the whole command fail for someone who never asked for a model.
    return (EXIT_DOMAIN_FAILURE if not report.ok else EXIT_OK), payload


def cmd_init(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Create a workspace, optionally register a path and ingest it."""
    from .runtime import get_runtime

    runtime = get_runtime()
    path = _abs_path(args.path) if args.path else None
    if path and not os.path.isdir(path):
        raise InvalidRequest(f"source path is not a directory: {path}",
                             details={"path": path})

    workspace = runtime.workspaces.create(args.name, path=path,
                                          exclude=args.exclude or [])
    out.say(f"Created workspace {workspace['id']} ({workspace['name']}).")
    payload = _ok({"workspace_id": workspace["id"], "workspace": workspace})

    if not args.ingest:
        if path:
            out.say(f"Registered source path: {path}")
        out.say("Not ingesting (pass --ingest to learn this workspace now).")
        out.emit(payload, lambda p: print(p["workspace_id"]))
        return EXIT_OK, payload

    code, ingest_payload = _run_ingest(runtime, out, workspace["id"], None,
                                       args.wait, args.timeout)
    # Merge the ingest outcome INCLUDING its ok flag: a workspace that was
    # created but whose ingest failed or timed out must not report ok=true.
    payload.update(ingest_payload)
    payload["workspace_id"] = workspace["id"]
    payload["workspace"] = workspace
    out.emit(payload, lambda p: print(p["workspace_id"]))
    return code, payload


def cmd_add(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Register or update a source path on an existing workspace."""
    from .runtime import get_runtime

    runtime = get_runtime()
    path = _abs_path(args.path)
    if not os.path.isdir(path):
        raise InvalidRequest(f"source path is not a directory: {path}",
                             details={"path": path})

    workspace = runtime.workspaces.add_path(args.workspace, path,
                                            args.exclude or [])
    out.say(f"Registered {path} on workspace {args.workspace}.")
    payload = _ok({"workspace_id": workspace["id"], "workspace": workspace,
                   "paths": workspace.get("paths", [])})

    def human(p: Dict[str, Any]) -> None:
        for spec in p["paths"]:
            excludes = spec.get("exclude") or []
            suffix = f"  (excluding {len(excludes)})" if excludes else ""
            print(f"{spec['path']}{suffix}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_ingest(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Enqueue an ingest, optionally waiting for it."""
    from .runtime import get_runtime

    runtime = get_runtime()
    code, payload = _run_ingest(runtime, out, args.workspace, args.path,
                                args.wait, args.timeout)
    # The job id is present on every path, including the timeout one — a
    # timed-out job is still running and still pollable by id.
    out.emit(payload, lambda p: print(p.get("job_id", "")))
    return code, payload


def _run_ingest(runtime: Any, out: Output, workspace_id: str,
                path: Optional[str], wait: bool,
                timeout: float) -> Tuple[int, Dict[str, Any]]:
    """Shared by ``init --ingest`` and ``ingest``.

    Returns (exit_code, payload) and deliberately does NOT emit: the caller owns
    the single final ``out.emit`` call. Emitting here as well would put two JSON
    objects on stdout and break the one-object contract.
    """
    if wait:
        out.say("Starting the job worker...")

    try:
        result = runtime.ingest.start(workspace_id, path=path, wait=wait,
                                      timeout=timeout)
    except OperationTimeout as exc:
        # The job is NOT cancelled — say so, and hand back the id to poll.
        out.warn(str(exc))
        payload = {"ok": False, "error": exc.as_dict()}
        payload.update(exc.details)
        return EXIT_TIMEOUT, payload

    payload = _ok({"workspace_id": workspace_id, "job_id": result["job_id"],
                   "job": result["job"], "waited": result["waited"]})

    if not result["waited"]:
        out.say(f"Queued ingest job {result['job_id']}.")
        out.say("It runs in the background; poll it with "
                f"'{PROG} status --workspace {workspace_id}'.")
        return EXIT_OK, payload

    status = result.get("status", "")
    payload["status"] = status
    payload["completed"] = result.get("completed", False)
    payload["waited_seconds"] = result.get("waited_seconds")
    progress = (result.get("job") or {}).get("progress") or {}
    payload["progress"] = progress

    if result.get("completed"):
        out.say(f"Ingest finished in {result.get('waited_seconds')}s: "
                f"{progress.get('files_indexed', 0)} file(s) indexed, "
                f"{progress.get('chunks', 0)} chunk(s).")
        return EXIT_OK, payload

    # failed / paused / interrupted — all "did not complete", reported honestly.
    error = (result.get("job") or {}).get("error") or ""
    payload["ok"] = False
    payload["error"] = {
        "code": "job_failed",
        "message": f"ingest job {result['job_id']} did not complete "
                   f"(status: {status})" + (f": {error}" if error else ""),
        "details": {"job_id": result["job_id"], "status": status},
    }
    out.warn(payload["error"]["message"])
    return EXIT_JOB_FAILURE, payload


def cmd_status(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Report workspace metadata, counts, jobs and the schema version."""
    from .runtime import get_runtime

    runtime = get_runtime()
    status = runtime.workspaces.status(args.workspace)
    jobs = runtime.jobs.list([args.workspace])
    active = [j for j in jobs if j.get("status") in ("queued", "running")]
    # Additive Asset-model counts (assets_total/active/removed, revisions,
    # segments, evidence). Backward compatible: every prior key is unchanged.
    assets = runtime.assets.stats(args.workspace)

    payload = _ok({
        "workspace_id": args.workspace,
        "workspace": status["workspace"],
        "state": status["state"],
        "paths": status["paths"],
        "template": status["template"],
        "counts": status["counts"],
        "assets": {k: assets[k] for k in (
            "assets_total", "assets_active", "assets_removed",
            "revisions", "segments", "evidence")},
        "source_root": runtime.workspaces.source_root(args.workspace),
        "active_jobs": active,
        "recent_jobs": jobs[:args.jobs],
        "schema_version": runtime.info()["schema_version"],
        "version": runtime.version,
    })

    def human(p: Dict[str, Any]) -> None:
        ws = p["workspace"]
        print(f"{ws['name']}  ({ws['id']})")
        print(f"  state:           {p['state']}")
        print(f"  created:         {ws.get('created_at', '')}")
        template = p["template"].get("effective")
        source = p["template"].get("effective_source")
        print(f"  template:        "
              f"{template or 'none'}{f' ({source})' if template else ''}")
        print(f"  schema version:  {p['schema_version']}")
        print("  source paths:")
        if not p["paths"]:
            print("    (none registered)")
        for spec in p["paths"]:
            excludes = spec.get("exclude") or []
            suffix = f"  [{len(excludes)} exclusion(s)]" if excludes else ""
            print(f"    {spec['path']}{suffix}")
        print("  counts:")
        for label, key in (("indexed files", "indexed_files"),
                           ("code chunks", "code_chunks"),
                           ("glossary terms", "glossary_terms"),
                           ("solved cases", "solved_cases")):
            value = p["counts"].get(key)
            # None means "could not read that store", NOT zero.
            print(f"    {label:16} {'unknown' if value is None else value}")
        a = p["assets"]
        print("  assets:")
        print(f"    {'total':16} {a['assets_total']} "
              f"({a['assets_active']} active, {a['assets_removed']} removed)")
        print(f"    {'revisions':16} {a['revisions']}")
        print(f"    {'segments':16} {a['segments']}")
        print(f"    {'evidence':16} {a['evidence']}")
        if p["active_jobs"]:
            print("  active jobs:")
            for job in p["active_jobs"]:
                print(f"    {job['job_id']}  {job['type']:8} {job['status']:8} "
                      f"{job.get('step', '')}")
        else:
            print("  active jobs:     none")
        if p["recent_jobs"]:
            print("  recent jobs:")
            for job in p["recent_jobs"]:
                print(f"    {job['job_id']}  {job['type']:8} {job['status']:8} "
                      f"{job.get('created_at', '')}")

    out.emit(payload, human)
    return EXIT_OK, payload


# ---------------------------------------------------------------------------
# Asset model inspection (OpenMind v2 Phase 2)
# ---------------------------------------------------------------------------
def cmd_asset_list(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """List a workspace's assets (bounded), with the total count."""
    from .runtime import get_runtime

    result = get_runtime().assets.list_assets(
        args.workspace, asset_type=args.type, state=args.state,
        limit=args.limit, offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} of {p['total']} asset(s) "
              f"(limit {p['limit']}, offset {p['offset']}):")
        for a in p["assets"]:
            rev = a.get("current_revision_id") or "-"
            print(f"  {a['id']}  {a['state']:11} {a['asset_type']:16} "
                  f"{a['logical_key']}  (rev {rev})")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_asset_show(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Show one asset's metadata and its current-revision summary."""
    from .runtime import get_runtime

    asset = get_runtime().assets.get_asset(args.workspace, args.asset)
    payload = _ok({"asset": asset})

    def human(p: Dict[str, Any]) -> None:
        a = p["asset"]
        print(f"{a['logical_key']}  ({a['id']})")
        print(f"  type:    {a['asset_type']}")
        print(f"  state:   {a['state']}")
        print(f"  created: {a['created_at']}")
        cur = a.get("current_revision")
        if cur:
            print(f"  current revision: {cur['id']} (seq {cur['sequence']}, "
                  f"{cur['segment_count']} segment(s), status {cur['status']})")
            print(f"    content_hash: {cur['content_hash']}")
            if cur.get("source_commit"):
                print(f"    source_commit: {cur['source_commit']}")
        else:
            print("  current revision: none")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_asset_revisions(args: argparse.Namespace,
                        out: Output) -> Tuple[int, Dict[str, Any]]:
    """List an asset's revision history, newest first."""
    from .runtime import get_runtime

    result = get_runtime().assets.list_revisions(
        args.workspace, args.asset, limit=args.limit)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} revision(s) for asset {p['asset_id']}:")
        for r in p["revisions"]:
            print(f"  seq {r['sequence']:>4}  {r['status']:11} "
                  f"{r['content_hash'][:12]}  {r['created_at']}"
                  + (f"  commit {r['source_commit'][:10]}" if r["source_commit"] else ""))

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_asset_segments(args: argparse.Namespace,
                       out: Output) -> Tuple[int, Dict[str, Any]]:
    """List a revision's segments (bounded). Never prints full source content."""
    from .runtime import get_runtime

    result = get_runtime().assets.list_segments(
        args.workspace, args.revision, limit=args.limit, offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} of {p['total']} segment(s) for revision "
              f"{p['revision_id']}:")
        for s in p["segments"]:
            span = (f"{s['start_line']}-{s['end_line']}"
                    if s.get("start_line") else "-")
            print(f"  {s['ordinal']:>4}  {s['segment_type']:11} {s['content_mode']:8} "
                  f"L{span:<11} {s['segment_key']}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_asset_evidence(args: argparse.Namespace,
                       out: Output) -> Tuple[int, Dict[str, Any]]:
    """Show one evidence citation: its locator, snapshot/current-source status
    and bounded recovered content."""
    from .runtime import get_runtime

    result = get_runtime().assets.get_evidence(
        args.workspace, args.evidence, max_chars=args.max_chars)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        loc = p["locator"]
        print(f"evidence {p['id']}  (revision {p['revision_id']})")
        print(f"  locator: {loc.get('file')}:{loc.get('startLine')}-{loc.get('endLine')}"
              f"  {loc.get('symbol') or ''}")
        print(f"  snapshot:       {p['snapshot']['status']}")
        print(f"  current source: {p['current_source']['status']}")
        rev = p.get("revision") or {}
        if rev:
            print(f"  revision:       seq {rev['sequence']} ({rev['status']})")
        print(f"  content ({len(p['content'])} chars"
              f"{', truncated' if p['truncated'] else ''}):")
        for line in p["content"].splitlines():
            print(f"    | {line}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_asset_add(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Ingest a single existing file that lives under a registered source root."""
    from .runtime import get_runtime

    runtime = get_runtime()
    if args.wait:
        out.say("Starting the job worker...")
    try:
        result = runtime.assets.sync_file(args.workspace, args.path,
                                          wait=args.wait, timeout=args.timeout)
    except OperationTimeout as exc:
        out.warn(str(exc))
        payload = {"ok": False, "error": exc.as_dict()}
        payload.update(exc.details)
        return EXIT_TIMEOUT, payload

    payload = _ok(result)
    if not result.get("supported"):
        out.say(f"Registered unsupported file {result['logical_key']} "
                f"(state: {result['state']}); not parsed.")
    elif result.get("waited") and not result.get("completed"):
        payload["ok"] = False
        payload["error"] = {"code": "job_failed",
                            "message": f"asset add did not complete "
                                       f"(status: {result.get('status')})"}
        out.warn(payload["error"]["message"])
        out.emit(payload, lambda p: print(p.get("job_id", "")))
        return EXIT_JOB_FAILURE, payload
    else:
        out.say(f"Synced {result['logical_key']} "
                f"(job {result.get('job_id')}).")

    out.emit(payload, lambda p: print(p.get("asset_id") or p.get("job_id") or ""))
    return EXIT_OK, payload


# ---------------------------------------------------------------------------
# Documents (OpenMind v2 Phase 3)
# ---------------------------------------------------------------------------
def cmd_document_add(args: argparse.Namespace,
                     out: Output) -> Tuple[int, Dict[str, Any]]:
    """Append a document from anywhere on this machine.

    The import DECISION is part of the result, not a hidden detail: an exact
    duplicate, a filename collision needing the user's choice, and a real new
    revision are three different outcomes and are reported as such.
    """
    from .runtime import get_runtime
    from .domain.types import ImportStatus

    if sum(bool(x) for x in (args.asset, args.logical_key, args.new_asset)) > 1:
        raise InvalidRequest(
            "--asset, --logical-key and --new-asset are mutually exclusive: "
            "each names a DIFFERENT target for these bytes, so combining them "
            "has no single correct meaning",
            details={"asset": args.asset, "logical_key": args.logical_key,
                     "new_asset": args.new_asset})

    runtime = get_runtime()
    if args.wait and not args.dry_run:
        out.say("Starting the job worker...")
    try:
        result = runtime.documents.add_document(
            args.workspace, args.path, asset_id=args.asset or "",
            logical_key=args.logical_key or "", new_asset=bool(args.new_asset),
            version_label=args.version_label or "", wait=args.wait,
            timeout=args.timeout, dry_run=bool(args.dry_run))
    except OperationTimeout as exc:
        out.warn(str(exc))
        payload = {"ok": False, "error": exc.as_dict()}
        payload.update(exc.details)
        return EXIT_TIMEOUT, payload

    payload = _ok(result)
    status = result.get("status", "")

    # The outcome is decided BEFORE emitting. `emit` serializes the payload at
    # the moment it is called, so setting ok/error afterwards would print
    # `"ok": true` on stdout for a run that then exits non-zero — the exact
    # mismatch a script would trust and get wrong.
    exit_code = EXIT_OK
    if status == ImportStatus.POSSIBLE_REVISION:
        # Nothing was written and the user must choose.
        exit_code = EXIT_DOMAIN_FAILURE
        payload["ok"] = False
        payload["error"] = {
            "code": "possible_revision",
            "message": result.get("reason", "a different document already uses "
                                             "that key"),
            "details": {"possible_asset_id": (result.get("possible_asset")
                                              or {}).get("id", ""),
                        "guidance": result.get("guidance", {})},
        }
    elif status == ImportStatus.UNSUPPORTED:
        exit_code = EXIT_DOMAIN_FAILURE
        payload["ok"] = False
        payload["error"] = {"code": "unsupported_document",
                            "message": result.get("reason", "unsupported")}
    elif result.get("waited") and not result.get("completed"):
        exit_code = EXIT_JOB_FAILURE
        payload["ok"] = False
        payload["error"] = result.get("error") or {
            "code": "job_failed",
            "message": f"document import did not complete "
                       f"(status: {result.get('status_job')})"}

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['status']}  {p.get('logical_key', '')}")
        if p.get("reason"):
            print(f"  {p['reason']}")
        guidance = p.get("guidance") or {}
        if guidance:
            print("  resolve it with ONE of:")
            print(f"    --asset {p['possible_asset']['id']}"
                  f"      (these bytes are a new revision of that document)")
            print(f"    --new-asset"
                  f"                  (this is a different document; it will "
                  f"become {guidance['suggested_new_key']})")
            print(f"    --logical-key <key>       (name it yourself)")
        report = p.get("import_report") or {}
        if report:
            print(f"  parser:    {report.get('parser')} "
                  f"({report.get('parse_status')})")
            print(f"  asset:     {report.get('asset_id')}")
            print(f"  revision:  {report.get('revision_id')} "
                  f"(seq {report.get('revision_sequence')})")
            print(f"  segments:  {report.get('segments_created')} "
                  f"({report.get('blocks_indexed')} indexed)")
            if report.get("version_label"):
                print(f"  version:   {report['version_label']} "
                      f"({report.get('version_label_source')})")
            for warning in report.get("warnings") or []:
                print(f"  warning:   {warning['code']}: {warning['message']}")
            for entry in report.get("unsupported_content") or []:
                print(f"  not read:  {entry['kind']} x{entry['count']}")
            related = report.get("related_candidates") or {}
            if related.get("count"):
                print(f"  candidates: {related['count']} related CANDIDATE(s) "
                      f"— observed mentions, not confirmed relations:")
                for candidate in related.get("top", []):
                    target = (candidate["target"].get("logical_key")
                              or candidate["target"].get("symbol") or "")
                    print(f"    {candidate['confidence']:6} "
                          f"{candidate['candidate_type']:24} {target}")
        elif p.get("job_id"):
            print(f"  job: {p['job_id']}")

    if payload.get("error"):
        out.warn(payload["error"]["message"])
    out.emit(payload, human)
    return exit_code, payload


def cmd_document_list(args: argparse.Namespace,
                      out: Output) -> Tuple[int, Dict[str, Any]]:
    """List the workspace's document assets (bounded)."""
    from .runtime import get_runtime

    result = get_runtime().documents.list_documents(
        args.workspace, status=args.status, parser=args.parser,
        state=args.state, limit=args.limit, offset=args.offset)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} of {p['total']} document(s):")
        for record in p["documents"]:
            info = record.get("document") or {}
            print(f"  {record['id']}  {info.get('status', ''):11} "
                  f"{info.get('parser_name', ''):12} {record['logical_key']}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_document_show(args: argparse.Namespace,
                      out: Output) -> Tuple[int, Dict[str, Any]]:
    """Show one document: asset, current revision and parse summary."""
    from .runtime import get_runtime

    document = get_runtime().documents.get_document(args.workspace, args.asset)
    payload = _ok({"document": document})

    def human(p: Dict[str, Any]) -> None:
        d = p["document"]
        parse = d["parse"]
        print(f"{d['logical_key']}  ({d['id']})")
        print(f"  title:     {parse['title']}")
        print(f"  parser:    {parse['parser_name']} {parse['parser_version']} "
              f"-> {parse['status']}")
        print(f"  media:     {parse['media_type']}")
        print(f"  state:     {d['state']}  (source: {d['source_kind']})")
        current = d.get("current_revision") or {}
        if current:
            print(f"  revision:  {current['id']} (seq {current['sequence']}, "
                  f"{current['segment_count']} segment(s))")
            if current.get("version_label"):
                print(f"  version:   {current['version_label']}")
        print(f"  indexed:   {d['index']['chunk_count']} chunk(s)")
        for key, value in sorted((parse.get("coverage") or {}).items()):
            print(f"  coverage.{key}: {value}")
        for warning in parse.get("warnings") or []:
            print(f"  warning:   {warning['code']}: {warning['message']}")
        for entry in parse.get("unsupported_content") or []:
            print(f"  not read:  {entry['kind']} x{entry['count']}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_document_outline(args: argparse.Namespace,
                         out: Output) -> Tuple[int, Dict[str, Any]]:
    """Print a bounded STRUCTURAL outline of a document revision."""
    from .runtime import get_runtime

    result = get_runtime().documents.get_outline(
        args.workspace, args.revision, limit=args.limit)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} of {p['total']} block(s) in revision "
              f"{p['revision_id']}  ({p['parse']['parser_name']}):")
        for entry in p["outline"]:
            depth = len(entry.get("heading_path") or [])
            indent = "  " * min(depth, 6)
            print(f"  {indent}{entry['block_type']:12} {entry['preview']}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_document_search(args: argparse.Namespace,
                        out: Output) -> Tuple[int, Dict[str, Any]]:
    """Search the workspace's documents."""
    from .runtime import get_runtime

    result = get_runtime().documents.search(
        args.workspace, args.query, limit=args.limit, parser=args.parser,
        block_type=args.block_type, logical_key=args.logical_key)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} hit(s)  [{p['query_mode']}]")
        for hit in p["hits"]:
            print(f"  {hit['logical_key']}  {hit['block_type']}  "
                  f"({', '.join(hit['retrieval_sources'])})")
            print(f"    evidence {hit['evidence_id']}: {hit['excerpt'][:160]}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_document_related(args: argparse.Namespace,
                         out: Output) -> Tuple[int, Dict[str, Any]]:
    """Deterministic candidate associations for one document."""
    from .runtime import get_runtime

    result = get_runtime().documents.find_related_candidates(
        args.workspace, args.asset, limit=args.limit)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"{p['count']} CANDIDATE(s) — observed mentions, NOT confirmed "
              f"relations:")
        for candidate in p["candidates"]:
            target = (candidate["target"].get("logical_key")
                      or candidate["target"].get("symbol") or "")
            print(f"  {candidate['confidence']:6} "
                  f"{candidate['candidate_type']:26} {candidate['mention'][:28]:30} "
                  f"-> {target}")
            print(f"         {candidate['reason']}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_knowledge_search(args: argparse.Namespace,
                         out: Output) -> Tuple[int, Dict[str, Any]]:
    """Search code and documents together, reported separately."""
    from .runtime import get_runtime

    result = get_runtime().documents.search_knowledge(
        args.workspace, args.query, code_limit=args.limit,
        document_limit=args.limit)
    payload = _ok(result)

    def human(p: Dict[str, Any]) -> None:
        print(f"code: {p['grounding']['codeCount']} hit(s)")
        for chunk in p["code"]["hits"]:
            print(f"  {chunk.get('file_path', '')}  {chunk.get('symbol', '')}")
        print(f"documents: {p['grounding']['documentCount']} hit(s)")
        for hit in p["documents"]["hits"]:
            print(f"  {hit['logical_key']}  {hit['block_type']}: "
                  f"{hit['excerpt'][:120]}")
        print(p["grounding"]["note"])

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_export(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Generate the deterministic ``.openmind`` artifact directory.

    Deliberately does NOT bootstrap the runtime: export is offline and
    standalone, and must not open the database or the vector store.
    """
    from .services.export_service import ExportService

    summary = ExportService().export(
        args.repo, args.output, name=args.name, template=args.template,
        no_template=args.no_template, generated_at=args.generated_at)
    payload = _ok(dict(summary))

    def human(p: Dict[str, Any]) -> None:
        print(f"Open Mind artifacts written to {p['output']}")
        print(f"  schema version:           {p['schemaVersion']}")
        print(f"  repository:               {p['repository']['name']} "
              f"(commit: {p['repository']['commit'] or 'n/a'})")
        print(f"  template profile:         {p['template'] or 'none'}")
        print(f"  files indexed:            {p['filesIndexed']}")
        print(f"  symbols indexed:          {p['symbolsIndexed']}")
        print(f"  glossary entries:         {p['glossaryEntries']}")
        print(f"  architecture components:  {p['architectureComponents']}")
        print(f"  flows:                    {p['flows']}")
        print(f"  artifact files:           {', '.join(p['files'])}")

    out.emit(payload, human)
    return EXIT_OK, payload


def cmd_mcp_serve(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Run the MCP stdio server — the same implementation as
    ``python -m openmind.mcp_server``, not a second copy of the tools."""
    from .runtime import get_runtime

    try:
        from .mcp_server import create_mcp_server
    except SystemExit as exc:
        # mcp_server raises SystemExit when the 'mcp' package is absent.
        raise DependencyUnavailable(str(exc) or "the 'mcp' package is required",
                                    dependency="mcp") from exc

    runtime = get_runtime()
    # stderr only: stdout IS the MCP transport and must carry protocol frames.
    out.say("Starting the OpenMind MCP server on stdio...")
    server = create_mcp_server(runtime)
    server.run()
    return EXIT_OK, _ok({"served": "mcp"})


def cmd_serve(args: argparse.Namespace, out: Output) -> Tuple[int, Dict[str, Any]]:
    """Run the FastAPI application."""
    from . import config

    host = args.host
    if host.strip().lower() not in config.LOOPBACK_HOSTS and not args.allow_non_loopback:
        # Never silently bind to a public interface: every prompt, path and
        # snippet the API serves would be reachable from the network.
        raise InvalidRequest(
            f"refusing to bind to non-loopback host {host!r}. OpenMind serves "
            f"project content and is loopback-only by default; pass "
            f"--allow-non-loopback if you really intend to expose it.",
            details={"host": host})

    try:
        import uvicorn
    except ImportError as exc:
        raise DependencyUnavailable(
            "uvicorn is required to serve the web application "
            "(pip install 'uvicorn[standard]')", dependency="uvicorn") from exc

    out.say(f"Starting Open Mind on http://{host}:{args.port}  (Ctrl+C to stop)")
    # timeout_graceful_shutdown: an open SSE job stream never completes on its
    # own, so without a bound uvicorn waits forever on Ctrl+C.
    uvicorn.run("openmind.main:app", host=host, port=args.port,
                timeout_graceful_shutdown=5, log_level=(
                    "debug" if args.verbose else "warning" if args.quiet else "info"))
    return EXIT_OK, _ok({"served": "http", "host": host, "port": args.port})


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _global_flags() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand, so
    ``openmind --json doctor`` and ``openmind doctor --json`` both work.

    ``default=SUPPRESS`` is load-bearing. A shared parent parser is applied to
    BOTH the top-level parser and every subparser, and both write into the same
    namespace — so with an ordinary ``default=False`` the subparser would reset
    a flag that was given before the subcommand, and ``openmind --quiet doctor``
    would silently not be quiet. With SUPPRESS an unspecified flag sets nothing,
    the earlier value survives, and :func:`build_parser` supplies the base
    defaults once.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="print one machine-readable JSON object on stdout")
    parent.add_argument("--quiet", "-q", action="store_true",
                        default=argparse.SUPPRESS,
                        help="suppress human progress output")
    parent.add_argument("--verbose", "-v", action="store_true",
                        default=argparse.SUPPRESS,
                        help="print extra diagnostics on stderr")
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _global_flags()
    parser = argparse.ArgumentParser(
        prog=PROG, parents=[common],
        description="OpenMind — a local, grounded engineering knowledge runtime. "
                    "The web UI is optional: this CLI, the MCP server and the "
                    "FastAPI app all drive the same runtime.",
        epilog="Exit codes: 0 success, 1 domain failure, 2 invalid usage, "
               "3 missing dependency, 4 job failure, 5 timeout.")
    parser.add_argument("--version", action="version",
                        version=f"{PROG} {RUNTIME_VERSION}")
    # NOTE: deliberately no parser.set_defaults() for the global flags.
    # set_defaults() rewrites action.default in place, and `parents=` SHARES
    # Action objects with every subparser — so setting a default here would
    # undo the SUPPRESS in _global_flags() and re-break `openmind --quiet
    # doctor`. main() supplies the defaults with getattr instead.
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = sub.add_parser("doctor", parents=[common],
                            help="run non-destructive runtime diagnostics")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", parents=[common],
                          help="create a workspace and optionally ingest it")
    init.add_argument("--name", required=True, help="workspace display name")
    init.add_argument("--path", help="source directory to register")
    init.add_argument("--exclude", action="append", metavar="PATTERN",
                      help="path to exclude (repeatable)")
    init.add_argument("--ingest", action="store_true",
                      help="ingest immediately after creating (off by default)")
    init.add_argument("--wait", action="store_true",
                      help="with --ingest, wait for the job to finish")
    init.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS",
                      help="bound for --wait (default: 3600)")
    init.set_defaults(func=cmd_init)

    add = sub.add_parser("add", parents=[common],
                         help="register or update a source path")
    add.add_argument("--workspace", required=True, help="workspace id")
    add.add_argument("--path", required=True, help="source directory")
    add.add_argument("--exclude", action="append", metavar="PATTERN",
                     help="path to exclude (repeatable)")
    add.set_defaults(func=cmd_add)

    ingest = sub.add_parser("ingest", parents=[common],
                            help="enqueue an ingest job")
    ingest.add_argument("--workspace", required=True, help="workspace id")
    ingest.add_argument("--path", help="restrict the ingest to this subtree")
    ingest.add_argument("--wait", action="store_true",
                        help="wait for the job to reach a terminal state")
    ingest.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS",
                        help="bound for --wait (default: 3600)")
    ingest.set_defaults(func=cmd_ingest)

    status = sub.add_parser("status", parents=[common],
                            help="report workspace state, counts and jobs")
    status.add_argument("--workspace", required=True, help="workspace id")
    status.add_argument("--jobs", type=int, default=5, metavar="N",
                        help="how many recent jobs to list (default: 5)")
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export", parents=[common],
                            help="write the deterministic .openmind artifacts")
    export.add_argument("--repo", required=True, help="repository root to analyze")
    export.add_argument("--output", required=True, help="directory to write into")
    export.add_argument("--name", help="repository display name")
    export.add_argument("--generated-at", dest="generated_at", metavar="ISO8601",
                        help="override generatedAt (reproducible builds)")
    tgroup = export.add_mutually_exclusive_group()
    tgroup.add_argument("--template", metavar="NAME",
                        help="apply a named template profile "
                             "(default: deterministic auto-selection)")
    tgroup.add_argument("--no-template", dest="no_template", action="store_true",
                        help="skip template profiles entirely")
    export.set_defaults(func=cmd_export)

    mcp = sub.add_parser("mcp", parents=[common],
                         help="Model Context Protocol server")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", metavar="<subcommand>")
    mcp_serve = mcp_sub.add_parser("serve", parents=[common],
                                   help="run the MCP stdio server")
    mcp_serve.set_defaults(func=cmd_mcp_serve)
    mcp.set_defaults(func=None, _parser=mcp)

    asset = sub.add_parser("asset", parents=[common],
                           help="inspect the canonical Asset model")
    asset_sub = asset.add_subparsers(dest="asset_command", metavar="<subcommand>")

    a_list = asset_sub.add_parser("list", parents=[common],
                                  help="list a workspace's assets (bounded)")
    a_list.add_argument("--workspace", required=True, help="workspace id")
    a_list.add_argument("--type", dest="type", help="filter by asset type")
    a_list.add_argument("--state", help="filter by state (active/removed/unsupported)")
    a_list.add_argument("--limit", type=int, default=100, metavar="N",
                        help="max assets to return (default: 100)")
    a_list.add_argument("--offset", type=int, default=0, metavar="N",
                        help="skip the first N (default: 0)")
    a_list.set_defaults(func=cmd_asset_list)

    a_show = asset_sub.add_parser("show", parents=[common],
                                  help="show one asset + its current revision")
    a_show.add_argument("--workspace", required=True, help="workspace id")
    a_show.add_argument("--asset", required=True, help="asset id")
    a_show.set_defaults(func=cmd_asset_show)

    a_revs = asset_sub.add_parser("revisions", parents=[common],
                                  help="list an asset's revision history")
    a_revs.add_argument("--workspace", required=True, help="workspace id")
    a_revs.add_argument("--asset", required=True, help="asset id")
    a_revs.add_argument("--limit", type=int, default=50, metavar="N",
                        help="max revisions to return (default: 50)")
    a_revs.set_defaults(func=cmd_asset_revisions)

    a_segs = asset_sub.add_parser("segments", parents=[common],
                                  help="list a revision's segments (bounded)")
    a_segs.add_argument("--workspace", required=True, help="workspace id")
    a_segs.add_argument("--revision", required=True, help="revision id")
    a_segs.add_argument("--limit", type=int, default=100, metavar="N",
                        help="max segments to return (default: 100)")
    a_segs.add_argument("--offset", type=int, default=0, metavar="N",
                        help="skip the first N (default: 0)")
    a_segs.set_defaults(func=cmd_asset_segments)

    a_ev = asset_sub.add_parser("evidence", parents=[common],
                                help="show one evidence citation (bounded content)")
    a_ev.add_argument("--workspace", required=True, help="workspace id")
    a_ev.add_argument("--evidence", required=True, help="evidence id")
    a_ev.add_argument("--max-chars", dest="max_chars", type=int, default=4000,
                      metavar="N", help="max recovered content chars (default: 4000)")
    a_ev.set_defaults(func=cmd_asset_evidence)

    a_add = asset_sub.add_parser("add", parents=[common],
                                 help="ingest a single existing file")
    a_add.add_argument("--workspace", required=True, help="workspace id")
    a_add.add_argument("--path", required=True, help="a single existing file")
    a_add.add_argument("--wait", action="store_true",
                       help="wait for the ingest to finish")
    a_add.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS",
                       help="bound for --wait (default: 3600)")
    a_add.set_defaults(func=cmd_asset_add)

    asset.set_defaults(func=None, _parser=asset)

    # -- documents (v2 Phase 3) --------------------------------------------
    document = sub.add_parser("document", parents=[common],
                              help="append and query enterprise documents")
    document_sub = document.add_subparsers(dest="document_command",
                                           metavar="<subcommand>")

    d_add = document_sub.add_parser(
        "add", parents=[common],
        help="append a document from anywhere on this machine")
    d_add.add_argument("--workspace", required=True, help="workspace id")
    d_add.add_argument("--path", required=True, help="the document file")
    # Mutually exclusive in MEANING as well as in the parser: each names a
    # different target for the same bytes.
    d_target = d_add.add_mutually_exclusive_group()
    d_target.add_argument("--asset", metavar="ASSET_ID",
                          help="treat the bytes as a new revision of this asset")
    d_target.add_argument("--logical-key", dest="logical_key", metavar="KEY",
                          help="find or create the asset with this key")
    d_target.add_argument("--new-asset", dest="new_asset", action="store_true",
                          help="create a distinct asset with a deterministic key")
    d_add.add_argument("--version-label", dest="version_label", metavar="LABEL",
                       help="explicit version label for the new revision")
    d_add.add_argument("--wait", action="store_true",
                       help="wait for the import job to finish")
    d_add.add_argument("--timeout", type=float, default=3600.0,
                       metavar="SECONDS", help="bound for --wait (default: 3600)")
    d_add.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="report the import plan and store nothing")
    d_add.set_defaults(func=cmd_document_add)

    d_list = document_sub.add_parser("list", parents=[common],
                                     help="list document assets (bounded)")
    d_list.add_argument("--workspace", required=True, help="workspace id")
    d_list.add_argument("--status", help="filter by parse status "
                                         "(parsed/partial/needs-ocr/...)")
    d_list.add_argument("--parser", help="filter by parser name")
    d_list.add_argument("--state", default="active",
                        help="asset state (default: active)")
    d_list.add_argument("--limit", type=int, default=100, metavar="N",
                        help="max documents to return (default: 100)")
    d_list.add_argument("--offset", type=int, default=0, metavar="N",
                        help="skip the first N (default: 0)")
    d_list.set_defaults(func=cmd_document_list)

    d_show = document_sub.add_parser(
        "show", parents=[common],
        help="show one document, its current revision and parse summary")
    d_show.add_argument("--workspace", required=True, help="workspace id")
    d_show.add_argument("--asset", required=True, help="document asset id")
    d_show.set_defaults(func=cmd_document_show)

    d_outline = document_sub.add_parser(
        "outline", parents=[common],
        help="bounded structural outline of a document revision")
    d_outline.add_argument("--workspace", required=True, help="workspace id")
    d_outline.add_argument("--revision", required=True, help="revision id")
    d_outline.add_argument("--limit", type=int, default=500, metavar="N",
                           help="max blocks to return (default: 500)")
    d_outline.set_defaults(func=cmd_document_outline)

    d_search = document_sub.add_parser("search", parents=[common],
                                       help="search the workspace's documents")
    d_search.add_argument("--workspace", required=True, help="workspace id")
    d_search.add_argument("--query", required=True, help="the search query")
    d_search.add_argument("--limit", type=int, default=20, metavar="N",
                          help="max hits to return (default: 20)")
    d_search.add_argument("--parser", help="filter by parser name")
    d_search.add_argument("--block-type", dest="block_type",
                          help="filter by block type")
    d_search.add_argument("--logical-key", dest="logical_key",
                          help="restrict to one document")
    d_search.set_defaults(func=cmd_document_search)

    d_related = document_sub.add_parser(
        "related", parents=[common],
        help="deterministic candidate associations for one document")
    d_related.add_argument("--workspace", required=True, help="workspace id")
    d_related.add_argument("--asset", required=True, help="document asset id")
    d_related.add_argument("--limit", type=int, default=30, metavar="N",
                           help="max candidates to return (default: 30)")
    d_related.set_defaults(func=cmd_document_related)

    document.set_defaults(func=None, _parser=document)

    knowledge = sub.add_parser("knowledge", parents=[common],
                               help="query code and documents together")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command",
                                             metavar="<subcommand>")
    k_search = knowledge_sub.add_parser(
        "search", parents=[common],
        help="search code and documents, reported separately")
    k_search.add_argument("--workspace", required=True, help="workspace id")
    k_search.add_argument("--query", required=True, help="the search query")
    k_search.add_argument("--limit", type=int, default=12, metavar="N",
                          help="max hits per side (default: 12)")
    k_search.set_defaults(func=cmd_knowledge_search)
    knowledge.set_defaults(func=None, _parser=knowledge)

    # -- semantic plane (v2 Phase 4): provider / semantic / lens groups ----
    from . import cli_semantic
    cli_semantic.register(sub, common)

    # -- canonical knowledge graph (v2 Phase 5): graph / promotion /
    # entity / claim / relation / bundle groups, plus the history
    # subcommands of the existing `knowledge` group above.
    from . import cli_knowledge
    cli_knowledge.register(sub, common, knowledge_sub)

    # -- formal traceability + governed conflicts (v2 Phase 6): the trace
    # and conflict groups.
    from . import cli_trace
    cli_trace.register(sub, common)

    serve = sub.add_parser("serve", parents=[common],
                           help="run the FastAPI web application")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind host (default: 127.0.0.1, loopback only)")
    serve.add_argument("--port", type=int, default=8077, help="bind port")
    serve.add_argument("--allow-non-loopback", dest="allow_non_loopback",
                       action="store_true",
                       help="permit binding to a non-loopback interface "
                            "(exposes project content to the network)")
    serve.set_defaults(func=cmd_serve)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Output(as_json=getattr(args, "as_json", False),
                 quiet=getattr(args, "quiet", False),
                 verbose=getattr(args, "verbose", False))

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return EXIT_INVALID_USAGE
    if getattr(args, "func", None) is None:
        # A group like `mcp` with no subcommand.
        getattr(args, "_parser", parser).print_help(sys.stderr)
        return EXIT_INVALID_USAGE

    try:
        code, _payload = args.func(args, out)
        return code
    except OpenMindError as exc:
        out.emit_error(exc.as_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        out.emit_error({"code": "cancelled", "message": "cancelled by user"})
        return EXIT_TIMEOUT
    except BrokenPipeError:
        # `openmind status --json | head` — not an error worth a traceback.
        return EXIT_OK
    except Exception as exc:
        # Unexpected: report it as structured data too, and keep the traceback
        # available behind --verbose rather than swallowing it.
        out.emit_error({"code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}"})
        if out.verbose:
            import traceback
            traceback.print_exc()
        return EXIT_DOMAIN_FAILURE


if __name__ == "__main__":
    sys.exit(main())
