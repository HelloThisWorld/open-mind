"""Knowledge-graph CLI contract — one JSON object on stdout, stderr
diagnostics, stable exit codes, no ANSI, and the graph/promotion/entity/
claim/relation/knowledge-history/bundle command surface end-to-end."""
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
from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_confirmed_candidate,
                                make_minimal_workspace)

from openmind import cli  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402


def run_cli(*argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


runtime = get_runtime()
pid = make_minimal_workspace(runtime, "kg-cli")
evidence_id = find_evidence(pid, "REQ-NC-017")
candidate_id = make_confirmed_candidate(runtime, pid)

# -- graph seed / stats / sync ----------------------------------------------
code, out, err = run_cli("graph", "seed", "plan", "--workspace", pid,
                         "--json")
payload = json.loads(out)
check("graph seed plan emits one JSON object", code == 0 and payload["ok"])
check("no ANSI escapes in JSON mode", "\x1b[" not in out)

code, out, _ = run_cli("graph", "seed", "--workspace", pid, "--json")
check("graph seed succeeds", code == 0 and json.loads(out)["ok"])

code, out, _ = run_cli("graph", "sync", "--workspace", pid, "--json")
check("unchanged graph sync reports noop",
      code == 0 and json.loads(out)["action"] == "noop")

code, out, _ = run_cli("graph", "stats", "--workspace", pid, "--json")
stats = json.loads(out)
check("graph stats reports the knowledge revision",
      code == 0 and "knowledge_revision" in stats)

code, out, _ = run_cli("graph", "reconcile", "--workspace", pid, "--json")
check("graph reconcile runs", code == 0 and json.loads(out)["ok"])

# -- promotion ---------------------------------------------------------------
code, out, _ = run_cli("promotion", "plan", "--workspace", pid,
                       "--candidate", candidate_id, "--json")
plan = json.loads(out)["plan"]
check("promotion plan reports eligibility", code == 0 and plan["eligible"])

code, out, _ = run_cli("promotion", "promote", "--workspace", pid,
                       "--candidate", candidate_id, "--actor", "cli-rev",
                       "--note", "approved via cli", "--json")
promoted = json.loads(out)
check("promotion promote succeeds with entity + claim",
      code == 0 and promoted["status"] == "promoted"
      and promoted["entity"]["canonical_key"] == "requirement:REQ-NC-017")
entity_id = promoted["entity"]["id"]

code, out, _ = run_cli("promotion", "promote", "--workspace", pid,
                       "--candidate", candidate_id, "--actor", "cli-rev",
                       "--note", "again", "--json")
check("repeated promotion is idempotent over the CLI",
      code == 0 and json.loads(out)["status"] == "already-promoted")

# actor/note are required flags on promote
code, out, err = run_cli("promotion", "promote", "--workspace", pid,
                         "--candidate", candidate_id, "--json")
check("promote without --actor/--note is a usage error (exit 2)",
      code == 2)

# -- entity / claim / relation ----------------------------------------------
code, out, _ = run_cli("entity", "create", "--workspace", pid, "--type",
                       "business-rule", "--key", "business-rule:BR-CLI",
                       "--name", "BR-CLI", "--evidence", evidence_id,
                       "--actor", "cli", "--note", "created", "--json")
created = json.loads(out)
check("entity create over the CLI", code == 0
      and created["entity"]["origin"] == "manual")
second_id = created["entity"]["id"]

code, out, _ = run_cli("entity", "list", "--workspace", pid, "--json")
listing = json.loads(out)
check("entity list is bounded JSON with totals",
      code == 0 and listing["count"] >= 2 and "total" in listing)

code, out, _ = run_cli("entity", "show", "--workspace", pid, "--entity",
                       entity_id, "--json")
check("entity show includes aliases/bindings/claims",
      code == 0 and "aliases" in json.loads(out)["entity"])

code, out, _ = run_cli("entity", "alias-add", "--workspace", pid,
                       "--entity", second_id, "--alias", "cli-alias",
                       "--type", "name", "--actor", "cli", "--note", "a",
                       "--json")
check("entity alias-add works", code == 0)

code, out, _ = run_cli("claim", "create", "--workspace", pid, "--entity",
                       second_id, "--type", "normative-statement",
                       "--statement", "BR-CLI governs the retry budget.",
                       "--evidence", evidence_id, "--actor", "cli",
                       "--note", "claim", "--json")
claim = json.loads(out)["claim"]
check("claim create over the CLI", code == 0
      and claim["claim_type"] == "normative-statement")

code, out, _ = run_cli("claim", "list", "--workspace", pid, "--json")
check("claim list works", code == 0 and json.loads(out)["count"] >= 1)

code, out, _ = run_cli("relation", "create", "--workspace", pid,
                       "--source", second_id, "--target", entity_id,
                       "--type", "refines", "--state", "confirmed",
                       "--evidence", evidence_id, "--actor", "cli",
                       "--note", "rel", "--json")
relation = json.loads(out)["relation"]
check("relation create over the CLI", code == 0
      and relation["relation_state"] == "confirmed")

code, out, _ = run_cli("relation", "reject", "--workspace", pid,
                       "--relation", relation["id"], "--actor", "cli",
                       "--note", "not real", "--json")
check("relation reject works", code == 0)
code, out, _ = run_cli("relation", "restore", "--workspace", pid,
                       "--relation", relation["id"], "--actor", "cli",
                       "--note", "was real", "--json")
check("relation restore returns the prior state",
      code == 0 and json.loads(out)["relation_state"] == "confirmed")

code, out, _ = run_cli("entity", "authority", "--workspace", pid,
                       "--entity", entity_id, "--status", "authoritative",
                       "--actor", "lead", "--note", "board", "--json")
check("entity authority marking works",
      code == 0 and json.loads(out)["authority_status"] == "authoritative")

# -- graph queries ------------------------------------------------------------
code, out, _ = run_cli("graph", "search", "--workspace", pid, "--query",
                       "REQ-NC-017", "--json")
result = json.loads(out)
check("graph search returns entity hits",
      code == 0 and result["entities"]
      and result["entities"][0]["canonical_key"]
      == "requirement:REQ-NC-017")

code, out, _ = run_cli("graph", "node", "--workspace", pid, "--node",
                       entity_id, "--json")
check("graph node lookup works",
      code == 0 and json.loads(out)["node"]["nodeKind"] == "entity")

code, out, _ = run_cli("graph", "expand", "--workspace", pid, "--node",
                       entity_id, "--depth", "2", "--json")
check("graph expand is bounded JSON",
      code == 0 and "truncated" in json.loads(out))

code, out, _ = run_cli("graph", "path", "--workspace", pid, "--from",
                       second_id, "--to", entity_id, "--json")
check("graph path finds the created relation",
      code == 0 and json.loads(out)["outcome"] == "found")

# -- knowledge history --------------------------------------------------------
code, out, _ = run_cli("knowledge", "revisions", "--workspace", pid,
                       "--json")
revisions = json.loads(out)
check("knowledge revisions lists the ledger",
      code == 0 and revisions["revisions"])
number = revisions["revisions"][-1]["revision_number"]
code, out, _ = run_cli("knowledge", "revision", "--workspace", pid,
                       "--number", str(number), "--json")
check("knowledge revision shows one entry with decisions",
      code == 0 and "decisions" in json.loads(out)["revision"])
code, out, _ = run_cli("knowledge", "decisions", "--workspace", pid,
                       "--json")
check("knowledge decisions lists the audit",
      code == 0 and json.loads(out)["decisions"])
code, out, _ = run_cli("knowledge", "search", "--workspace", pid,
                       "--query", "REQ-NC-017", "--json")
check("the Phase 3 knowledge search command is untouched",
      code == 0 and "code" in json.loads(out) or "documents"
      in json.loads(out))

# -- bundle -------------------------------------------------------------------
out_dir = os.path.join(tempfile.mkdtemp(prefix="om_cli_bundle_"), "b")
code, out, _ = run_cli("bundle", "export", "--workspace", pid, "--output",
                       out_dir, "--current-only", "--generated-at",
                       "2026-07-22T00:00:00", "--json")
manifest = json.loads(out)["manifest"]
check("bundle export over the CLI",
      code == 0 and manifest["bundleSchemaVersion"] == "2.0.0-draft.2")
from openmind.bundle_verify import main as verify_main  # noqa: E402
stdout = io.StringIO()
with contextlib.redirect_stdout(stdout), \
        contextlib.redirect_stderr(io.StringIO()):
    verify_code = verify_main([out_dir])
check("bundle_verify accepts the CLI export", verify_code == 0)

# -- error contract -----------------------------------------------------------
code, out, _ = run_cli("graph", "stats", "--workspace", "p_missing",
                       "--json")
payload = json.loads(out)
check("unknown workspace: exit 1 with a machine-readable error",
      code == 1 and payload["ok"] is False
      and payload["error"]["code"] == "workspace_not_found")

code, out, _ = run_cli("entity", "create", "--workspace", pid, "--type",
                       "nonsense-type", "--key", "x:y", "--name", "x",
                       "--evidence", evidence_id, "--actor", "a",
                       "--note", "n", "--json")
payload = json.loads(out)
check("invalid vocabulary: exit 2 with the typed code",
      code == 2 and payload["error"]["code"] == "unknown_vocabulary_value")

code, out, _ = run_cli("promotion", "promote", "--workspace", pid,
                       "--candidate", "sc_missing", "--actor", "a",
                       "--note", "n", "--json")
check("missing candidate: exit 1 with candidate_not_found",
      code == 1 and json.loads(out)["error"]["code"]
      == "candidate_not_found")

finish()
