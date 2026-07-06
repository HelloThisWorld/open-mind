"""Template facet extraction — deterministic roles + captured facts.

Consumes a RESOLVED template profile (:mod:`openmind.templates`) and the same
``(rel_path, text, hash)`` triples the other extractors read, and produces the
"template facts" document:

- **roles**: each file classified into the first template role whose matcher
  hits (annotations regex > file-name glob > path glob, in template order),
  with the matcher and line that decided it — evidence, not a bare verdict.
- **facts**: every match of a facet's line pattern, its named capture values
  verbatim, and ``{file, line, snippet}`` provenance.

Contract properties, same as the glossary/structure extractors: deterministic
(sorted inputs, stable ordering, no model, no wall clock), honest omission (a
pattern that matches nothing yields nothing — sections are empty, never
invented), and bounded (per-facet cap is RECORDED as ``truncated``, never
silent). This module imports only the stdlib + config/templates so the
artifact export path stays runnable on a bare Python install.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .templates import Template

FACTS_SCHEMA_VERSION = "1.0.0"

_MAX_SNIPPET_CHARS = 200
_MAX_FACTS_PER_FACET = 500


def _glob_hit(value: str, patterns: List[str]) -> Optional[str]:
    v = value.lower()
    for pat in patterns:
        if fnmatch.fnmatch(v, pat.lower()):
            return pat
    return None


def _classify(rel: str, lines: List[str],
              roles: List[Dict[str, Any]],
              compiled: Dict[str, List[re.Pattern]]) -> Optional[Dict[str, Any]]:
    """First template-order role whose matcher hits. Annotation matches carry
    the deciding line; name/path glob matches anchor to line 1."""
    base = rel.rsplit("/", 1)[-1]
    for role in roles:
        for rx in compiled.get(role["name"], []):
            for lineno, line in enumerate(lines, 1):
                if rx.search(line):
                    return {"role": role["name"], "line": lineno,
                            "via": f"annotation:{rx.pattern}"}
        pat = _glob_hit(base, role.get("name_patterns") or [])
        if pat:
            return {"role": role["name"], "line": 1, "via": f"name:{pat}"}
        pat = _glob_hit(rel, role.get("path_globs") or [])
        if pat:
            return {"role": role["name"], "line": 1, "via": f"path:{pat}"}
    return None


def build_facts(files: Iterable[Tuple[str, str, str]],
                template: Template) -> Dict[str, Any]:
    """Build the template-facts document for *files* under *template*.

    ``files`` are ``(rel_path, text, hash)`` triples (the hash is accepted for
    signature symmetry with the other extractors; facts always rebuild in full
    — the pass is a cheap regex scan, and partial carry-over would complicate
    the honesty story for no measurable win).
    """
    role_rx: Dict[str, List[re.Pattern]] = {
        r["name"]: [re.compile(p) for p in (r.get("annotations") or [])]
        for r in template.roles}
    facet_rx: List[Tuple[Dict[str, Any], re.Pattern]] = [
        (f, re.compile(f["pattern"])) for f in template.facets]

    roles: Dict[str, Dict[str, Any]] = {}
    facts: List[Dict[str, Any]] = []
    per_facet: Dict[str, int] = {f["name"]: 0 for f in template.facets}
    truncated: Dict[str, bool] = {f["name"]: False for f in template.facets}

    for rel, text, _hash in sorted(files, key=lambda t: t[0]):
        lines = text.split("\n")

        if template.roles:
            hit = _classify(rel, lines, template.roles, role_rx)
            if hit:
                roles[rel] = hit

        for facet, rx in facet_rx:
            fglobs = facet.get("files") or []
            if fglobs and not _glob_hit(rel, fglobs):
                continue
            for lineno, line in enumerate(lines, 1):
                for m in rx.finditer(line):
                    if per_facet[facet["name"]] >= _MAX_FACTS_PER_FACET:
                        truncated[facet["name"]] = True
                        break
                    captures = facet.get("captures") or []
                    values = {name: (m.group(i + 1) or "")
                              for i, name in enumerate(captures)}
                    facts.append({
                        "facet": facet["name"],
                        "values": values,
                        "file": rel,
                        "line": lineno,
                        "snippet": line.strip()[:_MAX_SNIPPET_CHARS],
                    })
                    per_facet[facet["name"]] += 1
                if truncated[facet["name"]]:
                    break

    role_counts: Dict[str, int] = {}
    for hit in roles.values():
        role_counts[hit["role"]] = role_counts.get(hit["role"], 0) + 1
    role_defs = [{
        "name": r["name"], "title": r["title"], "layer": r.get("layer"),
        "count": role_counts.get(r["name"], 0),
    } for r in template.roles]
    facet_defs = [{
        "name": f["name"], "title": f.get("title") or f["name"],
        "count": per_facet[f["name"]], "truncated": truncated[f["name"]],
    } for f in template.facets]

    return {
        "schemaVersion": FACTS_SCHEMA_VERSION,
        "template": {"name": template.name, "source": template.source,
                     "schemaVersion": template.schema_version},
        "roles": {k: roles[k] for k in sorted(roles)},
        "role_defs": role_defs,
        "facets": facet_defs,
        "facts": facts,
        "stats": {"files_classified": len(roles), "fact_count": len(facts)},
    }


def facts_by_file(facts_doc: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group a facts document's fact records by file (empty dict for None)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for fact in ((facts_doc or {}).get("facts") or []):
        out.setdefault(fact.get("file") or "", []).append(fact)
    return out
