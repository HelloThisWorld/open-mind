"""Read/write the per-project machine-knowledge map files and provide
scope views over them. A scope resolves to one (or more) project ids; the
readers union per project in memory, never physically merging data.

The sole persisted map artifact is the deterministic glossary
(``map/glossary.json``). Generic atomic file I/O is exposed so other
deterministic artifacts can be added the same way."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, machine

_EMPTY = {
    "glossary": {"schema": 0, "terms": {}, "source_hashes": {}, "stats": {}},
    "structure": {"schema": 0, "root": "", "files": [], "modules": {},
                  "definitions": {}, "entry_points": [],
                  "dependency_graph": {"edges": [], "external": []},
                  "call_graph": {"edges": []}, "module_graph": {"edges": []},
                  "source_hashes": {}, "stats": {}},
}


def _path(project_id: str, name: str) -> Path:
    return config.project_map_dir(project_id) / name


def save_map(project_id: str, name: str, data: Dict[str, Any]) -> None:
    config.ensure_project_dirs(project_id)
    p = _path(project_id, name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_map(project_id: str, name: str) -> Dict[str, Any]:
    p = _path(project_id, name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = name.replace(".json", "")
    return json.loads(json.dumps(_EMPTY.get(key, {})))


def save_glossary(pid, d): save_map(pid, "glossary.json", d)
def load_glossary(pid): return load_map(pid, "glossary.json")


def save_structure(pid, d): save_map(pid, "structure.json", d)
def load_structure(pid): return load_map(pid, "structure.json")


# ---------------------------------------------------------------------------
# Scope-union views
# ---------------------------------------------------------------------------
def merged_glossary(project_ids: List[str]) -> Dict[str, Any]:
    """Glossary map for the scope (a single project's map, or a union if several
    ids are passed). First project in the given order wins on a term collision;
    provenance is preserved. Same deterministic lookup-not-similarity contract."""
    terms: Dict[str, Any] = {}
    for pid in project_ids:
        root = machine.project_root(pid)
        for k, e in load_glossary(pid).get("terms", {}).items():
            if k not in terms:
                ent = dict(e, project_id=pid)
                if ent.get("source_file"):   # display + jump-to-source: relative only
                    ent["source_file"] = machine.relativize(ent["source_file"], root)
                terms[k] = ent
    return {"terms": dict(sorted(terms.items())),
            "stats": {"term_count": len(terms)},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())}


def glossary_sources(project_ids: List[str]) -> set:
    """The set of source files the glossary scanned across the scope — used to
    allow-list jump-to-source reads (a file must be a known scanned source).
    Returned RELATIVE (matching what the UI sends); tolerant of legacy absolute
    keys via relativize()."""
    out: set = set()
    for pid in project_ids:
        root = machine.project_root(pid)
        for k in (load_glossary(pid).get("source_hashes") or {}).keys():
            out.add(machine.relativize(k, root))
    return out


def merged_structure(project_ids: List[str]) -> Dict[str, Any]:
    """Structure map for the scope. A structure map is rooted at one repo, so for
    a (typically single-project) scope we return the first project's map that has
    content; absolute file paths are reconstructed by callers from its ``root``."""
    docs = [load_structure(pid) for pid in project_ids]
    for d in docs:
        if d.get("files"):
            d["root"] = ""   # never expose a (legacy) absolute root to readers;
            return d          # files[].path is relative and stays the identity
    if docs:
        docs[0]["root"] = ""
        return docs[0]
    return load_map("", "structure.json")


def structure_sources(project_ids: List[str]) -> set:
    """Relative source paths the structure map scanned across the scope — extends
    the jump-to-source allow-list to every indexed code file (not just glossary
    sources). Keys are stored repo-relative; relativize() also normalizes any
    legacy absolute key so the set matches what the UI sends."""
    out: set = set()
    for pid in project_ids:
        root = machine.project_root(pid)
        for k in (load_structure(pid).get("source_hashes") or {}).keys():
            out.add(machine.relativize(k, root))
    return out
