"""Run the OpenMind acceptance suite.

    python scripts/run_acceptance.py             # the CI gate (core tier)
    python scripts/run_acceptance.py --all       # every supported script
    python scripts/run_acceptance.py --tier local
    python scripts/run_acceptance.py --list
    python scripts/run_acceptance.py --json

Each script runs in its own process with its own isolated ``OPENMIND_DATA_DIR``
and ``OPENMIND_MACHINE_DIR``, and with egress and embeddings forced offline, so
a run is deterministic, touches no live data, and calls no paid or public API.

A SKIP IS NEVER A PASS
----------------------
Every script on disk must appear in :data:`SUITE`. A ``tests/verify_*.py`` that
is not listed makes the runner fail with "not in the manifest" rather than
quietly going unrun — that is the failure mode this file exists to prevent. A
script that cannot execute is reported as ``skipped`` and counted separately;
a skipped CORE script fails the run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

CORE = "core"
LOCAL = "local"


class Script:
    """One acceptance script and why it is in the tier it is in."""

    def __init__(self, name: str, tier: str, summary: str,
                 reason: str = "", timeout: int = 1800) -> None:
        self.name = name
        self.tier = tier
        self.summary = summary
        self.reason = reason        # required for LOCAL: why it is not in CI
        self.timeout = timeout

    @property
    def path(self) -> Path:
        return TESTS_DIR / f"{self.name}.py"


#: The supported acceptance suite. Ordered cheapest-first so an obvious
#: breakage surfaces in seconds rather than minutes.
SUITE: List[Script] = [
    # -- deterministic, dependency-light ------------------------------------
    Script("verify_migrations", CORE, "schema migrations: empty, legacy, checksum, rollback"),
    Script("verify_content_store", CORE,
           "immutable content-addressed blob store: atomic, reuse, corruption"),
    Script("verify_structure", CORE, "detection + structure extraction, incremental"),
    Script("verify_glossary", CORE, "deterministic glossary extraction and lookup"),
    Script("verify_router", CORE, "capability routing and graceful degradation"),
    Script("verify_diagrams", CORE, "deterministic diagram projection"),
    Script("verify_grounding", CORE, "grounded answers and honest not-found"),
    Script("verify_artifacts", CORE, ".openmind export contract (schema 1.1.x)"),
    Script("verify_runtime", CORE, "runtime bootstrap, idempotency, worker opt-in"),
    Script("verify_services", CORE, "application service layer"),
    Script("verify_cli", CORE, "CLI contract: JSON, exit codes, commands"),
    Script("verify_adapters", CORE, "FastAPI route + MCP + skill-bridge compatibility"),

    # -- canonical Asset model (v2 Phase 2) ---------------------------------
    Script("verify_asset_model", CORE,
           "Asset/Revision/Segment/Evidence: ingest, idempotency, change, "
           "revert, removal, reappearance, legacy backfill, lifecycle"),
    Script("verify_asset_cli", CORE, "CLI asset commands: JSON, scoping, bounds"),
    Script("verify_asset_adapters", CORE,
           "additive REST endpoints + read-only MCP asset tools"),

    # -- document ingestion (v2 Phase 3) ------------------------------------
    # Ordered cheapest-first within the group: a broken parser SPI should
    # surface in under a second rather than after a full ingest run.
    Script("verify_document_registry", CORE,
           "parser SPI: probing, deterministic selection, ambiguity, "
           "missing dependency, isolation, lazy imports"),
    Script("verify_document_parsers", CORE,
           "every format's mandatory cases + byte-for-byte determinism"),
    Script("verify_document_security", CORE,
           "ZIP bombs/traversal, XML entities, formula and script "
           "non-execution, no remote fetch, explicit limits"),
    Script("verify_document_ingest", CORE,
           "append workflow: duplicate, revision, collision, new-asset, "
           "removal, reappearance, detached attachment, evidence recovery"),
    Script("verify_document_search", CORE,
           "document retrieval, combined knowledge search, deterministic "
           "candidate association"),
    Script("verify_document_cli", CORE,
           "document CLI contract: JSON, exit codes, bounds"),
    Script("verify_document_adapters", CORE,
           "additive REST routes + read-only MCP document tools + the "
           "compatibility gate"),

    # -- semantic plane (v2 Phase 4) ----------------------------------------
    # Ordered cheapest-first: profile/policy/prompt checks are sub-second;
    # the pipeline suites ingest fixture documents and run the mock provider.
    Script("verify_semantic_profiles", CORE,
           "provider profiles: atomic machine-local store, secret-free "
           "persistence, endpoint policy, SDK isolation"),
    Script("verify_semantic_policy", CORE,
           "workspace policy: fail-closed defaults, classification gate, "
           "pre-content rejection, payload allow-list"),
    Script("verify_semantic_transport", CORE,
           "audited semantic egress: host pinning, HTTPS, redirect "
           "containment, redaction, no-bypass repository scan"),
    Script("verify_semantic_providers", CORE,
           "provider adapters via stub transports: structured output, "
           "error taxonomy, retries, honest degradation"),
    Script("verify_semantic_prompts", CORE,
           "prompt-injection boundary: untrusted packet separation, no "
           "tools, no CoT, fixed schemas, versioned prompts"),
    Script("verify_semantic_verifier", CORE,
           "evidence verifier: ownership, inclusion, quote verification, "
           "locally derived confidence"),
    Script("verify_semantic_analysis", CORE,
           "analysis pipeline: zero-call planning/ingestion, checkpointed "
           "targets, failure isolation, resume, bounded pairs"),
    Script("verify_semantic_cache", CORE,
           "semantic cache: exact hits call no provider; key sensitivity; "
           "--force; cache-disabled policy"),
    Script("verify_semantic_budget", CORE,
           "budgets: every cap, honest partial runs, preserved work, "
           "null costs"),
    Script("verify_semantic_staleness", CORE,
           "staleness: revision changes stale candidates transitively; "
           "review preserved; startup backstop"),
    Script("verify_project_lenses", CORE,
           "Project Lenses: Template projection, organization files, safe "
           "patterns, bounded induction, deterministic validation, "
           "approval lifecycle"),
    Script("verify_semantic_cli", CORE,
           "semantic CLI contract: JSON, exit codes, secret-free output, "
           "provider/semantic/lens commands"),
    Script("verify_semantic_adapters", CORE,
           "additive REST + 7 read-only MCP tools + the 26-tool "
           "compatibility gate + artifact/bridge/Ask invariants"),

    # -- template / docs pipeline -------------------------------------------
    Script("verify_templates", CORE, "template profile selection and validation"),
    Script("verify_facets", CORE, "template facets, roles and projections"),
    Script("verify_guide", CORE, "generated guide sections and citations"),

    # -- app-level behaviour -------------------------------------------------
    Script("verify", CORE, "end-to-end ingest, search and job lifecycle"),
    Script("verify_fixes", CORE, "regression fixes (batch 1)"),
    Script("verify_fixes2", CORE, "regression fixes (batch 2)"),
    Script("verify_resources", CORE, "ingest RAM governor"),
    Script("verify_ask", CORE, "Ask gating when no model is ready"),
    Script("verify_ask2", CORE, "Ask history, cancellation and case saving"),
    Script("verify_async_delete", CORE, "asynchronous project delete"),
    Script("verify_delete_race", CORE,
           "delete completes: janitor/cleanup race, in-flight drain guard"),
    Script("verify_source_link", CORE,
           "GitHub link parsing + egress policy (no live network call)"),
    Script("verify_modelserver", CORE,
           "model-server attach/start logic against a local stub server"),

    # -- excluded from the CI gate, with a reason ---------------------------
    Script("verify_delete_responsive", LOCAL,
           "delete stays responsive under load",
           reason="asserts sub-2-second API latency while a delete drains; a "
                  "shared CI runner's scheduling jitter makes that flaky, and a "
                  "flaky gate is worse than an honest exclusion. Run it locally "
                  "before releasing a change to the delete path."),
]

BY_NAME = {s.name: s for s in SUITE}


def base_env() -> Dict[str, str]:
    """A deterministic, offline environment.

    Embeddings fall back to the in-process hashing embedder, enrichment and
    source-link egress are off, and the GPU is never touched. No paid API and no
    public LLM endpoint is reachable from a run configured this way.
    """
    env = dict(os.environ)
    env.update({
        "OPENMIND_EMBED_OFFLINE": "1",
        "OPENMIND_EMBED_DEVICE": "cpu",
        "OPENMIND_INGEST_FREE_GPU": "0",
        "OPENMIND_ENRICH_EGRESS": "0",
        "OPENMIND_SOURCELINK_EGRESS": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    # Never inherit the developer's live data dir; tests/_isolate.py also
    # guards this, but the runner must not depend on the script doing so.
    env.pop("OPENMIND_DATA_DIR", None)
    env.pop("OPENMIND_MACHINE_DIR", None)
    return env


def run_script(script: Script, verbose: bool = False) -> Dict[str, Any]:
    """Run one script in an isolated data directory. Never raises."""
    if not script.path.is_file():
        return {"name": script.name, "tier": script.tier, "status": "skipped",
                "reason": f"script not found: {script.path}", "seconds": 0.0}

    env = base_env()
    env["OPENMIND_DATA_DIR"] = tempfile.mkdtemp(prefix=f"om_acc_{script.name}_")
    env["OPENMIND_MACHINE_DIR"] = tempfile.mkdtemp(prefix=f"om_mach_{script.name}_")

    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script.path)],
            cwd=str(REPO_ROOT), env=env, timeout=script.timeout,
            capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return {"name": script.name, "tier": script.tier, "status": "failed",
                "reason": f"timed out after {script.timeout}s",
                "seconds": round(time.time() - started, 1)}
    except OSError as exc:
        return {"name": script.name, "tier": script.tier, "status": "skipped",
                "reason": f"could not start: {exc}",
                "seconds": round(time.time() - started, 1)}

    elapsed = round(time.time() - started, 1)
    # The verdict line ("N passed, M failed") is on STDOUT. stderr carries
    # library warnings, so summarizing the combined tail would report a
    # third-party warning where the result belongs.
    out_lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    err_lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
    result = {
        "name": script.name,
        "tier": script.tier,
        "status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "seconds": elapsed,
        "summary": (out_lines[-1] if out_lines
                    else err_lines[-1] if err_lines else "").strip(),
    }
    if proc.returncode != 0 or verbose:
        # Keep enough output to diagnose without dumping a whole ingest log.
        # On failure the FAIL lines are what matter, so show those first.
        failures = [ln for ln in out_lines if ln.startswith("FAIL")]
        result["output"] = "\n".join(failures[-20:] + out_lines[-20:]
                                     + err_lines[-10:])
    return result


def check_manifest() -> List[str]:
    """Every ``tests/verify_*.py`` must be in :data:`SUITE`.

    Without this, adding a test file and forgetting to register it would mean
    the suite silently stops covering it while still reporting success.
    """
    on_disk = {p.stem for p in TESTS_DIR.glob("verify*.py")}
    return sorted(on_disk - set(BY_NAME))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_acceptance.py",
        description="Run the OpenMind acceptance suite in isolated, offline "
                    "environments.")
    parser.add_argument("--tier", choices=[CORE, LOCAL], default=CORE,
                        help="which tier to run (default: core, the CI gate)")
    parser.add_argument("--all", action="store_true",
                        help="run every tier")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="run only this script (repeatable)")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop at the first failure")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print one machine-readable JSON object on stdout")
    parser.add_argument("--list", action="store_true", dest="list_only",
                        help="list the suite and exit")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="include output for passing scripts too")
    args = parser.parse_args(argv)

    log = (lambda m: print(m, file=sys.stderr, flush=True)) if args.as_json \
        else (lambda m: print(m, flush=True))

    unregistered = check_manifest()
    if unregistered:
        message = ("acceptance manifest is out of date; these scripts exist but "
                   "are not listed in SUITE: " + ", ".join(unregistered))
        if args.as_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            log("error: " + message)
            log("Add them to SUITE in scripts/run_acceptance.py "
                "(with a tier and, for 'local', a reason).")
        return 2

    if args.list_only:
        for script in SUITE:
            log(f"{script.tier:6} {script.name:26} {script.summary}")
            if script.reason:
                log(f"{'':6} {'':26} excluded from CI: {script.reason}")
        return 0

    if args.only:
        unknown = [n for n in args.only if n not in BY_NAME]
        if unknown:
            log(f"error: unknown script(s): {', '.join(unknown)}")
            return 2
        selected = [BY_NAME[n] for n in args.only]
    elif args.all:
        selected = list(SUITE)
    else:
        selected = [s for s in SUITE if s.tier == args.tier]

    log(f"Running {len(selected)} acceptance script(s) "
        f"[{'all tiers' if args.all else args.tier}] with "
        f"Python {sys.version.split()[0]}")
    log("")

    results: List[Dict[str, Any]] = []
    for script in selected:
        log(f"  {script.name:26} ... ")
        result = run_script(script, verbose=args.verbose)
        results.append(result)
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[result["status"]]
        log(f"  {script.name:26} {mark}  {result['seconds']:>6.1f}s  "
            f"{result.get('summary', result.get('reason', ''))}")
        if result["status"] == "failed" and result.get("output"):
            for line in result["output"].splitlines():
                log(f"        | {line}")
        if args.fail_fast and result["status"] != "passed":
            log("\nstopping at first failure (--fail-fast)")
            break

    passed = [r for r in results if r["status"] == "passed"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    # A skipped CORE script is a failure: the gate must never report success
    # for coverage it did not actually run.
    skipped_core = [r for r in skipped if r["tier"] == CORE]
    not_run = [s.name for s in selected
               if s.name not in {r["name"] for r in results}]

    ok = not failed and not skipped_core and not not_run

    log("")
    log(f"{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped"
        + (f", {len(not_run)} not run" if not_run else ""))
    if failed:
        log("failed: " + ", ".join(r["name"] for r in failed))
    if skipped_core:
        log("SKIPPED CORE SCRIPTS (counted as failure): "
            + ", ".join(f"{r['name']} ({r.get('reason', '')})" for r in skipped_core))
    if skipped and not skipped_core:
        log("skipped (non-core): " + ", ".join(r["name"] for r in skipped))

    if args.as_json:
        print(json.dumps({
            "ok": ok,
            "tier": "all" if args.all else args.tier,
            "counts": {"passed": len(passed), "failed": len(failed),
                       "skipped": len(skipped), "not_run": len(not_run)},
            "results": results,
        }, indent=2))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
