"""Template profiles — declarative framework/architecture lenses (schema 1.0.0).

A template profile is a small YAML/JSON file that tells Open Mind how to LOOK AT
a repository: which stack it matches (``match``), which logical roles files/
classes play (``roles``), which extra deterministic facts to capture
(``facets``), and how a guided learning doc should be organised (``guide``).

THIS MODULE IS THE ONLY ENTRY POINT to templates. The rest of the app talks to
four functions — ``list_templates`` / ``get_template`` / ``auto_select`` /
``resolve_for_project`` — and NOTHING changes behavior unless a template
resolves: no template selected means every consumer sees ``None`` and behaves
exactly as before templates existed.

All four sections are ACTIVE: ``match`` drives auto-selection at learn time
(schema 1.0); ``roles`` and ``facets`` are validated here and consumed by
:mod:`openmind.facets` (extraction), the ``.openmind`` export, and the
structure/graph API (schema 1.1); ``guide`` is an ordered outline of learning
-doc sections — each a deterministic query over the learned maps — validated
here and rendered by :mod:`openmind.docs` (schema 1.2).

Sources, in precedence order (same name -> user file wins):
  1. ``config.USER_TEMPLATES_DIR``   (<data dir>/templates/*.yaml|yml|json)
  2. ``config.BUILTIN_TEMPLATES_DIR`` (shipped with the package)

Everything here is deterministic: no model, no network, stable ordering, and a
selection result that carries the EVIDENCE it matched on (which dependencies,
languages, marker files) — never a bare verdict.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config

SCHEMA_MAJOR = 1                      # accepted template schemaVersion major
DEFAULT_MIN_SCORE = 3                 # auto-select floor unless match.min_score says otherwise

# scoring weights — documented so template authors can reason about min_score:
# one dependency cue alone clears the default floor; language or marker
# evidence alone does not (too generic to pick a lens from).
_W_DEPENDENCY = 3                     # per matched match.dependencies pattern
_W_PRIMARY_LANGUAGE = 2               # match.languages hit on the primary language
_W_OTHER_LANGUAGE = 1                 # match.languages hit on a non-primary language
_W_MARKER_FILE = 2                    # per matched match.marker_files pattern

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# role/facet names are identifier-like (they key fact records), so unlike
# template names they may also use underscores
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TEMPLATE_EXTS = (".yaml", ".yml", ".json")
_MATCH_KEYS = {"dependencies", "languages", "marker_files", "min_score"}
_ROLE_KEYS = {"name", "title", "layer", "annotations", "name_patterns", "path_globs"}
_FACET_KEYS = {"name", "title", "pattern", "captures", "files"}
_GUIDE_KEYS = {"section", "title", "query", "role", "facet", "limit", "intro"}
_TOP_KEYS = {"schemaVersion", "name", "title", "description",
             "match", "roles", "facets", "guide"}

# the deterministic queries a guide section may run over the learned maps
# (rendered by openmind.docs; every query cites file:line or reports an
# honest empty — never generated prose)
GUIDE_QUERIES = {"overview", "layers", "entry_points", "flows", "facets",
                 "glossary", "modules"}


@dataclass
class Template:
    """A parsed template file. ``valid`` templates are usable; invalid ones are
    still listed (with their errors) so authors can see WHY a file was rejected
    instead of it silently disappearing."""
    name: str
    title: str = ""
    description: str = ""
    source: str = "builtin"           # "builtin" | "user"
    path: str = ""
    schema_version: str = ""
    match: Dict[str, Any] = field(default_factory=dict)
    roles: List[Dict[str, Any]] = field(default_factory=list)
    facets: List[Dict[str, Any]] = field(default_factory=list)
    guide: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def summary(self) -> Dict[str, Any]:
        return {"name": self.name, "title": self.title or self.name,
                "description": self.description, "source": self.source,
                "schema_version": self.schema_version, "valid": self.valid,
                "errors": list(self.errors)}


# ---------------------------------------------------------------------------
# Loading + schema validation
# ---------------------------------------------------------------------------
def _load_raw(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a template file into a dict. Returns (data, error)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"unreadable file: {exc}"
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except Exception as exc:
            return None, f"invalid JSON: {exc}"
    else:
        try:
            import yaml  # optional at runtime: JSON templates work without it
        except Exception:
            return None, ("PyYAML is not installed — install it or provide this "
                          "template as .json")
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            return None, f"invalid YAML: {exc}"
    if not isinstance(data, dict):
        return None, "template root must be a mapping/object"
    return data, None


def _str_list(value: Any, where: str, errors: List[str]) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        errors.append(f"{where} must be a list of strings")
        return []
    return [v.strip() for v in value if v.strip()]


def _section_list(value: Any, where: str, errors: List[str]) -> List[Dict[str, Any]]:
    """Shape gate shared by the section validators: a section is a list of
    mappings (or absent)."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        errors.append(f"{where} must be a list of mappings")
        return []
    return value


def _validate_roles(value: Any, errors: List[str]) -> List[Dict[str, Any]]:
    """Active contract: each role classifies files into a logical layer via at
    least one matcher. Order matters — the first matching role wins — so the
    normalized list preserves template order."""
    items = _section_list(value, "roles", errors)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for i, r in enumerate(items):
        where = f"roles[{i}]"
        unknown = sorted(set(r) - _ROLE_KEYS)
        if unknown:
            errors.append(f"{where} unknown keys: {', '.join(unknown)}")
        name = str(r.get("name") or "").strip().lower()
        if not name or not _SLUG_RE.match(name):
            errors.append(f"{where}.name is required (lowercase slug)")
            continue
        if name in seen:
            errors.append(f"duplicate role name '{name}'")
        seen.add(name)
        layer = r.get("layer")
        if layer is not None and (not isinstance(layer, int) or layer < 1):
            errors.append(f"{where}.layer must be a positive integer")
            layer = None
        annotations = _str_list(r.get("annotations"), f"{where}.annotations", errors)
        for pat in annotations:
            try:
                re.compile(pat)
            except re.error as exc:
                errors.append(f"{where}.annotations pattern {pat!r} does not "
                              f"compile: {exc}")
        role = {"name": name,
                "title": str(r.get("title") or "").strip() or name,
                "layer": layer if isinstance(layer, int) else None,
                "annotations": annotations,
                "name_patterns": _str_list(r.get("name_patterns"),
                                           f"{where}.name_patterns", errors),
                "path_globs": _str_list(r.get("path_globs"),
                                        f"{where}.path_globs", errors)}
        if not (role["annotations"] or role["name_patterns"] or role["path_globs"]):
            errors.append(f"{where} needs at least one matcher "
                          "(annotations / name_patterns / path_globs)")
        out.append(role)
    return out


def _validate_facets(value: Any, errors: List[str]) -> List[Dict[str, Any]]:
    """Active contract: each facet is a line regex whose capture groups map
    1:1 onto named ``captures``; every match becomes a fact with file:line
    evidence. The regex must compile at load time — a bad pattern fails HERE,
    visibly, not silently at the next learn."""
    items = _section_list(value, "facets", errors)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for i, f in enumerate(items):
        where = f"facets[{i}]"
        unknown = sorted(set(f) - _FACET_KEYS)
        if unknown:
            errors.append(f"{where} unknown keys: {', '.join(unknown)}")
        name = str(f.get("name") or "").strip().lower()
        if not name or not _SLUG_RE.match(name):
            errors.append(f"{where}.name is required (lowercase slug)")
            continue
        if name in seen:
            errors.append(f"duplicate facet name '{name}'")
        seen.add(name)
        pattern = str(f.get("pattern") or "")
        captures = _str_list(f.get("captures"), f"{where}.captures", errors)
        if not pattern:
            errors.append(f"{where}.pattern is required")
        else:
            try:
                groups = re.compile(pattern).groups
                if groups != len(captures):
                    errors.append(f"{where}: pattern has {groups} capture "
                                  f"group(s) but captures names {len(captures)}"
                                  " — they must match 1:1")
            except re.error as exc:
                errors.append(f"{where}.pattern does not compile: {exc}")
        out.append({"name": name,
                    "title": str(f.get("title") or "").strip() or name,
                    "pattern": pattern,
                    "captures": captures,
                    "files": _str_list(f.get("files"), f"{where}.files", errors)})
    return out


def _validate_guide(value: Any, errors: List[str]) -> List[Dict[str, Any]]:
    """Active contract: an ordered outline of learning-doc sections. Each
    section runs ONE deterministic query over the learned maps; the renderer
    (openmind.docs) cites file:line for every fact and reports honest empties,
    so a guide can never make the docs say more than the maps support."""
    items = _section_list(value, "guide", errors)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for i, g in enumerate(items):
        where = f"guide[{i}]"
        unknown = sorted(set(g) - _GUIDE_KEYS)
        if unknown:
            errors.append(f"{where} unknown keys: {', '.join(unknown)}")
        section = str(g.get("section") or "").strip().lower()
        if not section or not _SLUG_RE.match(section):
            errors.append(f"{where}.section is required (lowercase slug; it "
                          "becomes the page name)")
            continue
        if section in seen or section == "index":
            errors.append(f"duplicate or reserved guide section '{section}'")
        seen.add(section)
        title = str(g.get("title") or "").strip()
        if not title:
            errors.append(f"{where}.title is required")
        query = str(g.get("query") or "").strip().lower()
        if query not in GUIDE_QUERIES:
            errors.append(f"{where}.query must be one of: "
                          f"{', '.join(sorted(GUIDE_QUERIES))}")
        facet = str(g.get("facet") or "").strip().lower()
        if query == "facets" and not facet:
            errors.append(f"{where}: query 'facets' requires a facet name")
        if facet and not _SLUG_RE.match(facet):
            errors.append(f"{where}.facet must be a lowercase slug")
        role = str(g.get("role") or "").strip().lower()
        if role and not _SLUG_RE.match(role):
            errors.append(f"{where}.role must be a lowercase slug")
        limit = g.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            errors.append(f"{where}.limit must be a positive integer")
            limit = None
        out.append({"section": section, "title": title, "query": query,
                    "facet": facet, "role": role,
                    "limit": limit if isinstance(limit, int) else None,
                    "intro": str(g.get("intro") or "").strip()})
    return out


def _parse(data: Dict[str, Any], path: Path, source: str) -> Template:
    errors: List[str] = []

    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {', '.join(unknown)}")

    ver = str(data.get("schemaVersion") or "")
    m = re.match(r"^(\d+)\.\d+(\.\d+)?$", ver)
    if not m:
        errors.append("schemaVersion is required, e.g. \"1.0.0\"")
    elif int(m.group(1)) != SCHEMA_MAJOR:
        errors.append(f"unsupported schemaVersion major {m.group(1)} "
                      f"(this build accepts {SCHEMA_MAJOR}.x)")

    name = str(data.get("name") or "").strip()
    if not name or not _NAME_RE.match(name):
        errors.append("name is required: lowercase letters/digits/hyphens "
                      "(e.g. \"spring-boot\")")
        name = name or path.stem.lower()

    description = str(data.get("description") or "").strip()
    if not description:
        errors.append("description is required (one line shown in the picker)")

    match_raw = data.get("match")
    match: Dict[str, Any] = {}
    if match_raw is not None and not isinstance(match_raw, dict):
        errors.append("match must be a mapping")
    elif isinstance(match_raw, dict):
        unknown_m = sorted(set(match_raw) - _MATCH_KEYS)
        if unknown_m:
            errors.append(f"unknown match keys: {', '.join(unknown_m)}")
        match["dependencies"] = _str_list(match_raw.get("dependencies"),
                                          "match.dependencies", errors)
        match["languages"] = _str_list(match_raw.get("languages"),
                                       "match.languages", errors)
        match["marker_files"] = _str_list(match_raw.get("marker_files"),
                                          "match.marker_files", errors)
        ms = match_raw.get("min_score")
        if ms is not None and (not isinstance(ms, int) or ms < 1):
            errors.append("match.min_score must be a positive integer")
        elif ms is not None:
            match["min_score"] = ms

    return Template(
        name=name,
        title=str(data.get("title") or "").strip(),
        description=description,
        source=source,
        path=str(path),
        schema_version=ver,
        match=match,
        roles=_validate_roles(data.get("roles"), errors),
        facets=_validate_facets(data.get("facets"), errors),
        guide=_validate_guide(data.get("guide"), errors),
        errors=errors,
    )


def _scan_dir(directory: Path, source: str) -> List[Template]:
    if not directory.is_dir():
        return []
    out: List[Template] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _TEMPLATE_EXTS:
            continue
        data, err = _load_raw(p)
        if err:
            out.append(Template(name=p.stem.lower(), source=source, path=str(p),
                                errors=[err]))
            continue
        out.append(_parse(data, p, source))
    return out


def _load_all() -> Dict[str, Template]:
    """All templates by name; a user template shadows a builtin of the same name.
    Duplicate names WITHIN one source are flagged invalid rather than silently
    picking one. Re-reads from disk on every call — template files are tiny and
    editing one must take effect without a restart."""
    by_name: Dict[str, Template] = {}
    for source, directory in (("builtin", config.BUILTIN_TEMPLATES_DIR),
                              ("user", config.USER_TEMPLATES_DIR)):
        seen_here: Dict[str, Template] = {}
        for t in _scan_dir(directory, source):
            if t.name in seen_here:
                t.errors.append(f"duplicate template name '{t.name}' in {source} "
                                f"templates (also defined by "
                                f"{Path(seen_here[t.name].path).name})")
            seen_here[t.name] = t
            by_name[t.name] = t       # later source (user) wins by name
    return by_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_templates() -> List[Dict[str, Any]]:
    """Summaries of every discovered template (valid AND invalid — invalid ones
    carry their errors so authors can fix them), deterministically ordered."""
    try:
        config.USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass                          # listing still works from builtins alone
    return [t.summary() for _, t in sorted(_load_all().items())]


def get_template(name: str) -> Optional[Template]:
    """The named template, or None if unknown or invalid."""
    if not name:
        return None
    t = _load_all().get(str(name).strip().lower())
    return t if (t and t.valid) else None


def auto_select(detection: Dict[str, Any],
                rel_files: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Score every valid template's ``match`` section against a detect()
    artifact + the walked repo-relative file list. Returns the best selection
    WITH its evidence, or None when nothing clears its score floor:

        {"name": ..., "score": int, "matched": {"dependencies": [...],
         "languages": [...], "marker_files": [...]}}

    Deterministic: ties break on template name.
    """
    detection = detection or {}
    deps = [str(d).lower()
            for man in (detection.get("manifests") or [])
            for d in (man.get("dependencies") or [])]
    primary = str(detection.get("primary_language") or "").lower()
    lang_names = {str(l.get("language") or "").lower()
                  for l in (detection.get("languages") or [])}
    files = [str(f).replace("\\", "/").lower() for f in (rel_files or [])]

    best: Optional[Dict[str, Any]] = None
    for name, t in sorted(_load_all().items()):
        if not t.valid or not t.match:
            continue
        score = 0
        matched: Dict[str, List[str]] = {"dependencies": [], "languages": [],
                                         "marker_files": []}
        for pat in t.match.get("dependencies", []):
            if any(pat.lower() in d for d in deps):
                score += _W_DEPENDENCY
                matched["dependencies"].append(pat)
        for lang in t.match.get("languages", []):
            if lang.lower() == primary:
                score += _W_PRIMARY_LANGUAGE
                matched["languages"].append(lang)
            elif lang.lower() in lang_names:
                score += _W_OTHER_LANGUAGE
                matched["languages"].append(lang)
        for pat in t.match.get("marker_files", []):
            if any(fnmatch.fnmatch(f, pat.lower()) for f in files):
                score += _W_MARKER_FILE
                matched["marker_files"].append(pat)
        floor = t.match.get("min_score", DEFAULT_MIN_SCORE)
        if score >= floor and (best is None or score > best["score"]):
            best = {"name": name, "score": score, "matched": matched}
    return best


def resolve_for_project(project: Optional[Dict[str, Any]]) -> Optional[Template]:
    """The template a consumer should apply for this project, or None (= behave
    exactly as if templates did not exist).

    Precedence: the user's explicit override (meta["template"]) wins over the
    recorded auto-selection (meta["template_auto"]). An override naming a
    missing/invalid template resolves to None — honest failure, NOT a silent
    fall-back to auto, because the user said "use THIS one"."""
    meta = dict((project or {}).get("meta") or {})
    override = str(meta.get("template") or "").strip()
    if override:
        return get_template(override)
    auto = str(meta.get("template_auto") or "").strip()
    if auto:
        return get_template(auto)
    return None


def selection_info(project: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The selection state for the API/UI: what the user chose, what detection
    recorded, which one is effective, and why an override is not working."""
    meta = dict((project or {}).get("meta") or {})
    override = str(meta.get("template") or "").strip() or None
    auto = str(meta.get("template_auto") or "").strip() or None
    override_error = None
    effective = None
    effective_source = None
    if override:
        if get_template(override):
            effective, effective_source = override, "override"
        else:
            override_error = (f"template '{override}' is not available "
                              "(missing or invalid) — clear the override or fix "
                              "the template file")
    elif auto and get_template(auto):
        effective, effective_source = auto, "auto"
    return {"override": override, "override_error": override_error,
            "auto": auto, "auto_score": meta.get("template_auto_score"),
            "effective": effective, "effective_source": effective_source}
