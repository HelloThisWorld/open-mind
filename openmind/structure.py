"""Stage 2 — FULL-COVERAGE DETERMINISTIC STRUCTURE (the base map).

WHY THIS EXISTS (design intent)
-------------------------------
A coding agent's model carries trained knowledge of a codebase's shape; a local
model does not. We substitute a deterministic, persisted STRUCTURE artifact that
captures that shape exactly, for ANY repository, with no heavy dependencies and
no fixed-architecture assumptions. Embedding retrieval answers "what code is
relevant?"; this map answers "what is actually here and how is it connected?" —
relationships similarity can never guarantee.

It always produces meaningful output for a valid repo — including an
infrastructure/library codebase — because the base layer is built from facts
that exist in every project: a module tree, a per-file definition index, entry
points, an import/dependency graph, and a name-based call/usage graph. With no
recognized language at all, a repo still yields its module tree and file
inventory. There is no path here that returns a bare all-zero result on a
non-empty repository.

HONESTY
-------
The graphs are recovered by the line-oriented heuristics in
:mod:`openmind.langspec` — deterministic static analysis, not a full type
checker. Internal import edges are resolved where the language makes that
tractable; unresolved/external references are recorded as such, never invented.
Call edges are name-based and flagged ``ambiguous`` when a called symbol is
defined in more than one file. Nothing in this map is model-generated.

INCREMENTAL UPKEEP
------------------
:func:`build_structure` reuses the prior artifact keyed by per-file content hash
(the same hash the RAG index maintains): an unchanged file is never re-scanned.
"""
from __future__ import annotations

import posixpath
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import langspec

# Bump when the artifact SHAPE or extraction logic changes — a prior artifact
# written under a different schema is ignored (fully re-extracted), mirroring
# glossary.SCHEMA_VERSION.
SCHEMA_VERSION = 1

# call-site / keyword filtering for the name-based call graph
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "def", "fn",
    "func", "class", "new", "and", "or", "not", "in", "is", "with", "await",
    "yield", "throw", "throws", "try", "else", "elif", "case", "do", "when",
    "match", "use", "import", "from", "as", "print", "println", "len", "range",
    "str", "int", "list", "dict", "set", "super", "self", "this", "typeof",
}
_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_MAX_CALL_EDGES_PER_FILE = 120

_PAT_CACHE: Dict[int, List[Tuple[Any, str]]] = {}
_IMP_CACHE: Dict[int, List[Any]] = {}


def _compiled_defs(lang: langspec.Language) -> List[Tuple[Any, str]]:
    key = id(lang)
    if key not in _PAT_CACHE:
        _PAT_CACHE[key] = [(re.compile(p), kind) for p, kind in lang.defs]
    return _PAT_CACHE[key]


def _compiled_imports(lang: langspec.Language) -> List[Any]:
    key = id(lang)
    if key not in _IMP_CACHE:
        _IMP_CACHE[key] = [re.compile(p) for p in lang.imports]
    return _IMP_CACHE[key]


# ---------------------------------------------------------------------------
# Path normalization (relative to repo root -> portable, trace-free artifacts)
# ---------------------------------------------------------------------------
def _rel(path: str, root: str) -> str:
    p = path.replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and (p == r or p.startswith(r + "/")):
        p = p[len(r):].lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _dir_of(path: str) -> str:
    d = posixpath.dirname(path)
    return d or "."


# ---------------------------------------------------------------------------
# Comment-aware code-line iteration (so defs/imports in comments don't count)
# ---------------------------------------------------------------------------
def _code_lines(text: str, lang: langspec.Language) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    bopen, bclose = lang.block_comment or ("", "")
    in_block = False
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw
        if in_block:
            end = line.find(bclose) if bclose else -1
            if end == -1:
                continue
            line = line[end + len(bclose):]
            in_block = False
        if bopen:
            while True:
                s = line.find(bopen)
                if s == -1:
                    break
                e = line.find(bclose, s + len(bopen))
                if e == -1:
                    line = line[:s]
                    in_block = True
                    break
                line = line[:s] + " " + line[e + len(bclose):]
        for lc in lang.line_comments:
            idx = line.find(lc)
            if idx != -1:
                line = line[:idx]
        if line.strip():
            out.append((i, line))
    return out


# ---------------------------------------------------------------------------
# Python imports need real semantics (relative dots + multiline parens), which a
# single declarative regex cannot express — so Python gets a small dedicated
# extractor that yields absolute, resolvable module keys.
# ---------------------------------------------------------------------------
_PY_FROM_PAREN = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]*\(([^)]*)\)", re.M | re.S)
_PY_FROM_LINE = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+([^(\n]+)$", re.M)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)


def _python_imports(rel_path: str, text: str) -> List[str]:
    pkg = posixpath.dirname(rel_path).replace("/", ".")
    out: List[str] = []

    def _rel_base(dots: str) -> str:
        base = pkg
        for _ in range(len(dots) - 1):
            base = base.rsplit(".", 1)[0] if "." in base else ""
        return base

    def _emit_from(dots: str, sub: str, names: str) -> None:
        if not dots:
            if sub:
                out.append(sub)               # absolute: from a.b import c -> a.b
            return
        base = _rel_base(dots)
        if sub:
            out.append(base + "." + sub if base else sub)   # from .mod import x
            return
        for chunk in names.replace("(", " ").replace(")", " ").split(","):
            tok = chunk.strip().split()       # from . import a, b as c -> sibling modules
            nm = tok[0] if tok else ""
            if nm and nm != "*":
                out.append(base + "." + nm if base else nm)

    for m in _PY_FROM_PAREN.finditer(text):
        _emit_from(m.group(1), m.group(2), m.group(3))
    for m in _PY_FROM_LINE.finditer(text):
        _emit_from(m.group(1), m.group(2), m.group(3))
    for m in _PY_IMPORT.finditer(text):
        for part in m.group(1).split(","):
            if part.strip():
                out.append(part.strip())
    return out


# ---------------------------------------------------------------------------
# Per-file scan (the unit that is cached by hash)
# ---------------------------------------------------------------------------
def _scan_file(rel_path: str, text: str, file_hash: str) -> Dict[str, Any]:
    lang = langspec.language_for(rel_path)
    rec: Dict[str, Any] = {
        "path": rel_path, "hash": file_hash,
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "language": lang.name if lang else (langspec.ext_of(rel_path).lstrip(".") or "text"),
        "defs": [], "imports_raw": [], "entries": [], "calls": [], "package": "",
    }
    if lang is None:
        return rec
    code = _code_lines(text, lang)
    def_compiled = _compiled_defs(lang)
    imp_compiled = _compiled_imports(lang)
    def_lines: set = set()
    for lineno, line in code:
        for rx, kind in def_compiled:
            m = rx.match(line)
            if m:
                rec["defs"].append({"name": m.group(1), "kind": kind, "line": lineno})
                def_lines.add(lineno)
                break
        for pat, kind in lang.entrypoints:
            if re.search(pat, line):
                rec["entries"].append({"kind": kind, "line": lineno})
    # imports: Python needs relative/multiline semantics; others are line-regex
    if lang.name == "Python":
        rec["imports_raw"] = _python_imports(rel_path, text)
    else:
        for lineno, line in code:
            for rx in imp_compiled:
                m = rx.search(line)
                if m:
                    rec["imports_raw"].append(m.group(1))
    # declared package (dotted-style languages) for cross-file import resolution
    if lang.import_style == "dotted":
        pm = re.search(r"^[ \t]*(?:package|namespace)[ \t]+([\w.]+)", text, re.M)
        if pm:
            rec["package"] = pm.group(1)
    # call sites (skip definition lines + keywords); names filtered to project later
    called: set = set()
    for lineno, line in code:
        if lineno in def_lines:
            continue
        for m in _CALL_RE.finditer(line):
            name = m.group(1)
            if name not in _KEYWORDS:
                called.add(name)
    rec["calls"] = sorted(called)
    return rec


# ---------------------------------------------------------------------------
# Import resolution (best-effort, per language style; honest about misses)
# ---------------------------------------------------------------------------
def _build_indexes(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    proj_paths = {f["path"] for f in files}
    py_modules: Dict[str, str] = {}
    dotted_index: Dict[str, str] = {}    # "package.Symbol" -> file (Java/C#/Kotlin)
    dotted_packages: set = set()
    for f in files:
        p = f["path"]
        if p.endswith(".py"):
            key = p[:-3]
            if key.endswith("/__init__"):
                key = key[: -len("/__init__")]
            py_modules[key.replace("/", ".")] = p
        if f.get("package"):
            stem = p.replace("\\", "/").rsplit("/", 1)[-1]
            cls = stem.rsplit(".", 1)[0]
            dotted_index[f["package"] + "." + cls] = p
            dotted_packages.add(f["package"])
    return {"proj_paths": proj_paths, "py_modules": py_modules,
            "dotted_index": dotted_index, "dotted_packages": dotted_packages}


def _resolve_import(target: str, from_file: str, lang: langspec.Language,
                    idx: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Return (to_file_or_None, internal?). internal=True with to_file=None means
    the reference resolves inside the project but not to a single file (a package/
    crate-internal module)."""
    style = lang.import_style
    if style == "dotted":
        if target in idx["dotted_index"]:
            return idx["dotted_index"][target], True
        base = target[:-2] if target.endswith(".*") else target
        # python-style dotted resolution against module keys
        if target in idx["py_modules"]:
            return idx["py_modules"][target], True
        for mod, fp in idx["py_modules"].items():
            if mod == base or mod.startswith(base + "."):
                return (fp if mod == base else None), True
        if base in idx["dotted_packages"] or any(
                pk.startswith(base + ".") for pk in idx["dotted_packages"]):
            return None, True
        return None, False
    if style == "relative":
        if not target.startswith("."):
            return None, False     # bare specifier -> external (node_modules)
        cand = posixpath.normpath(posixpath.join(_dir_of(from_file), target))
        for ext in ("",) + _TS_EXTS:
            if cand + ext in idx["proj_paths"]:
                return cand + ext, True
            if (cand + "/index" + ext) in idx["proj_paths"]:
                return cand + "/index" + ext, True
        return None, True          # relative intent, file unresolved
    if style == "include":
        cand = posixpath.normpath(posixpath.join(_dir_of(from_file), target))
        if cand in idx["proj_paths"]:
            return cand, True
        return None, False
    if style == "colons":
        head = target.split("::", 1)[0]
        if head in ("crate", "self", "super"):
            return None, True
        return None, False
    return None, False             # path/namespaced/none: treated as external


# ---------------------------------------------------------------------------
# Build (incremental, hash-keyed)
# ---------------------------------------------------------------------------
def build_structure(files: Iterable[Tuple[str, str, str]], root: str = "",
                    prior: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build (or incrementally refresh) the structure artifact.

    ``files`` is an iterable of (path, text, content_hash). Paths are relativized
    to ``root``. Returns the deterministic base map described in the module
    docstring.
    """
    prior = prior or {}
    if prior.get("schema") != SCHEMA_VERSION:
        prior = {}     # shape/logic changed -> ignore old artifact, fully re-extract
    prior_hashes: Dict[str, str] = prior.get("source_hashes") or {}
    prior_files: Dict[str, Dict[str, Any]] = {f["path"]: f for f in (prior.get("files") or [])}

    file_recs: List[Dict[str, Any]] = []
    source_hashes: Dict[str, str] = {}
    reused = scanned = 0
    for path, text, file_hash in files:
        rel = _rel(path, root)
        source_hashes[rel] = file_hash
        if prior_hashes.get(rel) == file_hash and rel in prior_files:
            file_recs.append(prior_files[rel]); reused += 1
        else:
            file_recs.append(_scan_file(rel, text, file_hash)); scanned += 1
    file_recs.sort(key=lambda f: f["path"])

    idx = _build_indexes(file_recs)
    # global definition index: symbol -> [{file, kind, line}]
    definitions: Dict[str, List[Dict[str, Any]]] = {}
    def_owner: Dict[str, set] = {}
    for f in file_recs:
        for d in f["defs"]:
            definitions.setdefault(d["name"], []).append(
                {"file": f["path"], "kind": d["kind"], "line": d["line"]})
            def_owner.setdefault(d["name"], set()).add(f["path"])

    # dependency graph (internal import edges) + external summary
    dep_edges: List[Dict[str, Any]] = []
    external: Dict[str, int] = {}
    for f in file_recs:
        lang = langspec.language_for(f["path"])
        if lang is None:
            continue
        seen: set = set()
        for target in f["imports_raw"]:
            to_file, internal = _resolve_import(target, f["path"], lang, idx)
            if internal and to_file and to_file != f["path"]:
                sig = (f["path"], to_file)
                if sig not in seen:
                    seen.add(sig)
                    dep_edges.append({"from": f["path"], "to": to_file,
                                      "target": target, "kind": "import"})
            elif not internal:
                external[target] = external.get(target, 0) + 1

    # call/usage graph (name-based, cross-file; ambiguous flagged)
    call_edges: List[Dict[str, Any]] = []
    for f in file_recs:
        emitted = 0
        seen: set = set()
        for name in f["calls"]:
            owners = def_owner.get(name)
            if not owners:
                continue
            ambiguous = len(owners) > 1
            for to_file in sorted(owners):
                if to_file == f["path"]:
                    continue
                sig = (f["path"], to_file, name)
                if sig in seen:
                    continue
                seen.add(sig)
                call_edges.append({"from": f["path"], "to": to_file,
                                   "symbol": name, "ambiguous": ambiguous})
                emitted += 1
                if emitted >= _MAX_CALL_EDGES_PER_FILE:
                    break
            if emitted >= _MAX_CALL_EDGES_PER_FILE:
                break

    # module (directory) rollup + module-level graph
    modules: Dict[str, Dict[str, Any]] = {}
    for f in file_recs:
        d = _dir_of(f["path"])
        m = modules.setdefault(d, {"path": d, "files": 0, "lines": 0, "languages": {}})
        m["files"] += 1
        m["lines"] += f["lines"]
        m["languages"][f["language"]] = m["languages"].get(f["language"], 0) + 1
    mod_edge_w: Dict[Tuple[str, str], int] = {}
    for e in dep_edges + call_edges:
        a, b = _dir_of(e["from"]), _dir_of(e["to"])
        if a != b:
            mod_edge_w[(a, b)] = mod_edge_w.get((a, b), 0) + 1
    module_edges = [{"from": a, "to": b, "weight": w}
                    for (a, b), w in sorted(mod_edge_w.items())]

    # entry points (flattened, with the symbol on the same/next line when useful)
    entry_points: List[Dict[str, Any]] = []
    for f in file_recs:
        for e in f["entries"]:
            entry_points.append({"file": f["path"], "kind": e["kind"], "line": e["line"]})
    entry_points.sort(key=lambda e: (e["kind"] != "main", e["file"], e["line"]))

    external_top = sorted(external.items(), key=lambda kv: (-kv[1], kv[0]))[:50]
    code_files = [f for f in file_recs if langspec.language_for(f["path"])]
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "root": root,
        "files": file_recs,
        "modules": dict(sorted(modules.items())),
        "definitions": dict(sorted(definitions.items())),
        "entry_points": entry_points,
        "dependency_graph": {"edges": dep_edges,
                             "external": [{"module": k, "count": v} for k, v in external_top]},
        "call_graph": {"edges": call_edges},
        "module_graph": {"edges": module_edges},
        "source_hashes": source_hashes,
        "stats": {
            "file_count": len(file_recs),
            "code_file_count": len(code_files),
            "total_lines": sum(f["lines"] for f in file_recs),
            "definition_count": sum(len(v) for v in definitions.values()),
            "module_count": len(modules),
            "entry_point_count": len(entry_points),
            "internal_dep_edges": len(dep_edges),
            "external_dependencies": len(external),
            "call_edges": len(call_edges),
            "languages": sorted({f["language"] for f in code_files}),
            "sources_scanned": scanned, "sources_reused": reused,
        },
    }


# ---------------------------------------------------------------------------
# Query-time accessors
# ---------------------------------------------------------------------------
def get_definition(doc: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Resolve a symbol to its definition site(s) from the persisted index."""
    hits = (doc.get("definitions") or {}).get(symbol)
    if not hits:
        return {"found": False, "symbol": symbol,
                "message": "symbol not defined in the indexed project"}
    return {"found": True, "symbol": symbol, "definitions": hits}


def term_usage(doc: Dict[str, Any], term: str, source_file: str = "") -> Dict[str, Any]:
    """Grounded usage profile for a glossary term, derived ENTIRELY from the
    deterministic structure map + the term's own provenance — never generated. If
    the term is also a code symbol it reports where it is DEFINED (file:line:kind)
    and every file that REFERENCES it (a call/usage site). For a pure concept /
    acronym that is not a code symbol the code lists are honestly empty, but the
    module it lives in (from its definition source) is still shown. All paths are
    repo-relative and link back to real source; nothing is fabricated."""
    defs = (doc.get("definitions") or {}).get(term) or []   # [{file, kind, line}]
    used_in: List[str] = []
    for f in (doc.get("files") or []):
        if term in (f.get("calls") or []):
            used_in.append(f["path"])
    used_in = sorted(set(used_in))
    mods = {_dir_of(d["file"]) for d in defs} | {_dir_of(p) for p in used_in}
    if source_file:
        mods.add(_dir_of(source_file.replace("\\", "/")))
    return {
        "term": term,
        "is_code_symbol": bool(defs),
        "defined_at": defs[:25],            # definition site(s), traceable to file:line
        "used_in": used_in[:50],            # files that reference the symbol
        "use_count": len(used_in),
        "modules": sorted(m for m in mods if m)[:20],
    }


def overview(doc: Dict[str, Any]) -> Dict[str, Any]:
    """A compact, display/grounding-friendly slice of the structure map."""
    s = doc.get("stats", {})
    return {
        "stats": s,
        "entry_points": doc.get("entry_points", [])[:50],
        "top_modules": sorted(
            (doc.get("modules") or {}).values(),
            key=lambda m: (-m["files"], m["path"]))[:25],
    }
