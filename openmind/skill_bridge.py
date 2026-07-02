"""Skill verification bridge — expose the deterministic capability skills to an
external eval harness over a JSON-lines stdin/stdout protocol.

WHY THIS EXISTS
---------------
The capability skills under ``skills/`` (glossary, code-graphs, capability-router)
promise deterministic, source-traceable, non-fabricating behaviour. That promise
should be *measured* by an independent harness, not just asserted. This bridge lets
`agent-skill-verification-template <https://github.com/HelloThisWorld/agent-skill-verification-template>`_
run the REAL Open Mind implementation (not a reimplementation) against a fixture
corpus, N times per test case, and grade every run with its schema / citation /
unsupported-claim / tool-call validators. The harness independently re-reads every
``file:line`` this bridge returns — a fabricated citation fails the gate.

PROTOCOL
--------
Start:   ``python -m openmind.skill_bridge --root <corpus-dir>``
         On startup the corpus is walked once and the glossary + structure
         artifacts are built (deterministically); a ready line is printed:
         ``{"ready": true, "files": N, "terms": N, "definitions": N}``
Request: one JSON object per line on stdin:
         ``{"id": <int>, "op": "glossary"|"usage"|"definition"|"route", "arg": "<str>"}``
Reply:   one JSON object per line on stdout:
         ``{"id": <int>, "ok": true, "result": {...}}`` or
         ``{"id": <int>, "ok": false, "error": "<message>"}``

Ops (all deterministic; ``route`` runs with ``use_model=False`` — the pure floor):
  glossary    exact-token term lookup  -> entry {found, term, definition,
              source_file, line_number, snippet, ...}
  usage       grounded usage profile   -> {is_code_symbol, defined_at[+snippet],
              used_in, use_count, modules}
  definition  symbol definition sites  -> {found, definitions: [{file, kind,
              line, snippet}]}
  route       capability routing trace -> {capability, decided_by,
              deterministic_fallback, reason, capabilities}

The bridge never touches the app's data directory, database, vector store, or any
model: everything is computed in memory from the corpus passed on the command line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import glossary, router, structure

# Corpus files considered: the same doc/code surface the app itself scans.
_EXTS = {".py", ".md", ".markdown", ".rst", ".txt", ".java", ".ts", ".js",
         ".go", ".rs", ".rb", ".yml", ".yaml", ".toml", ".sh", ".c", ".h", ".cpp"}


def _walk_corpus(root: Path) -> List[Tuple[str, str, str]]:
    """Collect (rel_posix_path, text, sha1) for every corpus file, sorted for
    deterministic build order."""
    files: List[Tuple[str, str, str]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _EXTS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        sha = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        files.append((rel, text, sha))
    return files


class _Corpus:
    """The corpus + the two deterministic artifacts built from it, plus line
    access so replies can carry the exact cited line's text (snippet)."""

    def __init__(self, root: Path) -> None:
        self.files = _walk_corpus(root)
        self.lines: Dict[str, List[str]] = {
            rel: text.splitlines() for rel, text, _ in self.files}
        self.glossary_doc = glossary.build_glossary(self.files)
        self.structure_doc = structure.build_structure(self.files, root="")

    def snippet(self, rel: str, line_number: Optional[int]) -> str:
        lines = self.lines.get(rel or "", [])
        if not line_number or line_number < 1 or line_number > len(lines):
            return ""
        return lines[line_number - 1]


def _op_glossary(c: _Corpus, term: str) -> Dict[str, Any]:
    entry = glossary.get_glossary(c.glossary_doc, term)
    if entry.get("found"):
        entry["snippet"] = c.snippet(entry.get("source_file"),
                                     entry.get("line_number"))
    return entry


def _op_usage(c: _Corpus, term: str) -> Dict[str, Any]:
    usage = structure.term_usage(c.structure_doc, term)
    for d in usage.get("defined_at", []):
        d["snippet"] = c.snippet(d.get("file"), d.get("line"))
    return usage


def _op_definition(c: _Corpus, symbol: str) -> Dict[str, Any]:
    res = structure.get_definition(c.structure_doc, symbol)
    for d in res.get("definitions", []) if res.get("found") else []:
        d["snippet"] = c.snippet(d.get("file"), d.get("line"))
    return res


def _op_route(_c: _Corpus, query: str) -> Dict[str, Any]:
    # use_model=False: the deterministic floor — the only mode an offline,
    # reproducible eval can meaningfully gate on.
    return router.route(query, use_model=False)


_OPS = {"glossary": _op_glossary, "usage": _op_usage,
        "definition": _op_definition, "route": _op_route}


def serve(root: Path) -> None:
    corpus = _Corpus(root)
    out = sys.stdout
    print(json.dumps({
        "ready": True,
        "files": len(corpus.files),
        "terms": corpus.glossary_doc["stats"]["term_count"],
        "definitions": corpus.structure_doc["stats"]["definition_count"],
    }), flush=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        rid: Any = None
        try:
            req = json.loads(raw)
            rid = req.get("id")
            op = _OPS.get(req.get("op") or "")
            if op is None:
                raise ValueError(f"unknown op: {req.get('op')!r}")
            result = op(corpus, str(req.get("arg") or ""))
            reply = {"id": rid, "ok": True, "result": result}
        except Exception as exc:  # reply per-request; the server keeps serving
            reply = {"id": rid, "ok": False, "error": str(exc)}
        print(json.dumps(reply, ensure_ascii=False, default=str), file=out,
              flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True,
                    help="corpus directory the skills answer questions about")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(json.dumps({"ready": False,
                          "error": f"corpus root not found: {root}"}),
              flush=True)
        sys.exit(2)
    serve(root)


if __name__ == "__main__":
    main()
