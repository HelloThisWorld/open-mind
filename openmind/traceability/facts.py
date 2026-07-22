"""Deterministic comparable-fact extraction and normalization.

Conflict detectors compare TYPED FACTS, never arbitrary prose. This module
is the only place facts come from, and it is a closed set of extractors:

* structured ``attributes`` maps stored in claim metadata (promoted Phase 4
  candidates carry them);
* strict patterns over claim statements for CLOSED properties (timeout /
  latency with explicit units, retry / maximum counts, HTTP method + path
  declarations, ``key=value`` configuration forms, column/field type
  declarations, boolean obligations in closed forms);
* interface entity canonical keys (``interface:POST:/name-check``);
* configuration entity canonical keys.

A statement matching none of these produces NO fact — arbitrary prose is
never compared. A number without an explicit unit keeps ``unit: ""`` and a
comparison against a united value is ``not-comparable``, which is silence,
not a conflict. Units are NEVER guessed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..knowledge import store as kg
from ..knowledge.vocabularies import EntityType, GraphLifecycleStatus
from .models import ComparableFact
from .vocabularies import ComparableValueType

MAX_FACTS_PER_WORKSPACE = 20_000
MAX_CLAIMS_SCANNED = 10_000

#: Closed property-alias map: differently-worded statements of the SAME
#: closed property normalize to one property name so they can be compared.
#: This is a lookup table, not language understanding.
PROPERTY_ALIASES = {
    "timeout": "timeout",
    "time-out": "timeout",
    "latency": "latency",
    "maximum latency": "latency",
    "max latency": "latency",
    "response time": "latency",
    "acceptance threshold": "latency",
    "threshold": "latency",
    "retries": "retries",
    "retry count": "retries",
    "maximum retries": "retries",
    "max retries": "retries",
    "attempts": "retries",
    "maximum attempts": "retries",
}

_DURATION_UNITS = {
    "ms": 1, "msec": 1, "millisecond": 1, "milliseconds": 1,
    "s": 1000, "sec": 1000, "second": 1000, "seconds": 1000,
    "min": 60_000, "minute": 60_000, "minutes": 60_000,
}
_SIZE_UNITS = {
    "byte": 1, "bytes": 1, "b": 1,
    "kb": 1024, "kilobyte": 1024, "kilobytes": 1024,
    "mb": 1024 * 1024, "megabyte": 1024 * 1024, "megabytes": 1024 * 1024,
}
_BOOLEAN_SYNONYMS = {
    "true": True, "yes": True, "enabled": True, "on": True,
    "mandatory": True,
    "false": False, "no": False, "disabled": False, "off": False,
    "optional": False,
}
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

#: SQL/JSON type spelling normalization (case + common synonyms; parameters
#: preserved). Closed table — an unknown spelling stays verbatim
#: (lower-cased) rather than being guessed at.
_TYPE_SYNONYMS = {
    "int": "integer", "int4": "integer", "bigint": "bigint",
    "bool": "boolean", "number": "number", "string": "string",
    "text": "text", "float": "decimal", "double": "decimal",
    "numeric": "decimal",
}

# -- statement patterns (strict; closed properties only) ---------------------
_TIMEOUT_RE = re.compile(
    r"(?i)\b(timeout|time-out|latency|maximum latency|max latency|"
    r"response time|acceptance threshold|threshold)\b"
    r"(?:\s+(?:is|of|=|:)?\s*|\s+(?:must|shall)?\s*(?:be|answer|complete|"
    r"respond)?\s*(?:within)?\s+)"
    r"(\d+(?:\.\d+)?)\s*"
    r"(ms|msec|milliseconds?|s|sec|seconds?|min|minutes?)?\b")
_COUNT_RE = re.compile(
    r"(?i)\b(?:(maximum|max)\s+)?(retries|retry count|attempts)\b"
    r"\s*(?:is|of|=|:)?\s*(\d+)\b")
_RETRIED_RE = re.compile(
    r"(?i)\bretried\s+(\d+|one|two|three|four|five)\s+times?\b")
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ENDPOINT_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/[A-Za-z0-9_\-./{}]*)")
_CONFIG_RE = re.compile(
    r"(?<![\w.])([a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+)\s*=\s*"
    r"([A-Za-z0-9._\-]+)")
_FIELD_TYPE_RE = re.compile(
    r"(?i)\b(?:column|field)\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:is|has type|type)\s+([A-Za-z]+(?:\(\d+(?:,\s*\d+)?\))?)")
_BOOLEAN_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9_.-]{2,})\s+(?:is|must be)\s+"
    r"(enabled|disabled|true|false|on|off|mandatory|optional)\b")


def normalize_property(name: str) -> str:
    clean = re.sub(r"\s+", " ", str(name or "").strip().lower())
    return PROPERTY_ALIASES.get(clean, clean)


def normalize_api_path(path: str) -> str:
    clean = str(path or "").strip()
    clean = re.sub(r"/{2,}", "/", clean)
    if len(clean) > 1 and clean.endswith("/"):
        clean = clean[:-1]
    return clean


def normalize_data_type(value: str) -> str:
    clean = str(value or "").strip().lower()
    base = re.match(r"([a-z]+)(\(.*\))?$", clean)
    if not base:
        return clean
    name = _TYPE_SYNONYMS.get(base.group(1), base.group(1))
    return name + (base.group(2) or "")


def _literal_value(raw: str) -> Dict[str, Any]:
    """Type a bare literal (configuration values): integer / decimal /
    boolean / identifier. No unit is ever attached."""
    text = str(raw or "").strip()
    lowered = text.lower()
    if lowered in _BOOLEAN_SYNONYMS:
        return {"value": _BOOLEAN_SYNONYMS[lowered],
                "value_type": ComparableValueType.BOOLEAN}
    if re.fullmatch(r"-?\d+", text):
        return {"value": int(text),
                "value_type": ComparableValueType.INTEGER}
    if re.fullmatch(r"-?\d+\.\d+", text):
        return {"value": float(text),
                "value_type": ComparableValueType.DECIMAL}
    return {"value": text, "value_type": ComparableValueType.IDENTIFIER}


def facts_from_statement(statement: str) -> List[Dict[str, Any]]:
    """Extract the closed-property facts one claim statement contains.
    Everything unmatched is silence."""
    facts: List[Dict[str, Any]] = []
    text = str(statement or "")

    for match in _TIMEOUT_RE.finditer(text):
        prop, number, unit = match.groups()
        record: Dict[str, Any] = {
            "property": normalize_property(prop),
            "raw_value": number, "raw_unit": unit or "",
        }
        if unit:
            factor = _DURATION_UNITS.get(unit.strip().lower())
            if factor is None:
                continue
            record["value"] = (float(number) * factor
                               if "." in number else int(number) * factor)
            record["unit"] = "ms"
            record["value_type"] = ComparableValueType.DURATION
        else:
            # No unit -> a bare number, deliberately NOT a duration.
            record["value"] = (float(number) if "." in number
                               else int(number))
            record["unit"] = ""
            record["value_type"] = ComparableValueType.INTEGER
        record["operator"] = "="
        facts.append(record)

    for match in _COUNT_RE.finditer(text):
        _maximum, prop, number = match.groups()
        facts.append({
            "property": normalize_property(prop),
            "value": int(number), "unit": "",
            "value_type": ComparableValueType.COUNT,
            "operator": "=", "raw_value": number, "raw_unit": "",
        })
    for match in _RETRIED_RE.finditer(text):
        number = match.group(1).lower()
        value = _WORD_NUMBERS.get(number)
        if value is None:
            value = int(number)
        facts.append({
            "property": "retries", "value": value, "unit": "",
            "value_type": ComparableValueType.COUNT,
            "operator": "=", "raw_value": match.group(1), "raw_unit": "",
        })

    for match in _ENDPOINT_RE.finditer(text):
        method, path = match.groups()
        normalized_path = normalize_api_path(path)
        facts.append({
            "property": "http-method",
            "subject_override": normalized_path,
            "value": method.upper(), "unit": "",
            "value_type": ComparableValueType.HTTP_METHOD,
            "operator": "=", "raw_value": f"{method} {path}",
            "raw_unit": "",
        })

    for match in _CONFIG_RE.finditer(text):
        key, raw = match.groups()
        typed = _literal_value(raw)
        facts.append({
            "property": "configuration-value",
            "subject_override": key.strip().lower(),
            "operator": "=", "raw_value": raw, "raw_unit": "",
            **typed,
            "unit": "",
        })

    for match in _FIELD_TYPE_RE.finditer(text):
        field_name, type_name = match.groups()
        facts.append({
            "property": "data-type",
            "subject_override": field_name.strip().lower(),
            "value": normalize_data_type(type_name), "unit": "",
            "value_type": ComparableValueType.DATA_TYPE,
            "operator": "=", "raw_value": type_name, "raw_unit": "",
        })

    for match in _BOOLEAN_RE.finditer(text):
        subject, literal = match.groups()
        if subject.strip().lower() in ("it", "this", "that"):
            continue
        facts.append({
            "property": "boolean-obligation",
            "subject_override": subject.strip().lower(),
            "value": _BOOLEAN_SYNONYMS[literal.lower()], "unit": "",
            "value_type": ComparableValueType.BOOLEAN,
            "operator": "=", "raw_value": literal, "raw_unit": "",
        })
    return facts


def facts_from_attributes(attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Facts from a structured attributes map (promoted candidate
    metadata). Only closed keys with literal values; nothing free-form."""
    facts: List[Dict[str, Any]] = []
    for key, raw in (attributes or {}).items():
        prop = normalize_property(key)
        if prop in ("timeout", "latency", "retries"):
            typed = _literal_value(str(raw))
            if typed["value_type"] in (ComparableValueType.INTEGER,
                                       ComparableValueType.DECIMAL):
                facts.append({
                    "property": prop, "operator": "=", "unit": "",
                    "raw_value": str(raw), "raw_unit": "", **typed})
    return facts


# ---------------------------------------------------------------------------
# Workspace collection
# ---------------------------------------------------------------------------
def collect_comparable_facts(workspace_id: str) -> List[ComparableFact]:
    """Every comparable fact of a workspace's ACTIVE canonical claims and
    interface/configuration entity keys. Bounded and deterministic."""
    facts: List[ComparableFact] = []
    entities_by_id: Dict[str, Dict[str, Any]] = {}

    claims = kg.list_claims(workspace_id,
                            lifecycle_status=GraphLifecycleStatus.ACTIVE,
                            limit=MAX_CLAIMS_SCANNED)
    for claim in claims:
        entity = entities_by_id.get(claim["entity_id"])
        if entity is None:
            entity = kg.get_entity(workspace_id, claim["entity_id"])
            entities_by_id[claim["entity_id"]] = entity or {}
        if not entity or entity.get("lifecycle_status") != \
                GraphLifecycleStatus.ACTIVE:
            continue
        evidence = kg.claim_evidence(claim["id"])
        evidence_id = evidence[0]["evidence_id"] if evidence else ""
        quote = evidence[0]["quote"] if evidence else ""
        authority = claim["authority_status"]
        if authority == "unknown":
            authority = entity.get("authority_status", "unknown")
        raw_facts = facts_from_statement(claim["statement"])
        raw_facts.extend(facts_from_attributes(
            (claim.get("metadata") or {}).get("attributes") or {}))
        for raw in raw_facts:
            subject = raw.pop("subject_override",
                              entity.get("canonical_key", ""))
            facts.append(ComparableFact(
                subject_key=subject,
                property=raw["property"],
                operator=raw.get("operator", "="),
                value=raw["value"], unit=raw.get("unit", ""),
                value_type=raw["value_type"],
                source_claim_id=claim["id"],
                source_entity_id=claim["entity_id"],
                evidence_id=evidence_id, quote=quote,
                authority_status=authority,
                raw_value=str(raw.get("raw_value", "")),
                raw_unit=str(raw.get("raw_unit", ""))))
            if len(facts) >= MAX_FACTS_PER_WORKSPACE:
                return facts

    # Interface entity keys: interface:<METHOD>:<path>
    for entity in kg.list_entities(workspace_id,
                                   entity_type=EntityType.INTERFACE,
                                   lifecycle_status="active", limit=2000):
        parts = entity["canonical_key"].split(":", 2)
        if len(parts) == 3 and parts[1].upper() in _HTTP_METHODS:
            facts.append(ComparableFact(
                subject_key=normalize_api_path(parts[2]),
                property="http-method", operator="=",
                value=parts[1].upper(), unit="",
                value_type=ComparableValueType.HTTP_METHOD,
                source_entity_id=entity["id"],
                authority_status=entity["authority_status"],
                raw_value=entity["canonical_key"]))

    # Configuration entity keys: configuration:asset:<id>:<key> — the key
    # itself; the configured VALUE comes from the entity's claims (already
    # scanned above; a configuration claim "namecheck.timeout=5000" yields
    # the key=value fact with the config-key subject).
    if len(facts) > MAX_FACTS_PER_WORKSPACE:
        facts = facts[:MAX_FACTS_PER_WORKSPACE]
    return facts


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def compare_facts(a: ComparableFact, b: ComparableFact) -> str:
    """``equal`` / ``different`` / ``not-comparable``. Different value
    types, or a united value against a unitless one, are NOT comparable —
    a unit is never guessed."""
    if a.property != b.property:
        return "not-comparable"
    if a.value_type != b.value_type:
        # integer vs count are both bare numbers of the same dimension
        bare = {ComparableValueType.INTEGER, ComparableValueType.COUNT}
        if not (a.value_type in bare and b.value_type in bare):
            return "not-comparable"
    if (a.unit or b.unit) and a.unit != b.unit:
        return "not-comparable"
    if a.value_type == ComparableValueType.DECIMAL or \
            b.value_type == ComparableValueType.DECIMAL:
        try:
            return ("equal" if float(a.value) == float(b.value)
                    else "different")
        except (TypeError, ValueError):
            return "not-comparable"
    return "equal" if a.value == b.value else "different"


__all__ = [
    "PROPERTY_ALIASES", "MAX_FACTS_PER_WORKSPACE",
    "normalize_property", "normalize_api_path", "normalize_data_type",
    "facts_from_statement", "facts_from_attributes",
    "collect_comparable_facts", "compare_facts",
]
