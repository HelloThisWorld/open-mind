"""CLI contract — JSON output, exit codes, stream separation, and every command.

Each case runs the CLI as a REAL subprocess. Calling ``cli.main()`` in-process
would test the parser but not the contract that actually matters: the exit code
the shell sees, and the guarantee that stdout carries JSON and nothing else.
"""
import json
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from openmind.version import RUNTIME_VERSION  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "fixtures", "sample-repo")

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc
          + (f"  [{extra}]" if extra and not cond else ""))


ENV = dict(os.environ)
ENV.update({
    "OPENMIND_DATA_DIR": tempfile.mkdtemp(prefix="om_cli_"),
    "OPENMIND_MACHINE_DIR": tempfile.mkdtemp(prefix="om_cli_machine_"),
    "OPENMIND_EMBED_OFFLINE": "1",
    "OPENMIND_EMBED_DEVICE": "cpu",
    "OPENMIND_INGEST_FREE_GPU": "0",
    "OPENMIND_ENRICH_EGRESS": "0",
    "OPENMIND_SOURCELINK_EGRESS": "0",
    "PYTHONIOENCODING": "utf-8",
})


def run(*args, timeout=900):
    """Run the CLI and return (exit_code, stdout, stderr)."""
    proc = subprocess.run([sys.executable, "-m", "openmind.cli", *args],
                          cwd=REPO, env=ENV, capture_output=True, text=True,
                          errors="replace", timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def as_json(stdout):
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Global behaviour
# ---------------------------------------------------------------------------
code, out, err = run("--version")
check("--version exits 0", code == 0)
check("--version prints the runtime version", RUNTIME_VERSION in out, out.strip())

code, out, err = run("--help")
check("--help exits 0", code == 0)
for command in ("doctor", "init", "add", "ingest", "status", "export", "mcp", "serve"):
    check(f"--help documents the '{command}' command", command in out)
check("--help documents the exit codes", "Exit codes" in out)

code, out, err = run()
check("no command exits 2 (invalid usage)", code == 2, str(code))
check("no command prints usage on stderr, not stdout",
      "usage" in err.lower() and out.strip() == "")

code, out, err = run("mcp")
check("a command group with no subcommand exits 2", code == 2, str(code))

code, out, err = run("status", "--json")
check("a missing required argument exits 2", code == 2, str(code))

# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
code, out, err = run("doctor", "--json")
payload = as_json(out)
check("doctor --json exits 0 on a healthy runtime", code == 0, f"{code}: {err[-200:]}")
check("doctor --json prints exactly one JSON object on stdout", payload is not None)
check("doctor --json reports ok", payload and payload.get("ok") is True)
check("doctor --json reports the runtime version",
      payload and payload.get("version") == RUNTIME_VERSION)
check("doctor --json lists checks", payload and len(payload.get("checks", [])) >= 8)
check("doctor --json includes runtime info",
      payload and "schema_version" in (payload.get("runtime") or {}))
check("doctor --json emits no ANSI escape codes", "\x1b[" not in out)

names = {c["name"] for c in (payload or {}).get("checks", [])}
for required in ("data_dir", "database", "migrations", "project_dirs",
                 "vectorstore", "embeddings", "mcp", "model_config",
                 "model_server", "runtime_version"):
    check(f"doctor checks '{required}'", required in names)

check("doctor does not fail because no local model is configured",
      code == 0 and any(c["name"] == "model_config" and c["status"] == "warn"
                        for c in (payload or {}).get("checks", [])))

code, human_out, err = run("doctor")
check("doctor without --json prints a human report on stdout",
      code == 0 and "OpenMind" in human_out)
check("the human doctor report is not JSON", as_json(human_out) is None)

# Global flags must work on BOTH sides of the subcommand. argparse `parents=`
# shares Action objects with every subparser, so a default on the sub-parser
# silently clobbers a flag given before the subcommand — regression-tested here
# in both orders for each flag.
code, quiet_out, quiet_err = run("--quiet", "doctor")
check("--quiet BEFORE the subcommand suppresses the human report",
      quiet_out.strip() == "", repr(quiet_out[:120]))
code, quiet_out, quiet_err = run("doctor", "--quiet")
check("--quiet AFTER the subcommand suppresses the human report",
      quiet_out.strip() == "", repr(quiet_out[:120]))
code, jout, _ = run("--json", "doctor")
check("--json BEFORE the subcommand still emits JSON", as_json(jout) is not None)
code, jout, _ = run("doctor", "--json")
check("--json AFTER the subcommand still emits JSON", as_json(jout) is not None)

# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
code, out, err = run("init", "--name", "cli demo", "--path", FIXTURE, "--json")
payload = as_json(out)
check("init --json exits 0", code == 0, f"{code}: {err[-300:]}")
check("init --json returns the workspace id",
      payload and str(payload.get("workspace_id", "")).startswith("p_"))
check("init --json reports ok", payload and payload["ok"] is True)
check("init registers the given path",
      payload and len(payload["workspace"]["paths"]) == 1)
check("init resolves the path to an absolute location",
      payload and os.path.isabs(payload["workspace"]["paths"][0]["path"]))
check("init does NOT ingest unless asked",
      payload and payload["workspace"]["state"] == "init")
check("init emits nothing but JSON on stdout", "\x1b[" not in out)

WS = payload["workspace_id"]

code, out, err = run("init", "--name", "bad path", "--path",
                     os.path.join(REPO, "no-such-directory"), "--json")
check("init with a non-existent path exits 2", code == 2, str(code))
check("the error is machine-readable",
      (as_json(out) or {}).get("error", {}).get("code") == "invalid_request")

code, out, err = run("init", "--name", "no path", "--json")
check("init without --path succeeds (a path can be added later)", code == 0)

# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
code, out, err = run("add", "--workspace", WS, "--path", FIXTURE,
                     "--exclude", "target", "--exclude", "build", "--json")
payload = as_json(out)
check("add --json exits 0", code == 0, f"{code}: {err[-200:]}")
check("add updates the existing path rather than duplicating it",
      payload and len(payload["paths"]) == 1)
check("add records the exclude set",
      payload and payload["paths"][0]["exclude"] == ["build", "target"])

code, out, err = run("add", "--workspace", "p_nope", "--path", FIXTURE, "--json")
check("add to an unknown workspace exits 1", code == 1, str(code))
check("the unknown-workspace error is machine-readable",
      (as_json(out) or {}).get("error", {}).get("code") == "workspace_not_found")

# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
code, out, err = run("ingest", "--workspace", "p_nope", "--json")
check("ingest on an unknown workspace exits 1", code == 1, str(code))

code, out, err = run("ingest", "--workspace", WS, "--wait", "--timeout", "600",
                     "--json")
payload = as_json(out)
check("ingest --wait --json exits 0", code == 0, f"{code}: {err[-400:]}")
check("ingest --wait reports completion",
      payload and payload.get("completed") is True and payload.get("status") == "done")
check("ingest --wait returns the persisted job id",
      payload and str(payload.get("job_id", "")).startswith("job_"))
check("ingest --wait reports progress counters",
      payload and payload.get("progress", {}).get("files_indexed", 0) > 0)
check("ingest --wait reports how long it took",
      payload and payload.get("waited_seconds") is not None)

# ---------------------------------------------------------------------------
# status
#
# Checked BEFORE the non-waiting ingest below: that one leaves a job in flight,
# which legitimately puts the workspace back into 'learning'.
# ---------------------------------------------------------------------------
code, out, err = run("status", "--workspace", WS, "--json")
payload = as_json(out)
check("status --json exits 0", code == 0, f"{code}: {err[-200:]}")
for key in ("workspace", "state", "paths", "template", "counts",
            "active_jobs", "recent_jobs", "schema_version", "version"):
    check(f"status --json reports '{key}'", payload and key in payload)
check("status reports the workspace reached 'ready'",
      payload and payload["state"] == "ready", str(payload and payload["state"]))
check("status reports indexed files after an ingest",
      payload and payload["counts"]["indexed_files"] > 0)
check("status reports code chunks after an ingest",
      payload and payload["counts"]["code_chunks"] > 0)
check("status reports the migration schema version",
      payload and payload["schema_version"] >= 2)
check("status reports the runtime version",
      payload and payload["version"] == RUNTIME_VERSION)
check("status lists the ingest job",
      payload and any(j["type"] == "ingest" for j in payload["recent_jobs"]))
check("status does not require a running FastAPI server", code == 0)

code, out, err = run("status", "--workspace", "p_nope", "--json")
check("status on an unknown workspace exits 1", code == 1, str(code))
payload = as_json(out)
check("a failing command still prints parseable JSON", payload is not None)
check("the failure payload sets ok=false", payload and payload["ok"] is False)
check("the failure payload carries a code and message",
      payload and payload["error"]["code"] == "workspace_not_found"
      and payload["error"]["message"])

code, human_out, err = run("status", "--workspace", "p_nope")
check("a human-mode failure prints to stderr, not stdout",
      human_out.strip() == "" and "error" in err.lower())

# --------------------------------------------------------------------------
# Cases that deliberately leave a job in flight — kept last, because they put
# the workspace back into 'learning' and would break the status checks above.
# --------------------------------------------------------------------------
code, out, err = run("ingest", "--workspace", WS, "--json")
payload = as_json(out)
check("ingest without --wait exits 0 immediately", code == 0)
check("ingest without --wait returns a job id and does not wait",
      payload and payload["waited"] is False and payload["job_id"])

# A --wait that times out must still print exactly ONE JSON object, report
# ok=false, exit 5, and hand back a pollable job id (the job is NOT cancelled).
code, out, err = run("ingest", "--workspace", WS, "--wait", "--timeout", "0.05",
                     "--json")
payload = as_json(out)
check("an ingest --wait timeout exits 5", code == 5, str(code))
check("a timed-out ingest prints exactly one JSON object (not two)",
      payload is not None and out.count('"ok"') == 1, repr(out[:200]))
check("a timed-out ingest reports ok=false", payload and payload["ok"] is False)
check("a timed-out ingest uses the timeout error code",
      payload and payload["error"]["code"] == "timeout")
check("a timed-out ingest still returns a pollable job id",
      payload and str(payload.get("job_id", "")).startswith("job_"))
check("the timeout message says the job is still running",
      payload and "still running" in payload["error"]["message"])

# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
out_dir = os.path.join(tempfile.mkdtemp(prefix="om_cli_export_"), ".openmind")
code, out, err = run("export", "--repo", FIXTURE, "--output", out_dir, "--json")
payload = as_json(out)
check("export --json exits 0", code == 0, f"{code}: {err[-300:]}")
check("export writes a manifest",
      os.path.isfile(os.path.join(out_dir, "manifest.json")))
check("export reports the frozen schema version",
      payload and payload["schemaVersion"] == "1.1.0")

manifest = json.load(open(os.path.join(out_dir, "manifest.json"), encoding="utf-8"))
check("the manifest keeps schema 1.1.0", manifest["schemaVersion"] == "1.1.0")
check("the manifest still names the generator 'open-mind'",
      manifest["generator"]["name"] == "open-mind")
check("the manifest reports the runtime version as the generator version",
      manifest["generator"]["version"] == RUNTIME_VERSION)
for artifact in ("glossary.json", "architecture.json", "flows.json",
                 "source-index.json", "metadata.json"):
    check(f"export wrote {artifact}",
          os.path.isfile(os.path.join(out_dir, artifact)))

code, out, err = run("export", "--repo", os.path.join(REPO, "nope"),
                     "--output", out_dir, "--json")
check("export with a missing repo exits 2", code == 2, str(code))

code, out, err = run("export", "--repo", FIXTURE, "--output", out_dir,
                     "--template", "spring-boot", "--no-template", "--json")
check("export rejects --template with --no-template (exit 2)", code == 2, str(code))

# Export must stay STANDALONE: no database, no vector store, no web app, and no
# heavy dependency. The dependency-free artifact-contract CI job runs the CLI
# exporter with nothing installed, so an eager import anywhere on this path
# would break it. Simulate the absent dependencies rather than trusting layering.
probe = subprocess.run(
    [sys.executable, "-c", '''
import builtins, os, tempfile
os.environ["OPENMIND_DATA_DIR"] = tempfile.mkdtemp()
os.environ["OPENMIND_MACHINE_DIR"] = tempfile.mkdtemp()
BLOCKED = {"chromadb", "fastapi", "uvicorn", "fastembed", "onnxruntime", "mcp",
           "httpx", "tree_sitter", "tree_sitter_java", "sqlglot", "psutil",
           "numpy", "pydantic", "yaml"}
_real = builtins.__import__
def guard(name, *a, **kw):
    if name.split(".")[0] in BLOCKED:
        raise ImportError("No module named %r (simulated)" % name.split(".")[0])
    return _real(name, *a, **kw)
builtins.__import__ = guard
from openmind.services.export_service import ExportService
summary = ExportService().export("./fixtures/sample-repo", tempfile.mkdtemp())
print(summary["schemaVersion"], summary["filesIndexed"])
'''],
    cwd=REPO, env=ENV, capture_output=True, text=True)
parts = probe.stdout.split()
check("artifact export runs with NO heavy dependencies installed "
      "(no chroma, numpy, fastapi, mcp)",
      probe.returncode == 0 and parts and parts[0] == "1.1.0",
      (probe.stdout + probe.stderr)[-400:])
check("the dependency-free export still indexes the fixture",
      len(parts) > 1 and int(parts[1]) > 0, probe.stdout)

# ---------------------------------------------------------------------------
# serve — argument validation only (never actually bind)
# ---------------------------------------------------------------------------
code, out, err = run("serve", "--host", "0.0.0.0", "--json")
check("serve refuses a non-loopback bind by default (exit 2)", code == 2, str(code))
check("the refusal explains the risk and the override",
      "--allow-non-loopback" in ((as_json(out) or {}).get("error", {})
                                 .get("message", "")))

code, out, err = run("serve", "--help")
check("serve documents the loopback default", code == 0 and "127.0.0.1" in out)

# ---------------------------------------------------------------------------
# mcp serve — the CLI must reach the SAME implementation, not a copy
# ---------------------------------------------------------------------------
code, out, err = run("mcp", "--help")
check("'mcp' exposes a serve subcommand", code == 0 and "serve" in out)

probe = subprocess.run(
    [sys.executable, "-c",
     "import openmind.cli as c, openmind.mcp_server as m;"
     "import inspect;"
     "src = inspect.getsource(c.cmd_mcp_serve);"
     "print('create_mcp_server' in src);"
     "print(len(m.TOOLS))"],
    cwd=REPO, env=ENV, capture_output=True, text=True)
lines = probe.stdout.split()
check("the CLI delegates to create_mcp_server rather than redefining tools",
      lines and lines[0] == "True", probe.stdout + probe.stderr)
check("the MCP tool set has all 9 tools", len(lines) > 1 and lines[1] == "9",
      probe.stdout)

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
