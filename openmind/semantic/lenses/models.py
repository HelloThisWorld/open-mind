"""The closed Lens schema (2.0.0), its validator and the safe-pattern gate.

A lens definition is DATA — a bounded, declarative JSON document. The
validator here is what makes model-induced lenses safe to even store:

* only the known sections and keys exist (unknown anything → error);
* every regular expression must compile, stay under a hard length, and use
  none of the dangerous features (lookbehind, backreferences, conditionals)
  that enable catastrophic backtracking or expression trickery;
* every glob is charset-checked; no pattern may embed a URL;
* list sizes and total definition size are capped;
* nothing executable can be expressed at all — there is no key whose value
  is ever evaluated.

The same JSON Schema handed to a provider for induction (strict mode) is
generated from the same section table, so the model is asked for exactly the
shape the validator accepts.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from ..tasks import REGISTRY as TASK_REGISTRY

LENS_SCHEMA_VERSION = "2.0.0"

# Hard bounds (spec §26: cap pattern lengths and counts).
MAX_PATTERN_CHARS = 200
MAX_LIST_ITEMS = 32
MAX_ROLES = 24
MAX_IDENTIFIERS = 24
MAX_DEFINITION_BYTES = 100_000
MAX_NAME_CHARS = 80
MAX_TEXT_CHARS = 500

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
#: Regex features refused outright: lookbehind, backreferences, conditional
#: groups, recursion, comments. Conservative on purpose — a lens pattern is a
#: matcher, not a program.
_DANGEROUS_RE = re.compile(r"\(\?<|\\[1-9]|\(\?\(|\(\?P=|\(\?R|\(\?#")
_GLOB_OK_RE = re.compile(r"^[A-Za-z0-9_\-./*?\[\]!{},+@ ]+$")

_IDENTIFIER_KINDS = frozenset({
    "requirement", "business-rule", "decision", "constraint", "interface",
    "acceptance-criterion", "failure-mode", "data-model", "workflow",
    "test-case", "ticket", "error-code", "facet", "other",
})
_RELATION_TYPES = frozenset({
    "refines", "implements", "partially-implements", "configures",
    "verifies", "supersedes", "derived-from", "affected-by", "contradicts",
    "possibly-related",
})

_TOP_KEYS = frozenset({
    "schemaVersion", "name", "title", "description", "match", "roles",
    "identifiers", "documentPatterns", "semanticTasks", "relationHints",
    "validation", "sampleEvidenceIds",
})
_MATCH_KEYS = frozenset({"languages", "dependencies", "markerFiles",
                         "pathGlobs", "documentTitlePatterns",
                         "documentTypes"})
_ROLE_KEYS = frozenset({"name", "title", "pathGlobs", "namePatterns",
                        "annotations"})
_IDENT_KEYS = frozenset({"name", "kind", "pattern", "examples"})
_DOCPAT_KEYS = frozenset({"name", "headingPatterns", "tableHeaders"})
_SEMTASK_KEYS = frozenset({"task", "includeRoles", "includeAssetTypes",
                           "includeBlockTypes"})
_RELHINT_KEYS = frozenset({"sourceType", "targetType", "candidateRelation",
                           "signals"})
_VALIDATION_KEYS = frozenset({"minimumAssetCoverage", "maximumRoleOverlap"})


# ---------------------------------------------------------------------------
# Safe-pattern checks
# ---------------------------------------------------------------------------
def check_regex(pattern: Any, where: str, errors: List[str]) -> str:
    p = str(pattern or "")
    if not p:
        errors.append(f"{where}: empty pattern")
        return ""
    if len(p) > MAX_PATTERN_CHARS:
        errors.append(f"{where}: pattern exceeds {MAX_PATTERN_CHARS} chars")
        return ""
    if "://" in p:
        errors.append(f"{where}: pattern must not contain a URL")
        return ""
    if _DANGEROUS_RE.search(p):
        errors.append(f"{where}: pattern uses a disallowed regex feature "
                      f"(lookbehind/backreference/conditional/recursion)")
        return ""
    try:
        re.compile(p)
    except re.error as exc:
        errors.append(f"{where}: pattern does not compile: {exc}")
        return ""
    return p


def check_glob(pattern: Any, where: str, errors: List[str]) -> str:
    p = str(pattern or "")
    if not p:
        errors.append(f"{where}: empty glob")
        return ""
    if len(p) > MAX_PATTERN_CHARS:
        errors.append(f"{where}: glob exceeds {MAX_PATTERN_CHARS} chars")
        return ""
    if "://" in p or not _GLOB_OK_RE.match(p):
        errors.append(f"{where}: glob contains disallowed characters")
        return ""
    return p


def _text(value: Any, where: str, errors: List[str],
          required: bool = False) -> str:
    v = str(value or "").strip()
    if required and not v:
        errors.append(f"{where} is required")
    if len(v) > MAX_TEXT_CHARS:
        errors.append(f"{where} exceeds {MAX_TEXT_CHARS} chars")
        return v[:MAX_TEXT_CHARS]
    if "://" in v:
        errors.append(f"{where} must not contain a URL")
    return v


def _str_list(value: Any, where: str, errors: List[str],
              item_check=None) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{where} must be a list of strings")
        return []
    if len(value) > MAX_LIST_ITEMS:
        errors.append(f"{where} exceeds {MAX_LIST_ITEMS} items")
        value = value[:MAX_LIST_ITEMS]
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{where}[{i}] must be a string")
            continue
        if item_check is not None:
            cleaned = item_check(item, f"{where}[{i}]", errors)
            if cleaned:
                out.append(cleaned)
        else:
            cleaned = _text(item, f"{where}[{i}]", errors)
            if cleaned:
                out.append(cleaned)
    return out


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------
def validate_lens_definition(data: Any, *, source: str
                             ) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Validate + normalize one lens definition.

    Returns ``(normalized, errors, warnings)``. A definition with errors is
    stored/listable but can never be approved or activated.
    """
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return {}, ["lens definition must be a JSON object"], []
    try:
        size = len(json.dumps(data))
    except (TypeError, ValueError):
        return {}, ["lens definition is not JSON-serializable"], []
    if size > MAX_DEFINITION_BYTES:
        return {}, [f"lens definition exceeds {MAX_DEFINITION_BYTES} bytes"], []

    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {', '.join(unknown)}")

    version = str(data.get("schemaVersion") or "")
    if not version.startswith("2."):
        errors.append(f"schemaVersion must be 2.x, got {version!r}")

    name = str(data.get("name") or "").strip().lower()
    if not name or not _SLUG_RE.match(name) or len(name) > MAX_NAME_CHARS:
        errors.append("name is required: a lowercase slug")

    normalized: Dict[str, Any] = {
        "schemaVersion": version or LENS_SCHEMA_VERSION,
        "name": name,
        "title": _text(data.get("title"), "title", errors),
        "description": _text(data.get("description"), "description", errors),
    }

    # -- match --------------------------------------------------------------
    match_raw = data.get("match") or {}
    if not isinstance(match_raw, dict):
        errors.append("match must be an object")
        match_raw = {}
    unknown = sorted(set(match_raw) - _MATCH_KEYS)
    if unknown:
        errors.append(f"match: unknown keys {', '.join(unknown)}")
    normalized["match"] = {
        "languages": _str_list(match_raw.get("languages"),
                               "match.languages", errors),
        "dependencies": _str_list(match_raw.get("dependencies"),
                                  "match.dependencies", errors),
        "markerFiles": _str_list(match_raw.get("markerFiles"),
                                 "match.markerFiles", errors,
                                 item_check=check_glob),
        "pathGlobs": _str_list(match_raw.get("pathGlobs"),
                               "match.pathGlobs", errors,
                               item_check=check_glob),
        "documentTitlePatterns": _str_list(
            match_raw.get("documentTitlePatterns"),
            "match.documentTitlePatterns", errors, item_check=check_regex),
        "documentTypes": _str_list(match_raw.get("documentTypes"),
                                   "match.documentTypes", errors),
    }

    # -- roles --------------------------------------------------------------
    roles_raw = data.get("roles") or []
    if not isinstance(roles_raw, list):
        errors.append("roles must be a list")
        roles_raw = []
    if len(roles_raw) > MAX_ROLES:
        errors.append(f"roles exceed {MAX_ROLES} entries")
        roles_raw = roles_raw[:MAX_ROLES]
    roles: List[Dict[str, Any]] = []
    seen_roles: set = set()
    for i, role in enumerate(roles_raw):
        where = f"roles[{i}]"
        if not isinstance(role, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(role) - _ROLE_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")
        rname = str(role.get("name") or "").strip().lower()
        if not rname or not _SLUG_RE.match(rname):
            errors.append(f"{where}.name is required (lowercase slug)")
            continue
        if rname in seen_roles:
            errors.append(f"duplicate role name {rname!r}")
        seen_roles.add(rname)
        entry = {
            "name": rname,
            "title": _text(role.get("title"), f"{where}.title", errors),
            "pathGlobs": _str_list(role.get("pathGlobs"),
                                   f"{where}.pathGlobs", errors,
                                   item_check=check_glob),
            "namePatterns": _str_list(role.get("namePatterns"),
                                      f"{where}.namePatterns", errors,
                                      item_check=check_regex),
            "annotations": _str_list(role.get("annotations"),
                                     f"{where}.annotations", errors,
                                     item_check=check_regex),
        }
        if not (entry["pathGlobs"] or entry["namePatterns"]
                or entry["annotations"]):
            errors.append(f"{where} needs at least one matcher")
        roles.append(entry)
    normalized["roles"] = roles

    # -- identifiers ----------------------------------------------------------
    idents_raw = data.get("identifiers") or []
    if not isinstance(idents_raw, list):
        errors.append("identifiers must be a list")
        idents_raw = []
    if len(idents_raw) > MAX_IDENTIFIERS:
        errors.append(f"identifiers exceed {MAX_IDENTIFIERS} entries")
        idents_raw = idents_raw[:MAX_IDENTIFIERS]
    identifiers: List[Dict[str, Any]] = []
    for i, ident in enumerate(idents_raw):
        where = f"identifiers[{i}]"
        if not isinstance(ident, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(ident) - _IDENT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")
        kind = str(ident.get("kind") or "").strip().lower()
        if kind not in _IDENTIFIER_KINDS:
            errors.append(f"{where}.kind {kind!r} is not a known identifier "
                          f"kind")
        pattern = check_regex(ident.get("pattern"), f"{where}.pattern",
                              errors)
        entry = {
            "name": _text(ident.get("name"), f"{where}.name", errors,
                          required=True),
            "kind": kind, "pattern": pattern,
            "examples": _str_list(ident.get("examples"),
                                  f"{where}.examples", errors),
        }
        if pattern:
            compiled = re.compile(pattern)
            for j, example in enumerate(entry["examples"]):
                if not compiled.search(example):
                    warnings.append(f"{where}.examples[{j}] does not match "
                                    f"its own pattern")
        identifiers.append(entry)
    normalized["identifiers"] = identifiers

    # -- documentPatterns ------------------------------------------------------
    docpats_raw = data.get("documentPatterns") or []
    if not isinstance(docpats_raw, list):
        errors.append("documentPatterns must be a list")
        docpats_raw = []
    docpats = []
    for i, pat in enumerate(docpats_raw[:MAX_LIST_ITEMS]):
        where = f"documentPatterns[{i}]"
        if not isinstance(pat, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(pat) - _DOCPAT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")
        docpats.append({
            "name": _text(pat.get("name"), f"{where}.name", errors,
                          required=True),
            "headingPatterns": _str_list(pat.get("headingPatterns"),
                                         f"{where}.headingPatterns", errors,
                                         item_check=check_regex),
            "tableHeaders": _str_list(pat.get("tableHeaders"),
                                      f"{where}.tableHeaders", errors),
        })
    normalized["documentPatterns"] = docpats

    # -- semanticTasks ---------------------------------------------------------
    tasks_raw = data.get("semanticTasks") or []
    if not isinstance(tasks_raw, list):
        errors.append("semanticTasks must be a list")
        tasks_raw = []
    sem_tasks = []
    for i, entry in enumerate(tasks_raw[:MAX_LIST_ITEMS]):
        where = f"semanticTasks[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(entry) - _SEMTASK_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")
        task_name = str(entry.get("task") or "").strip().lower()
        if task_name not in TASK_REGISTRY:
            errors.append(f"{where}.task {task_name!r} is not a registered "
                          f"semantic task")
        sem_tasks.append({
            "task": task_name,
            "includeRoles": _str_list(entry.get("includeRoles"),
                                      f"{where}.includeRoles", errors),
            "includeAssetTypes": _str_list(entry.get("includeAssetTypes"),
                                           f"{where}.includeAssetTypes",
                                           errors),
            "includeBlockTypes": _str_list(entry.get("includeBlockTypes"),
                                           f"{where}.includeBlockTypes",
                                           errors),
        })
        for role in sem_tasks[-1]["includeRoles"]:
            if role not in seen_roles:
                warnings.append(f"{where} includes unknown role {role!r}")
    normalized["semanticTasks"] = sem_tasks

    # -- relationHints ---------------------------------------------------------
    hints_raw = data.get("relationHints") or []
    if not isinstance(hints_raw, list):
        errors.append("relationHints must be a list")
        hints_raw = []
    hints = []
    for i, hint in enumerate(hints_raw[:MAX_LIST_ITEMS]):
        where = f"relationHints[{i}]"
        if not isinstance(hint, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(hint) - _RELHINT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")
        relation = str(hint.get("candidateRelation") or "").strip().lower()
        if relation not in _RELATION_TYPES:
            errors.append(f"{where}.candidateRelation {relation!r} is not a "
                          f"relation-candidate type")
        hints.append({
            "sourceType": _text(hint.get("sourceType"),
                                f"{where}.sourceType", errors, required=True),
            "targetType": _text(hint.get("targetType"),
                                f"{where}.targetType", errors, required=True),
            "candidateRelation": relation,
            "signals": _str_list(hint.get("signals"), f"{where}.signals",
                                 errors),
        })
    normalized["relationHints"] = hints

    # -- validation thresholds -------------------------------------------------
    val_raw = data.get("validation") or {}
    if not isinstance(val_raw, dict):
        errors.append("validation must be an object")
        val_raw = {}
    unknown = sorted(set(val_raw) - _VALIDATION_KEYS)
    if unknown:
        errors.append(f"validation: unknown keys {', '.join(unknown)}")
    def _fraction(key: str, default: float) -> float:
        value = val_raw.get(key, default)
        try:
            f = float(value)
        except (TypeError, ValueError):
            errors.append(f"validation.{key} must be a number")
            return default
        if not (0.0 <= f <= 1.0):
            errors.append(f"validation.{key} must be between 0 and 1")
            return default
        return f
    normalized["validation"] = {
        "minimumAssetCoverage": _fraction("minimumAssetCoverage", 0.0),
        "maximumRoleOverlap": _fraction("maximumRoleOverlap", 1.0),
    }

    # -- sampleEvidenceIds (induced provenance) --------------------------------
    sample_ids = _str_list(data.get("sampleEvidenceIds"),
                           "sampleEvidenceIds", errors)
    if source == "induced" and not sample_ids:
        errors.append("an induced lens must reference its sampleEvidenceIds")
    normalized["sampleEvidenceIds"] = sample_ids

    return normalized, errors, warnings


# ---------------------------------------------------------------------------
# The provider-facing JSON Schema (strict mode) for induction
# ---------------------------------------------------------------------------
def lens_json_schema() -> Dict[str, Any]:
    def arr(items: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "array", "items": items}

    def s() -> Dict[str, Any]:
        return {"type": "string"}

    def obj(props: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "object", "properties": props,
                "required": sorted(props), "additionalProperties": False}

    return obj({
        "schemaVersion": s(), "name": s(), "title": s(), "description": s(),
        "match": obj({"languages": arr(s()), "dependencies": arr(s()),
                      "markerFiles": arr(s()), "pathGlobs": arr(s()),
                      "documentTitlePatterns": arr(s()),
                      "documentTypes": arr(s())}),
        "roles": arr(obj({"name": s(), "title": s(), "pathGlobs": arr(s()),
                          "namePatterns": arr(s()),
                          "annotations": arr(s())})),
        "identifiers": arr(obj({"name": s(), "kind": s(), "pattern": s(),
                                "examples": arr(s())})),
        "documentPatterns": arr(obj({"name": s(),
                                     "headingPatterns": arr(s()),
                                     "tableHeaders": arr(s())})),
        "semanticTasks": arr(obj({"task": s(), "includeRoles": arr(s()),
                                  "includeAssetTypes": arr(s()),
                                  "includeBlockTypes": arr(s())})),
        "relationHints": arr(obj({"sourceType": s(), "targetType": s(),
                                  "candidateRelation": s(),
                                  "signals": arr(s())})),
        "validation": obj({"minimumAssetCoverage": {"type": "number"},
                           "maximumRoleOverlap": {"type": "number"}}),
        "sampleEvidenceIds": arr(s()),
    })


__all__ = ["LENS_SCHEMA_VERSION", "validate_lens_definition",
           "lens_json_schema", "check_regex", "check_glob",
           "MAX_PATTERN_CHARS", "MAX_DEFINITION_BYTES"]
