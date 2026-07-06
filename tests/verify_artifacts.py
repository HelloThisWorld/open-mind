"""Artifact export contract (schema 1.0.0) — the integration boundary that
external consumers (e.g. open-mind-mcp-server) depend on.

Runs with no heavy dependencies: pure stdlib + the deterministic extractors.
Generates real artifacts from fixtures/sample-repo into a temp directory and
verifies the schema, the evidence discipline, and determinism.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import artifacts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_REPO = ROOT / "fixtures" / "sample-repo"

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def load(out_dir: Path, name: str):
    return json.loads((out_dir / name).read_text(encoding="utf-8"))


def valid_evidence(ev, repo: Path) -> bool:
    """An evidence record must cite a real repo-relative file and a line that
    exists in it, with a non-empty snippet."""
    if not isinstance(ev.get("file"), str) or not ev["file"]:
        return False
    if ev["file"].startswith(("/", "\\")) or ":" in ev["file"].split("/")[0]:
        return False   # absolute or drive-prefixed paths are not portable
    target = repo / ev["file"]
    if not target.is_file():
        return False
    line = ev.get("line")
    if not isinstance(line, int) or line < 1:
        return False
    line_count = len(target.read_text(encoding="utf-8").split("\n"))
    if line > line_count:
        return False
    return isinstance(ev.get("snippet"), str) and bool(ev["snippet"].strip())


out_root = Path(tempfile.mkdtemp(prefix="openmind-artifacts-"))
out_a = out_root / "a"
out_b = out_root / "b"
try:
    summary = artifacts.generate_artifacts(
        str(FIXTURE_REPO), str(out_a), generated_at="2026-01-01T00:00:00Z")
    artifacts.generate_artifacts(
        str(FIXTURE_REPO), str(out_b), generated_at="2026-01-01T00:00:00Z")

    # --- manifest ---
    check("manifest.json is generated", (out_a / "manifest.json").is_file())
    manifest = load(out_a, "manifest.json")
    check("manifest schemaVersion is 1.1.0", manifest.get("schemaVersion") == "1.1.0")
    check("manifest names the generator", manifest.get("generator", {}).get("name") == "open-mind")
    check("manifest records the repository name",
          manifest.get("repository", {}).get("name") == "sample-repo")
    check("manifest commit is a string or null",
          manifest.get("repository", {}).get("commit") is None
          or isinstance(manifest["repository"]["commit"], str))
    refs = manifest.get("artifacts", {})
    check("manifest references all five artifact types",
          set(refs.keys()) == {"metadata", "glossary", "architecture", "flows", "sourceIndex"})
    check("every artifact file referenced by the manifest exists",
          all((out_a / fname).is_file() for fname in refs.values()))

    # --- metadata ---
    metadata = load(out_a, refs["metadata"])
    check("metadata summary counts are positive",
          metadata["summary"]["filesIndexed"] > 0
          and metadata["summary"]["symbolsIndexed"] > 0
          and metadata["summary"]["artifactsGenerated"] == len(refs))
    check("metadata is self-describing about the template (none matches this repo)",
          "template" in metadata and metadata["template"] is None)
    check("without a template the 1.0.0 shape is preserved (no layers key)",
          "layers" not in load(out_a, refs["architecture"]))

    # --- glossary ---
    glossary_doc = load(out_a, refs["glossary"])
    entries = glossary_doc.get("entries", [])
    check("glossary extracts entries from the fixture repo", len(entries) >= 5)
    check("every glossary entry has evidence",
          all(len(e.get("evidence", [])) >= 1 for e in entries))
    check("every glossary evidence record resolves to a real file:line",
          all(valid_evidence(ev, FIXTURE_REPO) for e in entries for ev in e["evidence"]))
    check("glossary confidence values are valid",
          all(e.get("confidence") in ("high", "medium", "low") for e in entries))
    check("glossary kinds are valid",
          all(e.get("kind") in ("class", "function", "service", "event", "config",
                                "module", "concept", "unknown") for e in entries))
    terms = {e["term"] for e in entries}
    check("known fixture terms are extracted (OrderService, RetryPolicy, DLQ)",
          {"OrderService", "RetryPolicy", "DLQ"} <= terms)
    check("glossary definitions are verbatim (DLQ expansion)",
          next((e["description"] for e in entries if e["term"] == "DLQ"), "")
          == "dead letter queue")

    # --- architecture ---
    architecture_doc = load(out_a, refs["architecture"])
    components = architecture_doc.get("components", [])
    check("architecture components are extracted", len(components) >= 3)
    check("every architecture component has evidence",
          all(len(c.get("evidence", [])) >= 1 for c in components))
    check("every architecture evidence record resolves to a real file:line",
          all(valid_evidence(ev, FIXTURE_REPO) for c in components for ev in c["evidence"]))
    check("architecture components carry responsibilities",
          all(isinstance(c.get("responsibilities"), list) and c["responsibilities"]
              for c in components))

    # --- flows ---
    flows_doc = load(out_a, refs["flows"])
    flows = flows_doc.get("flows", [])
    check("entry-point flows are extracted", len(flows) >= 1)
    check("flow steps are ordered from 1 without gaps",
          all([s["order"] for s in f["steps"]] == list(range(1, len(f["steps"]) + 1))
              for f in flows))
    check("every flow step has evidence resolving to a real file:line",
          all(valid_evidence(ev, FIXTURE_REPO)
              for f in flows for s in f["steps"] for ev in s["evidence"]))

    # --- source index ---
    source_index = load(out_a, refs["sourceIndex"])
    files = source_index.get("files", [])
    check("source index covers the indexed files",
          len(files) == metadata["summary"]["filesIndexed"])
    check("source index paths resolve inside the fixture repo",
          all((FIXTURE_REPO / f["path"]).is_file() for f in files))
    all_symbols = {s["name"] for f in files for s in f.get("symbols", [])}
    check("source index contains known fixture symbols",
          {"createOrder", "publishEvent", "OrderService"} <= all_symbols)
    check("source index symbol lines are valid",
          all(valid_evidence({"file": f["path"], "line": s["line"], "snippet": s["snippet"]},
                             FIXTURE_REPO)
              for f in files for s in f.get("symbols", [])))

    # --- determinism (same input + pinned timestamp -> byte-identical output) ---
    identical = all(
        (out_a / fname).read_bytes() == (out_b / fname).read_bytes()
        for fname in ["manifest.json"] + sorted(refs.values()))
    check("two runs over the same input are byte-identical", identical)

    # --- standalone boundary ---
    check("export runs without the MCP server or the web app",
          "mcp" not in sys.modules and "fastapi" not in sys.modules)
    check("CLI summary reports the artifact files",
          set(summary["files"]) == {"manifest.json"} | set(refs.values()))
finally:
    shutil.rmtree(out_root, ignore_errors=True)

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
