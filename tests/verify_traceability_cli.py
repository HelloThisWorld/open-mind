"""Trace/conflict CLI contract: one JSON object on stdout, diagnostics on
stderr, no ANSI in JSON mode, stable exit codes, bounded output, explicit
actor/note on writes, every pre-existing command still registered."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[")

runtime = get_runtime()
fx = make_fixture(runtime, "cli-fix")
pid = fx.pid
objects = fx.lifecycle()
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 2 seconds.")
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 5 seconds.")


def run(*argv, json_mode=True):
    cmd = [sys.executable, "-m", "openmind.cli", *argv]
    if json_mode:
        cmd.append("--json")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=REPO_ROOT, env=dict(os.environ), timeout=300)
    payload = None
    if json_mode and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    return proc, payload


# -- contract -----------------------------------------------------------------
proc, payload = run("trace", "policy", "list", "--workspace", pid)
check("policy list: exit 0 + one JSON object",
      proc.returncode == 0 and payload and payload["ok"])
check("stdout carries ONLY the JSON object",
      json.loads(proc.stdout) is not None
      and proc.stdout.strip().startswith("{"))
check("no ANSI in JSON mode",
      not ANSI.search(proc.stdout) and not ANSI.search(proc.stderr))

proc, payload = run("trace", "policy", "set", "--workspace", pid,
                    "--policy", "api-service", "--actor", "reviewer",
                    "--note", "Use the API lifecycle.")
check("policy set works with actor+note",
      proc.returncode == 0 and payload["policy_name"] == "api-service")
proc, _ = run("trace", "policy", "set", "--workspace", pid,
              "--policy", "api-service", "--note", "missing actor")
check("policy set without --actor is usage error (exit 2)",
      proc.returncode == 2)

proc, payload = run("trace", "refresh", "--workspace", pid, "--wait")
check("trace refresh --wait works",
      proc.returncode == 0 and payload["run"]["status"] == "done")
proc, payload = run("trace", "refresh", "--workspace", pid, "--wait")
check("unchanged refresh reports the no-op",
      proc.returncode == 0 and payload.get("no_op") is True)
proc, payload = run("trace", "refresh-plan", "--workspace", pid)
check("refresh-plan reports the mode",
      proc.returncode == 0 and payload["mode"] == "no-op")

proc, payload = run("trace", "requirement", "--workspace", pid,
                    "--requirement", objects["requirement"]["id"])
check("trace requirement returns paths + gaps",
      proc.returncode == 0 and payload["paths"] and "gaps" in payload)
proc, payload = run("trace", "code", "--workspace", pid,
                    "--entity", objects["code"]["id"])
check("trace code resolves the requirement",
      proc.returncode == 0
      and any(r["entity_id"] == objects["requirement"]["id"]
              for r in payload["requirements"]))
proc, payload = run("trace", "test", "--workspace", pid,
                    "--entity", objects["test"]["id"])
check("trace test resolves requirement + implementation",
      proc.returncode == 0 and payload["requirements"]
      and payload["implementation_targets"])
proc, payload = run("trace", "coverage", "--workspace", pid)
check("trace coverage returns the snapshot",
      proc.returncode == 0 and payload["snapshot"])
proc, payload = run("trace", "runs", "--workspace", pid)
run_id = payload["runs"][0]["id"]
proc, payload = run("trace", "run", "--workspace", pid, "--run", run_id)
check("trace run shows one run",
      proc.returncode == 0 and payload["run"]["id"] == run_id)

from openmind.traceability import store as trace_store  # noqa: E402
a_path = trace_store.list_paths(pid, limit=1)[0]
proc, payload = run("trace", "path", "--workspace", pid,
                    "--trace", a_path["id"])
check("trace path shows ordered steps",
      proc.returncode == 0
      and payload["path"]["steps"][0]["ordinal"] == 1)
proc, payload = run("trace", "path", "--workspace", pid,
                    "--trace", "tr_missing")
check("unknown trace path: typed error, exit 1",
      proc.returncode == 1 and payload["ok"] is False
      and payload["error"]["code"] == "trace_path_not_found")

proc, payload = run("trace", "gaps", "--workspace", pid, "--limit", "5")
check("trace gaps bounded", proc.returncode == 0
      and len(payload["gaps"]) <= 5)
proc, payload = run("trace", "orphans-tests", "--workspace", pid)
check("orphan tests query works", proc.returncode == 0
      and "orphans" in payload)

# conflict commands
proc, payload = run("conflict", "scan", "--workspace", pid, "--wait",
                    "--actor", "scanner")
check("conflict scan works", proc.returncode == 0
      and payload["status"] == "done" and payload["created"])
conflict_id = payload["created"][0]
proc, payload = run("conflict", "list", "--workspace", pid,
                    "--status", "open")
check("conflict list works", proc.returncode == 0
      and payload["count"] >= 1)
proc, payload = run("conflict", "show", "--workspace", pid,
                    "--conflict", conflict_id)
check("conflict show carries objects/evidence/decisions",
      proc.returncode == 0 and payload["conflict"]["evidence"]
      and payload["conflict"]["objects"])
proc, payload = run("conflict", "review", "--workspace", pid,
                    "--conflict", conflict_id, "--actor", "reviewer",
                    "--note", "under review now")
check("conflict review works", proc.returncode == 0
      and payload["conflict"]["status"] == "under-review")
proc, payload = run("conflict", "resolve", "--workspace", pid,
                    "--conflict", conflict_id, "--resolution",
                    "left-correct", "--evidence", fx.evidence_id,
                    "--actor", "reviewer", "--note", "left value correct")
check("conflict resolve works with evidence", proc.returncode == 0
      and payload["conflict"]["status"] == "resolved")
proc, payload = run("conflict", "resolve", "--workspace", pid,
                    "--conflict", conflict_id, "--resolution",
                    "left-correct", "--evidence", fx.evidence_id,
                    "--actor", "reviewer", "--note", "again")
check("illegal transition is a typed conflict error (exit 1)",
      proc.returncode == 1 and payload["error"]["code"]
      == "conflict_state_invalid")

# unknown workspace: typed domain error
proc, payload = run("trace", "coverage", "--workspace", "p_missing")
check("unknown workspace exits 1 with a typed error",
      proc.returncode == 1 and payload["ok"] is False)

# human mode: JSON never on stdout... stdout carries human lines instead
proc, _ = run("trace", "coverage", "--workspace", pid, json_mode=False)
check("human mode prints without ANSI",
      proc.returncode == 0 and not ANSI.search(proc.stdout))

# -- every pre-existing command group still registered ------------------------
proc_help = subprocess.run(
    [sys.executable, "-m", "openmind.cli", "--help"], capture_output=True,
    text=True, cwd=REPO_ROOT, env=dict(os.environ), timeout=120)
help_text = proc_help.stdout + proc_help.stderr
for command in ("doctor", "init", "add", "ingest", "status", "export",
                "mcp", "asset", "document", "knowledge", "provider",
                "semantic", "lens", "graph", "promotion", "entity",
                "claim", "relation", "bundle", "serve", "trace",
                "conflict"):
    check(f"CLI still registers `{command}`", command in help_text)

finish()
