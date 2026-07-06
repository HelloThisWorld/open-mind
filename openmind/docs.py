"""Generated documentation — the template-driven learning guide.

A template profile's ``guide`` section (template schema 1.2, validated by
:mod:`openmind.templates`) is an ordered outline of sections; each section
runs ONE deterministic query over the learned maps (structure, glossary,
template facts) and renders to markdown. The renderer is bound by the same
contract as every other artifact in this project:

- **No model.** Every line is projected from persisted maps; every fact cites
  ``file:line``; a query that finds nothing renders an honest "nothing found"
  line, never invented prose.
- **Deterministic.** Same maps + same template -> byte-identical pages (no
  timestamps). Regeneration is idempotent and diff-friendly.
- **Honest reset.** Without a resolved template — or with one that has no
  guide — generation keeps its historical no-op semantics: it clears any
  previously generated pages and produces nothing, so the docs surface never
  serves pages from a profile that is no longer selected.
- **Human notes survive.** Each page carries an ``OPENMIND:NOTES`` block that
  is preserved verbatim across regeneration; notes never override facts.

Generation runs automatically after every learn (the gendocs job) and on
demand via ``POST /gendocs``. Pages are served by ``GET /docs`` /
``GET /docs/{page}`` and the MCP ``get_doc`` tool.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, db, mapio, templates

_PAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_NOTES_START = "<!-- OPENMIND:NOTES:START -->"
_NOTES_END = "<!-- OPENMIND:NOTES:END -->"
_EMPTY_LINE = "_Nothing found for this section in the indexed project._"

# per-query default caps (a guide section may override with `limit`)
_DEFAULT_LIMITS = {"entry_points": 30, "flows": 8, "facets": 50,
                   "glossary": 30, "modules": 15, "layers": 8}
_FLOW_MAX_STEPS = 5


def _docs_dir(pid: str) -> Path:
    d = config.project_docs_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Page plumbing: frontmatter + notes preservation
# ---------------------------------------------------------------------------
def _frontmatter(meta: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _extract_notes(text: str) -> str:
    """The verbatim human-notes block body of an existing page ('' if none)."""
    start = text.find(_NOTES_START)
    end = text.find(_NOTES_END)
    if start < 0 or end < 0 or end <= start:
        return ""
    return text[start + len(_NOTES_START):end].strip("\n")


def _notes_block(preserved: str) -> str:
    body = (preserved + "\n") if preserved else ""
    return (f"{_NOTES_START}\n{body}{_NOTES_END}\n"
            "*Human notes above are preserved across regeneration and never "
            "override extracted facts.*")


def _cite(file: str, line: Any = None) -> str:
    return f"`{file}:{int(line)}`" if line else f"`{file}`"


# ---------------------------------------------------------------------------
# Section queries (each: maps -> markdown body lines, honest when empty)
# ---------------------------------------------------------------------------
def _q_overview(sec, sdoc, gdoc, fdoc) -> List[str]:
    stats = sdoc.get("stats") or {}
    tpl = fdoc.get("template") or {}
    langs: Dict[str, int] = {}
    for m in (sdoc.get("modules") or {}).values():
        for lang, n in (m.get("languages") or {}).items():
            langs[lang] = langs.get(lang, 0) + n
    out = []
    if tpl.get("name"):
        out.append(f"- Template profile: **{tpl['name']}** ({tpl.get('source', '')})")
    out.append(f"- Files indexed: {stats.get('file_count', len(sdoc.get('files') or []))}")
    out.append(f"- Definitions extracted: "
               f"{stats.get('definition_count', sum(len(f.get('defs') or []) for f in sdoc.get('files') or []))}")
    if langs:
        top = ", ".join(f"{k} ({v})" for k, v in
                        sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))[:6])
        out.append(f"- Languages by file count: {top}")
    out.append(f"- Modules (directories): {len(sdoc.get('modules') or {})}")
    out.append(f"- Entry points: {len(sdoc.get('entry_points') or [])}")
    out.append(f"- Glossary terms: {len(gdoc.get('terms') or {})}")
    classified = (fdoc.get("stats") or {}).get("files_classified")
    if classified:
        out.append(f"- Files classified into layers: {classified}")
    return out


def _q_layers(sec, sdoc, gdoc, fdoc) -> List[str]:
    roles = fdoc.get("roles") or {}
    files_by_role: Dict[str, List[str]] = {}
    for f, hit in roles.items():
        files_by_role.setdefault(hit.get("role") or "", []).append(f)
    limit = sec.get("limit") or _DEFAULT_LIMITS["layers"]
    out: List[str] = []
    for rd in fdoc.get("role_defs") or []:
        rfiles = sorted(files_by_role.get(rd["name"]) or [])
        if not rfiles:
            continue
        out.append(f"- **{rd.get('title') or rd['name']}** — {len(rfiles)} file(s)")
        for f in rfiles[:limit]:
            hit = roles[f]
            out.append(f"  - {_cite(f, hit.get('line'))} (via {hit.get('via', '?')})")
        if len(rfiles) > limit:
            out.append(f"  - …and {len(rfiles) - limit} more")
    return out


def _q_entry_points(sec, sdoc, gdoc, fdoc) -> List[str]:
    roles = fdoc.get("roles") or {}
    limit = sec.get("limit") or _DEFAULT_LIMITS["entry_points"]
    want_role = sec.get("role") or ""
    out: List[str] = []
    for e in (sdoc.get("entry_points") or [])[: limit * 3]:
        file = e.get("file") or ""
        role = (roles.get(file) or {}).get("role") or ""
        if want_role and role != want_role:
            continue
        tag = f" — {role}" if role else ""
        out.append(f"- {_cite(file, e.get('line'))} ({e.get('kind', 'entry')}{tag})")
        if len(out) >= limit:
            break
    return out


def _q_flows(sec, sdoc, gdoc, fdoc) -> List[str]:
    """Entry flows over the name-based call graph, named from facet captures
    when one exists on the entry file (same idea as the .openmind export)."""
    roles = fdoc.get("roles") or {}
    facts_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for fact in fdoc.get("facts") or []:
        facts_by_file.setdefault(fact.get("file") or "", []).append(fact)
    facet_titles = {f["name"]: (f.get("title") or f["name"])
                    for f in (fdoc.get("facets") or [])}
    definitions = sdoc.get("definitions") or {}
    edges_by_from: Dict[str, List[Dict[str, Any]]] = {}
    for e in (sdoc.get("call_graph") or {}).get("edges") or []:
        edges_by_from.setdefault(e["from"], []).append(e)

    limit = sec.get("limit") or _DEFAULT_LIMITS["flows"]
    want_role = sec.get("role") or ""
    seen: set = set()
    out: List[str] = []
    flows = 0
    for entry in sdoc.get("entry_points") or []:
        file = entry.get("file") or ""
        if file in seen:
            continue
        seen.add(file)
        if want_role and (roles.get(file) or {}).get("role") != want_role:
            continue
        facts = facts_by_file.get(file) or []
        if facts:
            first = facts[0]
            label = " ".join(str(v) for v in first["values"].values() if v).strip()
            title = facet_titles.get(first["facet"], first["facet"])
            name = f"{title}: {label}" if label else f"Entry flow: {file}"
            if label and len(facts) > 1:
                name += f" (+{len(facts) - 1} more)"
        else:
            name = f"Entry flow: {file}"
        out.append(f"- **{name}**")
        out.append(f"  1. Entry point ({entry.get('kind', 'entry')}) at "
                   f"{_cite(file, entry.get('line'))}")
        step = 1
        for e in sorted(edges_by_from.get(file, []),
                        key=lambda e: (e["symbol"], e["to"]))[:_FLOW_MAX_STEPS]:
            site = next((d for d in definitions.get(e["symbol"]) or []
                         if d.get("file") == e["to"]), None)
            if site is None:
                continue
            step += 1
            amb = " (ambiguous target)" if e.get("ambiguous") else ""
            out.append(f"  {step}. Calls `{e['symbol']}` defined at "
                       f"{_cite(e['to'], site.get('line'))}{amb}")
        flows += 1
        if flows >= limit:
            break
    return out


def _q_facets(sec, sdoc, gdoc, fdoc) -> List[str]:
    want = sec.get("facet") or ""
    facet_def = next((f for f in (fdoc.get("facets") or [])
                      if f.get("name") == want), None)
    facts = [f for f in (fdoc.get("facts") or []) if f.get("facet") == want]
    if not facts:
        return []
    limit = sec.get("limit") or _DEFAULT_LIMITS["facets"]
    captures = list(facts[0].get("values") or {})
    header = " | ".join(c.capitalize() for c in captures) or "Value"
    out = [f"| {header} | Where |", "|" + "---|" * (max(len(captures), 1) + 1)]
    for fact in facts[:limit]:
        vals = " | ".join(str(fact["values"].get(c, "")) for c in captures) or "—"
        out.append(f"| {vals} | {_cite(fact['file'], fact['line'])} |")
    if len(facts) > limit:
        out.append(f"\n…and {len(facts) - limit} more "
                   f"{(facet_def or {}).get('title') or want} fact(s).")
    if facet_def and facet_def.get("truncated"):
        out.append("\n_Note: capture was capped at extraction time; counts are "
                   "a lower bound._")
    return out


def _q_glossary(sec, sdoc, gdoc, fdoc) -> List[str]:
    terms = gdoc.get("terms") or {}
    limit = sec.get("limit") or _DEFAULT_LIMITS["glossary"]
    out: List[str] = []
    for term, e in sorted(terms.items())[:limit]:
        definition = (e.get("definition") or "").strip()
        if not definition:
            continue
        cite = (_cite(e["source_file"], e.get("line_number"))
                if e.get("source_file") else "")
        out.append(f"- **{term}** — {definition}" + (f" ({cite})" if cite else ""))
    if len(terms) > limit:
        out.append(f"- …and {len(terms) - limit} more term(s)")
    return out


def _q_modules(sec, sdoc, gdoc, fdoc) -> List[str]:
    mods = sdoc.get("modules") or {}
    limit = sec.get("limit") or _DEFAULT_LIMITS["modules"]
    ranked = sorted(mods.items(), key=lambda kv: (-(kv[1].get("lines") or 0), kv[0]))
    out: List[str] = []
    for path, m in ranked[:limit]:
        langs = ", ".join(sorted((m.get("languages") or {}).keys()))
        out.append(f"- `{path or '.'}` — {m.get('files', 0)} file(s), "
                   f"{m.get('lines', 0)} line(s)" + (f" of {langs}" if langs else ""))
    if len(ranked) > limit:
        out.append(f"- …and {len(ranked) - limit} more module(s)")
    return out


_QUERIES: Dict[str, Callable[..., List[str]]] = {
    "overview": _q_overview, "layers": _q_layers, "entry_points": _q_entry_points,
    "flows": _q_flows, "facets": _q_facets, "glossary": _q_glossary,
    "modules": _q_modules,
}
assert set(_QUERIES) == templates.GUIDE_QUERIES  # renderer covers the contract


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def _render_section(sec: Dict[str, Any], tpl_name: str, order: int,
                    sdoc, gdoc, fdoc, notes: str) -> str:
    body = _QUERIES[sec["query"]](sec, sdoc, gdoc, fdoc)
    parts = [
        _frontmatter({"page": sec["section"], "title": sec["title"],
                      "template": tpl_name, "query": sec["query"],
                      "order": order}),
        "",
        f"# {sec['title']}",
        "",
    ]
    if sec.get("intro"):
        parts += [sec["intro"], ""]
    parts += (body if body else [_EMPTY_LINE])
    parts += ["", _notes_block(notes), ""]
    return "\n".join(parts)


def _render_index(tpl, sections: List[Dict[str, Any]],
                  sdoc, gdoc, fdoc, notes: str) -> str:
    parts = [
        _frontmatter({"page": "index", "title": tpl.title or tpl.name,
                      "template": tpl.name, "query": "index", "order": 0}),
        "",
        f"# {tpl.title or tpl.name} — learning guide",
        "",
        f"Generated from the learned maps through the **{tpl.name}** template "
        "profile. Every fact cites `file:line`; empty sections mean the maps "
        "hold nothing for them — nothing here is model-generated.",
        "",
        "## Contents",
        "",
    ]
    parts += [f"{i}. [{s['title']}]({s['section']}.md)"
              for i, s in enumerate(sections, 1)]
    parts += ["", _notes_block(notes), ""]
    return "\n".join(parts)


def generate(project_id: str, progress=None) -> Dict[str, Any]:
    """Render the learning guide for the project's resolved template profile.

    No resolved template, or no ``guide`` sections -> historical no-op
    semantics: previously generated pages are cleared and nothing is produced,
    so the docs surface honestly reflects the current selection."""
    d = _docs_dir(project_id)
    old_notes: Dict[str, str] = {}
    for f in d.glob("*.md"):
        try:
            old_notes[f.stem] = _extract_notes(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            f.unlink()
        except Exception as exc:
            print(f"[docs] could not delete {f}: {exc}", flush=True)

    tpl = templates.resolve_for_project(db.get_project(project_id))
    sections = list(tpl.guide) if tpl else []
    if not sections:
        if progress:
            progress(0, 0, "")
        return {"pages": [], "count": 0}

    sdoc = mapio.load_structure(project_id)
    gdoc = mapio.load_glossary(project_id)
    fdoc = mapio.load_facts(project_id)

    total = len(sections) + 1
    (d / "index.md").write_text(
        _render_index(tpl, sections, sdoc, gdoc, fdoc, old_notes.get("index", "")),
        encoding="utf-8")
    if progress:
        progress(1, total, "index")
    pages = ["index"]
    for i, sec in enumerate(sections, 1):
        (d / f"{sec['section']}.md").write_text(
            _render_section(sec, tpl.name, i, sdoc, gdoc, fdoc,
                            old_notes.get(sec["section"], "")),
            encoding="utf-8")
        pages.append(sec["section"])
        if progress:
            progress(i + 1, total, sec["section"])
    return {"pages": pages, "count": len(pages)}


# ---------------------------------------------------------------------------
# Serving (scope view: first project with pages, mirroring merged_structure)
# ---------------------------------------------------------------------------
def list_docs(project_ids: List[str]) -> List[Dict[str, Any]]:
    for pid in project_ids:
        d = config.project_docs_dir(pid)
        if not d.is_dir():
            continue
        entries: List[Tuple[int, Dict[str, Any]]] = []
        for f in sorted(d.glob("*.md")):
            try:
                fm = _parse_frontmatter(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            try:
                order = int(fm.get("order") or 999)
            except ValueError:
                order = 999
            entries.append((order, {"page": fm.get("page") or f.stem,
                                    "title": fm.get("title") or f.stem,
                                    "template": fm.get("template") or "",
                                    "project_id": pid}))
        if entries:
            return [e for _, e in sorted(entries, key=lambda t: (t[0], t[1]["page"]))]
    return []


def get_doc(project_ids: List[str], page: str) -> Optional[Dict[str, Any]]:
    page = (page or "").strip().lower()
    if not _PAGE_RE.match(page):
        return None
    for pid in project_ids:
        f = config.project_docs_dir(pid) / f"{page}.md"
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(content)
        return {"page": page, "title": fm.get("title") or page,
                "template": fm.get("template") or "", "content": content,
                "project_id": pid}
    return None
