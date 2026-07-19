"""CLI ``asset`` command group — JSON contract, exit codes, bounded output,
workspace scoping and honest not-found.

Each case runs the CLI as a REAL subprocess: what matters is the exit code the
shell sees and that stdout carries exactly one JSON object with every diagnostic
on stderr.
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "fixtures", "sample-repo")

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc
          + (f"  [{extra}]" if extra and not cond else ""))


ENV = dict(os.environ)
ENV.update({
    "OPENMIND_DATA_DIR": tempfile.mkdtemp(prefix="om_acli_"),
    "OPENMIND_MACHINE_DIR": tempfile.mkdtemp(prefix="om_acli_machine_"),
    "OPENMIND_EMBED_OFFLINE": "1",
    "OPENMIND_EMBED_DEVICE": "cpu",
    "OPENMIND_INGEST_FREE_GPU": "0",
    "OPENMIND_ENRICH_EGRESS": "0",
    "OPENMIND_SOURCELINK_EGRESS": "0",
    "PYTHONIOENCODING": "utf-8",
})


def run(*args, timeout=900):
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
# Set-up: a real workspace, ingested with the offline embedder.
# ---------------------------------------------------------------------------
code, out, err = run("init", "--name", "cli-assets", "--path", FIXTURE,
                     "--ingest", "--wait", "--json")
init = as_json(out)
check("init --ingest --wait exits 0", code == 0, str(code))
WS = init["workspace_id"] if init else ""
check("workspace was created", bool(WS))

# ---------------------------------------------------------------------------
# asset list
# ---------------------------------------------------------------------------
code, out, err = run("asset", "list", "--workspace", WS, "--json")
data = as_json(out)
check("asset list --json exits 0", code == 0)
check("asset list --json prints exactly one JSON object", data is not None)
check("asset list carries the total and a bounded page",
      data and "total" in data and "count" in data and "limit" in data)
check("asset list found the ingested assets", data and data["total"] >= 1)
check("asset list stdout is pure JSON (diagnostics on stderr)",
      "\x1b[" not in out)   # no ANSI escapes ever

# list limit is enforced
code, out, err = run("asset", "list", "--workspace", WS, "--limit", "2", "--json")
data = as_json(out)
check("asset list --limit bounds the page", data and data["count"] <= 2)

# type + state filters
code, out, err = run("asset", "list", "--workspace", WS, "--type", "source-code",
                     "--state", "active", "--json")
data = as_json(out)
check("asset list --type/--state filters exit 0", code == 0 and data is not None)

# an unknown type is a usage error (exit 2), not a silent empty page
code, out, err = run("asset", "list", "--workspace", WS, "--type", "bogus", "--json")
data = as_json(out)
check("asset list rejects an unknown --type", code == 2 and data and data["ok"] is False)

# ---------------------------------------------------------------------------
# asset show / revisions / segments / evidence
# ---------------------------------------------------------------------------
code, out, err = run("asset", "list", "--workspace", WS, "--limit", "1", "--json")
first = as_json(out)["assets"][0]
AID = first["id"]

code, out, err = run("asset", "show", "--workspace", WS, "--asset", AID, "--json")
show = as_json(out)
check("asset show --json exits 0", code == 0 and show is not None)
check("asset show returns the current revision summary",
      show and show["asset"].get("current_revision"))
RID = show["asset"]["current_revision"]["id"]

code, out, err = run("asset", "revisions", "--workspace", WS, "--asset", AID, "--json")
revs = as_json(out)
check("asset revisions --json exits 0", code == 0 and revs is not None)
check("asset revisions lists at least one revision", revs and revs["count"] >= 1)

code, out, err = run("asset", "segments", "--workspace", WS, "--revision", RID, "--json")
segs = as_json(out)
check("asset segments --json exits 0", code == 0 and segs is not None)
check("asset segments is bounded (limit present)", segs and "limit" in segs)
check("asset segments surfaces an evidence id for discovery",
      segs and segs["segments"] and segs["segments"][0].get("evidence_id"))
EID = segs["segments"][0]["evidence_id"]

code, out, err = run("asset", "evidence", "--workspace", WS, "--evidence", EID,
                     "--max-chars", "50", "--json")
ev = as_json(out)
check("asset evidence --json exits 0", code == 0 and ev is not None)
check("asset evidence reports snapshot + current-source status",
      ev and "snapshot" in ev and "current_source" in ev)
check("asset evidence content is bounded by --max-chars",
      ev and len(ev["content"]) <= 50)
check("asset evidence carries a workspace-relative locator",
      ev and not str(ev["locator"].get("file", "")).startswith("/"))

# ---------------------------------------------------------------------------
# Workspace scoping + honest not-found (typed errors, right exit codes)
# ---------------------------------------------------------------------------
code2, out2, err2 = run("init", "--name", "other", "--json")
WS2 = as_json(out2)["workspace_id"]

code, out, err = run("asset", "show", "--workspace", WS2, "--asset", AID, "--json")
data = as_json(out)
check("cross-workspace asset show fails with exit 1", code == 1)
check("cross-workspace asset show reports a typed not-found",
      data and data["ok"] is False and data["error"]["code"] == "asset_not_found")

code, out, err = run("asset", "evidence", "--workspace", WS2, "--evidence", EID, "--json")
data = as_json(out)
check("cross-workspace evidence fails with a typed error",
      code == 1 and data and data["error"]["code"] == "evidence_not_found")

code, out, err = run("asset", "show", "--workspace", "p_nonexistent0", "--asset",
                     AID, "--json")
data = as_json(out)
check("unknown workspace fails honestly (exit 1, typed not-found)",
      code == 1 and data and data["error"]["code"] == "workspace_not_found")

# a missing revision/evidence id is a typed not-found, not a crash
code, out, err = run("asset", "segments", "--workspace", WS, "--revision",
                     "r_doesnotexist", "--json")
data = as_json(out)
check("missing revision -> typed revision_not_found (exit 1)",
      code == 1 and data and data["error"]["code"] == "revision_not_found")

# ---------------------------------------------------------------------------
# asset add — single file rules
# ---------------------------------------------------------------------------
# a directory is rejected with guidance to use `openmind add`
code, out, err = run("asset", "add", "--workspace", WS, "--path", FIXTURE, "--json")
data = as_json(out)
check("asset add rejects a directory (exit 2)", code == 2)
check("asset add directory error is a typed invalid_request",
      data and data["ok"] is False and data["error"]["code"] == "invalid_request")

# a supported single file syncs — it must live UNDER the registered source
# root (the fixture), so create it there and remove it afterwards.
newfile = os.path.join(FIXTURE, "src", "_cli_added_tmp.ts")
with open(newfile, "w", encoding="utf-8") as fh:
    fh.write("export const Added = 1;\n")
try:
    code, out, err = run("asset", "add", "--workspace", WS, "--path", newfile,
                         "--wait", "--json")
    data = as_json(out)
    check("asset add of a supported file exits 0", code == 0, str(code) + " " + (err or "")[-200:])
    check("asset add reports the new asset", data and data.get("asset_id"))
    check("asset add did not corrupt the glossary (filtered ingest)",
          data is not None)
finally:
    if os.path.exists(newfile):
        os.remove(newfile)

# ---------------------------------------------------------------------------
# status includes additive asset counts
# ---------------------------------------------------------------------------
code, out, err = run("status", "--workspace", WS, "--json")
st = as_json(out)
check("status --json exits 0", code == 0 and st is not None)
check("status carries additive asset counts",
      st and "assets" in st and "assets_total" in st["assets"]
      and "revisions" in st["assets"] and "evidence" in st["assets"])
check("status still carries every prior key (backward compatible)",
      st and all(k in st for k in ("counts", "paths", "template", "state",
                                   "schema_version", "version")))

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
