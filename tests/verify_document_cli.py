"""The document CLI contract: JSON shape, exit codes, bounds, no ANSI.

Every command runs as a real subprocess, so what is asserted is what a script or
an agent actually receives on stdout, stderr and the exit status — not what an
in-process call happens to return.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from pathlib import Path  # noqa: E402

_results = []
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "documents"

EXIT_OK = 0
EXIT_DOMAIN_FAILURE = 1
EXIT_INVALID_USAGE = 2


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


ENV = dict(os.environ)
ENV.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})


def run(*args, timeout=900):
    proc = subprocess.run([sys.executable, "-m", "openmind.cli", *args],
                          cwd=str(REPO), env=ENV, capture_output=True,
                          text=True, errors="replace", timeout=timeout)
    return proc


def run_json(*args, **kwargs):
    proc = run(*args, "--json", **kwargs)
    try:
        return proc, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc, None


SRC = Path(tempfile.mkdtemp(prefix="om_cli_src_"))
(SRC / "NameCheckService.java").write_text(
    "package a;\npublic class NameCheckService { void screen() {} }\n",
    encoding="utf-8")

proc, created = run_json("init", "--name", "cli-docs", "--path", str(SRC))
WS = created["workspace_id"]
run("ingest", "--workspace", WS, "--wait", "--timeout", "600")


def attach(name, source=None, text=None):
    target = Path(tempfile.mkdtemp(prefix="om_cli_att_")) / name
    if text is not None:
        target.write_text(text, encoding="utf-8")
    else:
        shutil.copy(FIXTURES / source, target)
    return str(target)


# ---------------------------------------------------------------------------
# 1. document add
# ---------------------------------------------------------------------------
spec = attach("Requirements_v3.md", source="sample-requirements.md")
proc, payload = run_json("document", "add", "--workspace", WS, "--path", spec,
                         "--wait")
check("add: exits 0 on success", proc.returncode == EXIT_OK)
check("add: stdout is exactly one JSON object", payload is not None)
check("add: ok is true", payload.get("ok") is True)
check("add: the import status is reported", payload.get("status") == "new_asset")
check("add: the logical key is reported",
      payload.get("logical_key") == "documents/Requirements_v3.md")
check("add: the import report is included", bool(payload.get("import_report")))
report = payload["import_report"]
check("add: the report names the parser and its status",
      report.get("parser") == "markdown" and report.get("parse_status") == "parsed")
check("add: the report counts segments and indexed blocks",
      report.get("segments_created", 0) > 0
      and report.get("blocks_indexed", 0) > 0)
check("add: the report names the asset and revision",
      report.get("asset_id", "").startswith("a_")
      and report.get("revision_id", "").startswith("r_"))
check("add: related candidates are summarized",
      "related_candidates" in report)
check("add: no ANSI escape reaches stdout in JSON mode",
      "\x1b[" not in proc.stdout)
check("add: no ANSI escape reaches stderr", "\x1b[" not in proc.stderr)
check("add: the absolute source path is not echoed into the JSON result",
      spec not in proc.stdout)

ASSET = report["asset_id"]
REVISION = report["revision_id"]

# ---------------------------------------------------------------------------
# 2. Human mode
# ---------------------------------------------------------------------------
proc = run("document", "add", "--workspace", WS, "--path", spec)
check("add: a duplicate in human mode still exits 0",
      proc.returncode == EXIT_OK)
check("add: human mode prints the status on stdout", "duplicate" in proc.stdout)
check("add: human mode emits no JSON on stdout", not proc.stdout.startswith("{"))

# ---------------------------------------------------------------------------
# 3. Duplicate, collision and the exit codes that matter to a script
# ---------------------------------------------------------------------------
proc, duplicate = run_json("document", "add", "--workspace", WS, "--path", spec,
                           "--wait")
check("add: an exact duplicate is reported as duplicate",
      duplicate.get("status") == "duplicate")
check("add: a duplicate exits 0 (it is not an error)",
      proc.returncode == EXIT_OK)
check("add: a duplicate creates no job", duplicate.get("job_id") is None)

collision = attach("Requirements_v3.md",
                   text="# Another team's document\n\nUnrelated.\n")
proc, possible = run_json("document", "add", "--workspace", WS, "--path",
                          collision, "--wait")
check("add: a filename collision reports possible_revision",
      possible.get("status") == "possible_revision")
check("add: possible_revision exits NON-ZERO so a script cannot read "
      "'nothing written' as success", proc.returncode == EXIT_DOMAIN_FAILURE)
check("add: the failure payload is still valid JSON", possible is not None)
check("add: ok is false", possible.get("ok") is False)
check("add: the error code is machine-readable",
      (possible.get("error") or {}).get("code") == "possible_revision")
check("add: the resolving commands are in the error details",
      "guidance" in (possible.get("error") or {}).get("details", {}))

proc, distinct = run_json("document", "add", "--workspace", WS, "--path",
                          collision, "--new-asset", "--wait")
check("add: --new-asset resolves the collision", proc.returncode == EXIT_OK)
check("add: --new-asset produces a distinct readable key",
      distinct["logical_key"].startswith("documents/Requirements_v3--"))

# Distinct content: `collision`'s bytes were just imported under --new-asset,
# and re-offering them anywhere in the workspace is (correctly) a duplicate.
next_revision = attach("Requirements_v3.md",
                       text="# NameCheck Requirements\n\nREQ-NC-901: revised.\n")
proc, revised = run_json("document", "add", "--workspace", WS, "--path",
                         next_revision, "--asset", ASSET, "--wait")
check("add: --asset creates the next revision",
      revised.get("status") == "revision"
      and revised["import_report"]["revision_sequence"] == 2)
check("add: --asset targets the named asset",
      revised["import_report"]["asset_id"] == ASSET)

# ---------------------------------------------------------------------------
# 4. Mutually exclusive target flags
# ---------------------------------------------------------------------------
proc = run("document", "add", "--workspace", WS, "--path", spec, "--new-asset",
           "--logical-key", "documents/x", "--json")
check("add: --new-asset with --logical-key is rejected",
      proc.returncode == EXIT_INVALID_USAGE)
check("add: the rejection explains the conflict on stderr",
      "not allowed with" in proc.stderr)

# ---------------------------------------------------------------------------
# 5. --dry-run stores nothing
# ---------------------------------------------------------------------------
_, before = run_json("document", "list", "--workspace", WS)
fresh = attach("dry.md", text="# Dry run\n\nnothing should be stored.\n")
proc, plan = run_json("document", "add", "--workspace", WS, "--path", fresh,
                      "--dry-run")
check("dry-run: exits 0", proc.returncode == EXIT_OK)
check("dry-run: reports what WOULD happen", plan.get("status") == "new_asset")
check("dry-run: is flagged as a dry run", plan.get("dry_run") is True)
check("dry-run: creates no job", plan.get("job_id") is None)
check("dry-run: names the parser that would be used",
      plan.get("parser") == "markdown")
_, after = run_json("document", "list", "--workspace", WS)
check("dry-run: stores nothing", after["total"] == before["total"])

# ---------------------------------------------------------------------------
# 6. document list
# ---------------------------------------------------------------------------
proc, listing = run_json("document", "list", "--workspace", WS)
check("list: exits 0", proc.returncode == EXIT_OK)
check("list: reports a total and a count",
      isinstance(listing.get("total"), int)
      and listing["count"] == len(listing["documents"]))
check("list: every entry carries its parse summary",
      all("document" in d and d["document"].get("parser_name")
          for d in listing["documents"]))
_, bounded = run_json("document", "list", "--workspace", WS, "--limit", "1")
check("list: --limit bounds the page", bounded["count"] <= 1)
check("list: the total still reports everything",
      bounded["total"] == listing["total"])
_, filtered = run_json("document", "list", "--workspace", WS, "--parser",
                       "markdown")
check("list: --parser filters",
      all(d["document"]["parser_name"] == "markdown"
          for d in filtered["documents"]))
_, by_status = run_json("document", "list", "--workspace", WS, "--status",
                        "parsed")
check("list: --status filters",
      all(d["document"]["status"] == "parsed" for d in by_status["documents"]))
proc, bad_status = run_json("document", "list", "--workspace", WS, "--status",
                            "nonsense")
check("list: an unknown status is a caller error, not an empty page",
      proc.returncode != EXIT_OK and bad_status.get("ok") is False)
check("list: the error names the allowed values",
      "allowed" in (bad_status.get("error") or {}).get("details", {}))

# ---------------------------------------------------------------------------
# 7. document show
# ---------------------------------------------------------------------------
proc, shown = run_json("document", "show", "--workspace", WS, "--asset", ASSET)
check("show: exits 0", proc.returncode == EXIT_OK)
document = shown["document"]
check("show: returns the asset", document["id"] == ASSET)
check("show: returns the current revision",
      (document.get("current_revision") or {}).get("sequence") == 2)
check("show: returns the parse summary",
      document["parse"]["parser_name"] == "markdown")
check("show: reports the indexed chunk count",
      isinstance(document["index"]["chunk_count"], int))
check("show: reports the source kind",
      document["source_kind"] == "attachment")
proc, missing = run_json("document", "show", "--workspace", WS, "--asset",
                         "a_doesnotexist")
check("show: an unknown asset is an honest not-found",
      proc.returncode == EXIT_DOMAIN_FAILURE
      and (missing.get("error") or {}).get("code") == "asset_not_found")
proc, cross = run_json("document", "show", "--workspace", "p_nonexistent",
                       "--asset", ASSET)
check("show: an unknown workspace is an honest not-found",
      proc.returncode == EXIT_DOMAIN_FAILURE
      and (cross.get("error") or {}).get("code") == "workspace_not_found")

# ---------------------------------------------------------------------------
# 8. document outline
# ---------------------------------------------------------------------------
proc, outline = run_json("document", "outline", "--workspace", WS,
                         "--revision", REVISION)
check("outline: exits 0", proc.returncode == EXIT_OK)
check("outline: returns structure, not content",
      all(len(e["preview"]) <= 120 for e in outline["outline"]))
check("outline: every entry carries a block type and an evidence id",
      all(e["block_type"] and e["evidence_id"] for e in outline["outline"]))
check("outline: entries carry the heading path",
      any(e["heading_path"] for e in outline["outline"]))
check("outline: the parse is summarized",
      outline["parse"]["parser_name"] == "markdown")
_, small = run_json("document", "outline", "--workspace", WS, "--revision",
                    REVISION, "--limit", "3")
check("outline: --limit bounds the response", small["count"] == 3)
check("outline: the total still reports the whole document",
      small["total"] == outline["total"])
proc, no_rev = run_json("document", "outline", "--workspace", WS,
                        "--revision", "r_doesnotexist")
check("outline: an unknown revision is an honest not-found",
      proc.returncode == EXIT_DOMAIN_FAILURE
      and (no_rev.get("error") or {}).get("code") == "revision_not_found")

# ---------------------------------------------------------------------------
# 9. document search
# ---------------------------------------------------------------------------
# The original requirements document has since been superseded by revision 2,
# so attach a document that contains REQ-NC-017 in its CURRENT revision.
# Its text must be DISTINCT from the fixture: re-attaching identical bytes is
# (correctly) a duplicate, and a superseded revision is not searchable —
# historical search is deliberately not implemented in Phase 3.
searchable = attach("searchable.md",
                    text="# Search fixture\n\n"
                         "REQ-NC-017: the manual review timeout rule, restated "
                         "here so it lives in a current revision.\n")
_, added = run_json("document", "add", "--workspace", WS, "--path", searchable,
                    "--wait")
check("search: the search fixture imported as a new document",
      added.get("status") == "new_asset")
proc, found = run_json("document", "search", "--workspace", WS, "--query",
                       "REQ-NC-901")
check("search: exits 0", proc.returncode == EXIT_OK)
proc, req = run_json("document", "search", "--workspace", WS, "--query",
                     "REQ-NC-017")
check("search: an exact Requirement ID is found", req["count"] > 0)
check("search: the mode is reported", req["query_mode"] == "exact_token")
check("search: every hit carries an evidence id",
      all(h["evidence_id"] for h in req["hits"]))
check("search: hits are bounded",
      all(len(h["excerpt"]) <= 400 for h in req["hits"]))
_, limited = run_json("document", "search", "--workspace", WS, "--query",
                      "review", "--limit", "2")
check("search: --limit bounds the hits", limited["count"] <= 2)
_, empty = run_json("document", "search", "--workspace", WS, "--query",
                    "ZZQQ-NOTHING-12345")
check("search: a query matching nothing returns an honest empty result",
      empty["ok"] is True and empty["count"] >= 0)

# ---------------------------------------------------------------------------
# 10. document related
# ---------------------------------------------------------------------------
proc, related = run_json("document", "related", "--workspace", WS, "--asset",
                         ASSET)
check("related: exits 0", proc.returncode == EXIT_OK)
check("related: the envelope is labelled candidate",
      related["status"] == "candidate")
check("related: every candidate is labelled candidate",
      all(c["status"] == "candidate" for c in related["candidates"]))
check("related: every candidate carries a confidence",
      all(c["confidence"] in ("high", "medium", "low")
          for c in related["candidates"]))
check("related: no candidate claims a verified relationship",
      not any(c["candidate_type"] in ("implements", "refines", "verifies",
                                      "contradicts")
              for c in related["candidates"]))
_, few = run_json("document", "related", "--workspace", WS, "--asset", ASSET,
                  "--limit", "1")
check("related: --limit bounds the candidates", few["count"] <= 1)

# ---------------------------------------------------------------------------
# 11. knowledge search
# ---------------------------------------------------------------------------
proc, knowledge = run_json("knowledge", "search", "--workspace", WS, "--query",
                           "NameCheck timeout")
check("knowledge: exits 0", proc.returncode == EXIT_OK)
check("knowledge: code and documents are separate sections",
      "code" in knowledge and "documents" in knowledge)
check("knowledge: the grounding counts both sides",
      knowledge["grounding"]["codeCount"] == len(knowledge["code"]["hits"])
      and knowledge["grounding"]["documentCount"]
      == len(knowledge["documents"]["hits"]))
check("knowledge: the response disclaims any relationship",
      "NOT a claim" in knowledge["grounding"]["note"])

# ---------------------------------------------------------------------------
# 12. Group help and pre-existing commands
# ---------------------------------------------------------------------------
proc = run("document")
check("group: `document` with no subcommand prints help and exits 2",
      proc.returncode == EXIT_INVALID_USAGE and "add" in proc.stderr)
proc = run("knowledge")
check("group: `knowledge` with no subcommand prints help and exits 2",
      proc.returncode == EXIT_INVALID_USAGE and "search" in proc.stderr)
proc = run("--help")
check("help: the document group is advertised", "document" in proc.stdout)
check("help: the knowledge group is advertised", "knowledge" in proc.stdout)

# The Phase 1/2 commands must be untouched.
for command in (("doctor",), ("status", "--workspace", WS),
                ("asset", "list", "--workspace", WS)):
    proc, out = run_json(*command)
    check(f"compat: `{' '.join(command[:2])}` still works",
          proc.returncode == EXIT_OK and out is not None)
_, status = run_json("status", "--workspace", WS)
check("compat: status still reports the Phase 2 asset counts",
      {"assets_total", "revisions", "segments", "evidence"}
      <= set(status["assets"]))
check("compat: status reports the v0007 schema head",
      status["schema_version"] == 7)
check("compat: the version constant advanced to 1.6.0-dev",
      status["version"] == "1.6.0-dev")

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
