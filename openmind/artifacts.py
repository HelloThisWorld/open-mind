"""Artifact export — the stable integration contract for external tools.

WHY THIS EXISTS (design intent)
-------------------------------
Open Mind's knowledge lives in internal, versioned maps (glossary + structure)
persisted per project. External integrations — most importantly the companion
``open-mind-mcp-server`` — must not import those internals: they would break on
every internal schema bump. This module is the STABLE BOUNDARY instead: it runs
the same deterministic extractors and writes a self-describing ``.openmind``
artifact directory with a fixed, versioned schema (``manifest.json`` +
machine-readable artifact files). Consumers depend only on that contract.

THE CONTRACT (schema version 1.0.0)
-----------------------------------
    .openmind/
      manifest.json        who generated what, when, from which repository
      metadata.json        summary counts
      glossary.json        verbatim terms with file:line evidence
      architecture.json    directory-module components with evidence
      flows.json           entry-point flows over the name-based call graph
      source-index.json    per-file symbol index

Every factual entry carries ``evidence`` records — ``{file, line, snippet}``
with repo-relative paths — and a ``confidence`` of ``high``/``medium``/``low``.
Verbatim glossary extraction is ``high``; directory-module components and
name-based call flows are heuristic projections and are marked ``medium``
(``low`` when an included call edge is ambiguous). Nothing here is model
generated; unsupported artifact types would be emitted as empty-but-valid
arrays rather than invented content.

STANDALONE BY DESIGN
--------------------
This module needs only the Python standard library plus the deterministic
extractors (:mod:`openmind.walker`, :mod:`openmind.glossary`,
:mod:`openmind.structure`). It does not start the web app, does not require
the optional LLM/embedding stack, and knows nothing about the MCP server.

Usage:
    python -m openmind.artifacts --repo ./fixtures/sample-repo --output ./.openmind
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config, glossary, structure, walker

SCHEMA_VERSION = "1.0.0"
GENERATOR_NAME = "open-mind"

# Documentation extensions scanned for glossary definitions in an export run.
# The interactive ingest pipeline indexes code/config only; the export adds
# docs because README/GLOSSARY files are primary definition sources.
_EXPORT_DOC_EXTS = {".md", ".markdown", ".rst", ".adoc", ".txt"}

_ARTIFACT_FILES = {
    "metadata": "metadata.json",
    "glossary": "glossary.json",
    "architecture": "architecture.json",
    "flows": "flows.json",
    "sourceIndex": "source-index.json",
}

_MAX_SNIPPET_CHARS = 200
_MAX_COMPONENTS = 50
_MAX_RESPONSIBILITIES = 6
_MAX_FLOWS = 10
_MAX_FLOW_CALL_STEPS = 6


# ---------------------------------------------------------------------------
# Repo walk (code + docs, honoring the same ignore rules as the app walker)
# ---------------------------------------------------------------------------
def _iter_repo_files(root: str) -> Iterable[str]:
    """Yield indexable files under *root*: the app's INDEX_EXTENSIONS plus
    documentation files (glossary sources). Prunes the same built-in ignore
    dirs, honors .gitignore, skips binaries/oversized files, and never
    descends into a previously exported ``.openmind`` directory."""
    root = walker.norm(root)
    gi = walker.GitIgnore(root)
    wanted = set(config.INDEX_EXTENSIONS) | _EXPORT_DOC_EXTS
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        kept = []
        for d in dirnames:
            if d in config.DEFAULT_IGNORE_DIRS or d == ".openmind":
                continue
            full = walker.norm(os.path.join(dirpath, d))
            if gi.ignored(full, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in sorted(filenames):
            full = walker.norm(os.path.join(dirpath, f))
            ext = os.path.splitext(f)[1].lower()
            if ext not in wanted or ext in config.BINARY_EXTENSIONS:
                continue
            if gi.ignored(full, is_dir=False):
                continue
            try:
                if os.path.getsize(full) > config.MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield full


def _relativize(path: str, root: str) -> str:
    p = walker.norm(path)
    r = walker.norm(root)
    if p == r:
        return ""
    if p.startswith(r + "/"):
        return p[len(r) + 1:]
    return p


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------
def _line_text(text: str, line: int) -> str:
    """The verbatim (stripped, capped) content of a 1-based line, or ''."""
    if line < 1:
        return ""
    lines = text.split("\n")
    if line > len(lines):
        return ""
    return lines[line - 1].strip()[:_MAX_SNIPPET_CHARS]


def _evidence(file: str, line: int, snippet: str) -> Dict[str, Any]:
    return {"file": file, "line": line, "snippet": snippet}


def _snippet_or(text_by_file: Dict[str, str], file: str, line: int,
                fallback: str) -> str:
    snippet = _line_text(text_by_file.get(file, ""), line)
    return snippet or fallback[:_MAX_SNIPPET_CHARS]


# ---------------------------------------------------------------------------
# Contract mappers (internal docs -> schema 1.0.0 artifact shapes)
# ---------------------------------------------------------------------------
_GLOSSARY_KIND_BY_DEF = {"class": "class", "function": "function"}


def _map_glossary(gdoc: Dict[str, Any], sdoc: Dict[str, Any],
                  text_by_file: Dict[str, str]) -> Dict[str, Any]:
    definitions = sdoc.get("definitions") or {}
    entries: List[Dict[str, Any]] = []
    for term, e in sorted((gdoc.get("terms") or {}).items()):
        file = e.get("source_file") or ""
        line = int(e.get("line_number") or 0)
        definition = (e.get("definition") or "").strip()
        if not file or line < 1 or not definition:
            continue   # no usable provenance -> never exported
        kind = "concept"
        for d in definitions.get(term) or []:
            kind = _GLOSSARY_KIND_BY_DEF.get(d.get("kind", ""), "concept")
            break
        entries.append({
            "term": term,
            "kind": kind,
            "description": definition,
            "evidence": [_evidence(file, line,
                                   _snippet_or(text_by_file, file, line, definition))],
            "confidence": "high",   # verbatim extraction, exact provenance
        })
    return {"schemaVersion": SCHEMA_VERSION, "entries": entries}


def _map_architecture(sdoc: Dict[str, Any], repo_name: str,
                      text_by_file: Dict[str, str]) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = sdoc.get("files") or []
    defs_by_module: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for f in files:
        module = os.path.dirname(f["path"]).replace("\\", "/") or "."
        for d in f.get("defs") or []:
            defs_by_module.setdefault(module, []).append((f["path"], d))

    components: List[Dict[str, Any]] = []
    for module, info in sorted((sdoc.get("modules") or {}).items()):
        mod_defs = sorted(defs_by_module.get(module, []),
                          key=lambda fd: (fd[0], fd[1].get("line", 0)))
        if not mod_defs:
            continue   # a component is only exported with real evidence
        first_file, first_def = mod_defs[0]
        languages = ", ".join(sorted((info.get("languages") or {}).keys()))
        name = module if module != "." else repo_name
        description = (
            f"Directory module '{module}' with {info.get('files', 0)} file(s), "
            f"{info.get('lines', 0)} line(s) of {languages or 'source'}, and "
            f"{len(mod_defs)} extracted definition(s).")
        responsibilities = [
            f"Defines {d.get('name')} ({d.get('kind')}) in {path}"
            for path, d in mod_defs[:_MAX_RESPONSIBILITIES]
        ]
        line = int(first_def.get("line") or 1)
        components.append({
            "name": name,
            "kind": "module",
            "description": description,
            "responsibilities": responsibilities,
            "evidence": [_evidence(first_file, line,
                                   _snippet_or(text_by_file, first_file, line,
                                               str(first_def.get("name"))))],
            "confidence": "medium",   # directory->component mapping is heuristic
        })
        if len(components) >= _MAX_COMPONENTS:
            break
    return {"schemaVersion": SCHEMA_VERSION, "components": components}


def _map_flows(sdoc: Dict[str, Any], text_by_file: Dict[str, str]) -> Dict[str, Any]:
    definitions = sdoc.get("definitions") or {}
    call_edges = (sdoc.get("call_graph") or {}).get("edges") or []
    edges_by_from: Dict[str, List[Dict[str, Any]]] = {}
    for e in call_edges:
        edges_by_from.setdefault(e["from"], []).append(e)

    seen_files: set = set()
    flows: List[Dict[str, Any]] = []
    for entry in sdoc.get("entry_points") or []:
        file = entry["file"]
        if file in seen_files:
            continue
        seen_files.add(file)

        steps: List[Dict[str, Any]] = []
        line = int(entry.get("line") or 1)
        steps.append({
            "order": 1,
            "description": f"Entry point ({entry.get('kind', 'entry')}) declared in {file}",
            "component": os.path.dirname(file).replace("\\", "/") or ".",
            "evidence": [_evidence(file, line,
                                   _snippet_or(text_by_file, file, line, file))],
        })

        ambiguous = False
        out_edges = sorted(edges_by_from.get(file, []),
                           key=lambda e: (e["symbol"], e["to"]))
        for e in out_edges[:_MAX_FLOW_CALL_STEPS]:
            target = e["to"]
            def_site = next((d for d in definitions.get(e["symbol"]) or []
                             if d.get("file") == target), None)
            if def_site is None:
                continue
            ambiguous = ambiguous or bool(e.get("ambiguous"))
            def_line = int(def_site.get("line") or 1)
            steps.append({
                "order": len(steps) + 1,
                "description": (f"Calls {e['symbol']} ({def_site.get('kind', 'symbol')}) "
                                f"defined in {target}"),
                "component": os.path.dirname(target).replace("\\", "/") or ".",
                "evidence": [_evidence(target, def_line,
                                       _snippet_or(text_by_file, target, def_line,
                                                   e["symbol"]))],
            })

        flows.append({
            "name": f"Entry flow: {file}",
            "description": (f"Execution flow derived from the entry point in {file} "
                            "and the name-based call graph."),
            "steps": steps,
            # name-based call edges are heuristic; ambiguous targets lower it
            "confidence": "low" if ambiguous else "medium",
        })
        if len(flows) >= _MAX_FLOWS:
            break
    return {"schemaVersion": SCHEMA_VERSION, "flows": flows}


def _map_source_index(sdoc: Dict[str, Any],
                      text_by_file: Dict[str, str]) -> Dict[str, Any]:
    out_files: List[Dict[str, Any]] = []
    for f in sdoc.get("files") or []:
        symbols = [{
            "name": d.get("name"),
            "kind": d.get("kind"),
            "line": int(d.get("line") or 1),
            "snippet": _snippet_or(text_by_file, f["path"],
                                   int(d.get("line") or 1), str(d.get("name"))),
        } for d in sorted(f.get("defs") or [],
                          key=lambda d: (d.get("line", 0), d.get("name", "")))]
        out_files.append({
            "path": f["path"],
            "language": f.get("language") or None,
            "symbols": symbols,
        })
    out_files.sort(key=lambda f: f["path"])
    return {"schemaVersion": SCHEMA_VERSION, "files": out_files}


# ---------------------------------------------------------------------------
# Repository identity
# ---------------------------------------------------------------------------
def _git_commit(repo_root: str) -> Optional[str]:
    """The repo's HEAD commit, only when *repo_root* is itself a git toplevel
    (a nested fixture inside another checkout reports null, not the outer
    repo's commit)."""
    try:
        top = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        if walker.norm(top.stdout.strip()) != walker.norm(str(Path(repo_root).resolve())):
            return None
        head = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        commit = head.stdout.strip()
        return commit if head.returncode == 0 and commit else None
    except (OSError, subprocess.SubprocessError):
        return None


def _generator_version() -> str:
    try:
        from . import __version__
        return __version__
    except ImportError:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def generate_artifacts(repo: str, output: str, name: Optional[str] = None,
                       generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Analyze *repo* and write the ``.openmind`` artifact directory to
    *output*. Returns a small summary dict (counts + paths).

    ``generated_at`` overrides the timestamp for reproducible builds; apart
    from that field, output content is deterministic for identical input.
    """
    repo_path = Path(repo)
    if not repo_path.is_dir():
        raise FileNotFoundError(f"repository directory not found: {repo}")
    repo_root = str(repo_path)
    repo_name = name or walker.service_name(repo_root)
    stamp = generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # read once; feed the same (rel_path, text, hash) triples to both extractors
    triples: List[Tuple[str, str, str]] = []
    text_by_file: Dict[str, str] = {}
    for abs_path in _iter_repo_files(repo_root):
        rel = _relativize(abs_path, repo_root)
        if not rel:
            continue
        text = walker.read_text(abs_path)
        triples.append((rel, text, walker.hash_text(text)))
        text_by_file[rel] = text
    triples.sort(key=lambda t: t[0])

    gdoc = glossary.build_glossary(triples)
    sdoc = structure.build_structure(triples, root="")

    glossary_artifact = _map_glossary(gdoc, sdoc, text_by_file)
    architecture_artifact = _map_architecture(sdoc, repo_name, text_by_file)
    flows_artifact = _map_flows(sdoc, text_by_file)
    source_index_artifact = _map_source_index(sdoc, text_by_file)

    stats = sdoc.get("stats") or {}
    repository = {
        "name": repo_name,
        "root": walker.norm(repo_root),
        "commit": _git_commit(repo_root),
    }
    metadata = {
        "schemaVersion": SCHEMA_VERSION,
        "repository": repository,
        "generatedAt": stamp,
        "summary": {
            "filesIndexed": int(stats.get("file_count") or 0),
            "symbolsIndexed": int(stats.get("definition_count") or 0),
            "artifactsGenerated": len(_ARTIFACT_FILES),
        },
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generator": {"name": GENERATOR_NAME, "version": _generator_version()},
        "generatedAt": stamp,
        "repository": repository,
        "artifacts": dict(_ARTIFACT_FILES),
    }

    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "manifest.json": manifest,
        _ARTIFACT_FILES["metadata"]: metadata,
        _ARTIFACT_FILES["glossary"]: glossary_artifact,
        _ARTIFACT_FILES["architecture"]: architecture_artifact,
        _ARTIFACT_FILES["flows"]: flows_artifact,
        _ARTIFACT_FILES["sourceIndex"]: source_index_artifact,
    }
    for filename, doc in documents.items():
        (out_dir / filename).write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "output": str(out_dir),
        "repository": repository,
        "filesIndexed": metadata["summary"]["filesIndexed"],
        "symbolsIndexed": metadata["summary"]["symbolsIndexed"],
        "glossaryEntries": len(glossary_artifact["entries"]),
        "architectureComponents": len(architecture_artifact["components"]),
        "flows": len(flows_artifact["flows"]),
        "files": sorted(documents.keys()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openmind.artifacts",
        description="Generate the .openmind artifact directory (schema 1.0.0) "
                    "from a local repository. Deterministic, offline, no LLM.")
    parser.add_argument("--repo", required=True,
                        help="Path to the repository root to analyze")
    parser.add_argument("--output", required=True,
                        help="Directory to write the artifacts into (created if missing)")
    parser.add_argument("--name", default=None,
                        help="Repository display name (default: the repo folder name)")
    parser.add_argument("--generated-at", default=None, metavar="ISO8601",
                        help="Override the generatedAt timestamp (reproducible builds)")
    args = parser.parse_args(argv)

    try:
        summary = generate_artifacts(args.repo, args.output, name=args.name,
                                     generated_at=args.generated_at)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Open Mind artifacts written to {summary['output']}")
    print(f"  repository: {summary['repository']['name']} "
          f"(commit: {summary['repository']['commit'] or 'n/a'})")
    print(f"  files indexed:            {summary['filesIndexed']}")
    print(f"  symbols indexed:          {summary['symbolsIndexed']}")
    print(f"  glossary entries:         {summary['glossaryEntries']}")
    print(f"  architecture components:  {summary['architectureComponents']}")
    print(f"  flows:                    {summary['flows']}")
    print(f"  artifact files:           {', '.join(summary['files'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
