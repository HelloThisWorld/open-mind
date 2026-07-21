"""Semantic CLI contract — one JSON object on stdout, stderr diagnostics,
stable exit codes, no ANSI, no secrets, and the full provider/semantic/lens
command surface driven end-to-end in-process.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    check, finish, find_evidence, make_workspace, mock_profile,
    requirement_response)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind import cli  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402


def run_cli(*argv):
    """Run the CLI in-process; returns (exit_code, stdout, stderr).
    argparse usage errors raise SystemExit(2) — captured, not fatal."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def one_json(text):
    return json.loads(text)


runtime = get_runtime()
pid = make_workspace(runtime, "cli-ws")
evidence_id = find_evidence(pid, "shall respond to a status query")
mock_profile("mock-cli", responses={
    "requirement-extraction": requirement_response(evidence_id)})

os.environ["OM_CLI_SECRET"] = "sk-cli-secret-value"

# ---------------------------------------------------------------------------
# 1. provider commands
# ---------------------------------------------------------------------------
code, out, err = run_cli("provider", "configure", "--name", "openai-x",
                         "--kind", "openai", "--api-key-env",
                         "OM_CLI_SECRET", "--standard-model", "cfg-model",
                         "--max-classification", "internal", "--json")
payload = one_json(out)
check("provider configure emits one JSON object, ok=true",
      code == 0 and payload["ok"] is True)
check("configure output never contains the key value",
      "sk-cli-secret-value" not in out and "sk-cli-secret-value" not in err)
check("no ANSI escapes in JSON mode", "\x1b[" not in out)

code, out, _ = run_cli("provider", "list", "--json")
payload = one_json(out)
check("provider list shows both profiles",
      {p["name"] for p in payload["providers"]}
      >= {"openai-x", "mock-cli"})

code, out, _ = run_cli("provider", "show", "--name", "openai-x", "--json")
check("provider show works and reports the pinned host",
      code == 0 and one_json(out)["provider"]["expected_host"]
      == "api.openai.com")

code, out, _ = run_cli("provider", "validate", "--name", "openai-x",
                       "--json")
payload = one_json(out)
check("provider validate performs zero network calls",
      code == 0 and payload["network_calls_made"] == 0)

code, out, _ = run_cli("provider", "test", "--name", "mock-cli", "--json")
check("provider test WITHOUT --live is configuration-only",
      code == 0 and one_json(out)["live"] is False)
code, out, _ = run_cli("provider", "test", "--name", "mock-cli", "--live",
                       "--json")
check("provider test --live on the mock kind reports reachable without "
      "network", code == 0 and one_json(out)["reachable"] is True)

code, out, _ = run_cli("provider", "remove", "--name", "nope", "--json")
payload = one_json(out)
check("removing an unknown profile is a clean domain failure",
      code == 2 and payload["ok"] is False)

# a raw API key flag must not exist
code, out, err = run_cli("provider", "configure", "--name", "x",
                         "--api-key", "sk-live-nope", "--json")
check("there is NO --api-key flag (raw keys are refused by the parser)",
      code == 2)

# ---------------------------------------------------------------------------
# 2. semantic policy + plan + analyze + inspection
# ---------------------------------------------------------------------------
code, out, _ = run_cli("semantic", "policy", "show", "--workspace", pid,
                       "--json")
payload = one_json(out)
check("policy show reports the fail-closed defaults",
      code == 0 and payload["policy"]["data_classification"] == "restricted"
      and payload["policy"]["allow_remote"] is False)

code, out, _ = run_cli("semantic", "policy", "set", "--workspace", pid,
                       "--provider", "mock-cli", "--max-requests", "50",
                       "--json")
payload = one_json(out)
check("policy set selects the provider and stores budgets",
      code == 0 and payload["policy"]["provider_profile"] == "mock-cli"
      and payload["policy"]["budgets"]["max_requests_per_run"] == 50)

code, out, err = run_cli("semantic", "plan", "--workspace", pid,
                         "--tasks", "requirement-extraction", "--json")
payload = one_json(out)
check("semantic plan emits exactly one JSON object",
      code == 0 and payload["plan"]["target_count"] > 0)
check("plan stdout parses as a single object (nothing extra printed)",
      out.strip().startswith("{") and out.strip().endswith("}"))

code, out, _ = run_cli("semantic", "analyze", "--workspace", pid,
                       "--tasks", "requirement-extraction", "--wait",
                       "--timeout", "180", "--json")
payload = one_json(out)
check("semantic analyze --wait completes",
      code == 0 and payload["run"]["status"] == "done")
run_id = payload["run_id"]

code, out, _ = run_cli("semantic", "runs", "--workspace", pid, "--json")
check("semantic runs lists the run",
      any(r["id"] == run_id for r in one_json(out)["runs"]))
code, out, _ = run_cli("semantic", "show", "--workspace", pid, "--run",
                       run_id, "--json")
check("semantic show reports targets and usage",
      one_json(out)["run"]["targets"] and "usage" in one_json(out)["run"])

code, out, _ = run_cli("semantic", "candidates", "--workspace", pid,
                       "--type", "requirement", "--review-status",
                       "unreviewed", "--lifecycle-status", "active",
                       "--json")
payload = one_json(out)
check("semantic candidates filters work", payload["count"] >= 1)
candidate_id = payload["candidates"][0]["id"]

code, out, _ = run_cli("semantic", "candidate", "--workspace", pid,
                       "--candidate", candidate_id, "--json")
check("semantic candidate shows evidence quotes",
      one_json(out)["candidate"]["evidence"])

code, out, _ = run_cli("semantic", "review", "--workspace", pid,
                       "--candidate", candidate_id, "--decision", "confirm",
                       "--note", "Reviewed against the cited spec.",
                       "--json")
check("semantic review confirm works",
      one_json(out)["candidate"]["review_status"] == "confirmed")

code, out, _ = run_cli("semantic", "usage", "--workspace", pid, "--run",
                       run_id, "--json")
payload = one_json(out)
check("semantic usage reports the ledger with honest nulls",
      code == 0 and payload["totals"]["requests"] >= 1
      and "estimated_cost" in payload["totals"])

code, out, _ = run_cli("semantic", "relations", "--workspace", pid,
                       "--json")
check("semantic relations lists (possibly empty) relation candidates",
      code == 0 and "relations" in one_json(out))
code, out, _ = run_cli("semantic", "conflicts", "--workspace", pid,
                       "--json")
check("semantic conflicts lists (possibly empty) conflict candidates",
      code == 0 and "conflicts" in one_json(out))

# error contract: unknown workspace -> exit 1, parseable error object
code, out, _ = run_cli("semantic", "policy", "show", "--workspace",
                       "p_missing", "--json")
payload = one_json(out)
check("unknown workspace: exit 1 + machine-readable error",
      code == 1 and payload["ok"] is False
      and payload["error"]["code"] == "workspace_not_found")
code, out, _ = run_cli("semantic", "plan", "--workspace", pid, "--tasks",
                       "mind-reading", "--json")
check("unknown task: exit 2 (invalid usage)", code == 2)

# ---------------------------------------------------------------------------
# 3. lens commands
# ---------------------------------------------------------------------------
code, out, _ = run_cli("lens", "list", "--workspace", pid, "--json")
payload = one_json(out)
check("lens list shows the built-in projections",
      any(l["source"] == "builtin" for l in payload["lenses"]))

code, out, _ = run_cli("lens", "induce", "plan", "--workspace", pid,
                       "--provider", "mock-cli", "--json")
payload = one_json(out)
check("lens induce plan reports samples + zero provider calls",
      code == 0 and payload["plan"]["provider_calls_made"] == 0)

code, out, _ = run_cli("lens", "show", "--workspace", pid, "--lens",
                       "builtin:generic", "--json")
check("lens show resolves a virtual built-in lens",
      code == 0 and one_json(out)["lens"]["name"] == "generic")

code, out, _ = run_cli("lens", "activate", "--workspace", pid, "--lens",
                       "builtin:generic", "--json")
payload = one_json(out)
check("lens activate materializes and activates the built-in",
      code == 0 and payload["lens"]["status"] == "active")
lens_id = payload["lens"]["id"]
code, out, _ = run_cli("lens", "validate", "--workspace", pid, "--lens",
                       lens_id, "--json")
check("lens validate returns the deterministic verdict", code in (0, 1))
code, out, _ = run_cli("lens", "deactivate", "--workspace", pid, "--lens",
                       lens_id, "--json")
check("lens deactivate works", code == 0
      and one_json(out)["lens"]["status"] != "active")
code, out, _ = run_cli("lens", "export", "--workspace", pid, "--lens",
                       lens_id, "--json")
check("lens export returns the definition document",
      code == 0 and one_json(out)["definition"]["schemaVersion"])

# ---------------------------------------------------------------------------
# 4. Secrets never reach any CLI output
# ---------------------------------------------------------------------------
for args in (("provider", "list", "--json"),
             ("provider", "show", "--name", "openai-x", "--json"),
             ("semantic", "policy", "show", "--workspace", pid, "--json")):
    _, out, err = run_cli(*args)
    if "sk-cli-secret-value" in out or "sk-cli-secret-value" in err:
        check(f"no secret in output of {' '.join(args)}", False)
        break
else:
    check("no command output ever contains the credential value", True)

finish()
