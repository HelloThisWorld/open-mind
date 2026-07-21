"""Structured-output schemas and their LOCAL validators.

Two artifacts per schema, deliberately:

* a **JSON Schema document** handed to providers that support native
  structured output (OpenAI strict mode, Anthropic ``output_config``). Strict
  mode demands ``additionalProperties: false`` and every property listed in
  ``required``, so the schemas use required-everything with empty-string /
  empty-array as "none".
* a **hand-written Python validator** — the local authority. Provider-side
  enforcement is an optimization, never a trust boundary: whatever came back
  is validated HERE, identically for every provider (including the local one
  that cannot enforce schemas natively). Unknown top-level keys, unknown
  candidate types, empty statements and out-of-bounds sizes all fail with
  :class:`~openmind.semantic.errors.ProviderResponseValidationError`.

The validators check SHAPE and vocabulary. Evidence truth (does the quote
exist in the cited Evidence?) is the verifier's job, one layer up.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .errors import ProviderResponseValidationError
from .models import (ConflictCategory, DocumentClassificationType,
                     EngineeringConceptType, RelationCandidateType,
                     RevisionStatusVocabulary, StructuredSchema)

# Bounds enforced on every response. Generous for real content, hard against
# a model that tries to stuff prose where data belongs.
MAX_CANDIDATES = 50
MAX_EVIDENCE_PER_CANDIDATE = 8
MAX_QUOTE_CHARS = 600
MAX_REASON_CHARS = 500
MAX_TITLE_CHARS = 300
MAX_STATEMENT_CHARS = 2000
MAX_KEY_CHARS = 120
MAX_ATTRIBUTE_CHARS = 4000

_CONFIDENCE_HINTS = frozenset({"high", "medium", "low"})


def _err(message: str, **details: Any) -> ProviderResponseValidationError:
    return ProviderResponseValidationError(message, details=details or None)


# ---------------------------------------------------------------------------
# JSON Schema builders (provider-side)
# ---------------------------------------------------------------------------
def _evidence_schema() -> Dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "evidenceId": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": ["evidenceId", "quote"],
            "additionalProperties": False,
        },
    }


def _candidate_item_schema(allowed_types: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidateType": {"type": "string", "enum": sorted(allowed_types)},
            "stableKey": {"type": "string"},
            "title": {"type": "string"},
            "statement": {"type": "string"},
            "attributes": {"type": "object", "additionalProperties":
                           {"type": "string"}},
            "evidence": _evidence_schema(),
            "confidenceHint": {"type": "string",
                               "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["candidateType", "stableKey", "title", "statement",
                     "attributes", "evidence", "confidenceHint", "reason"],
        "additionalProperties": False,
    }


def _candidates_envelope(allowed_types: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidates": {"type": "array",
                           "items": _candidate_item_schema(allowed_types)},
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _ref_schema() -> Dict[str, Any]:
    # kind: what the id/key names (candidate/asset/revision/segment/symbol/
    # document); exactly the reference vocabulary relation analysis feeds in.
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "enum": ["candidate", "asset", "revision", "segment",
                              "symbol", "document"]},
            "id": {"type": "string"},
            "key": {"type": "string"},
        },
        "required": ["kind", "id", "key"],
        "additionalProperties": False,
    }


def _relations_envelope() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "relationType": {
                            "type": "string",
                            "enum": sorted(RelationCandidateType.VALUES)},
                        "sourceRef": _ref_schema(),
                        "targetRef": _ref_schema(),
                        "evidence": _evidence_schema(),
                        "confidenceHint": {"type": "string",
                                           "enum": ["high", "medium", "low"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["relationType", "sourceRef", "targetRef",
                                 "evidence", "confidenceHint", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["relations"],
        "additionalProperties": False,
    }


def _conflicts_envelope() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string",
                                     "enum": sorted(ConflictCategory.VALUES)},
                        "refs": {"type": "array", "items": _ref_schema()},
                        "explanation": {"type": "string"},
                        "evidence": _evidence_schema(),
                        "confidenceHint": {"type": "string",
                                           "enum": ["high", "medium", "low"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["category", "refs", "explanation", "evidence",
                                 "confidenceHint", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["conflicts"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Local validators (the authority)
# ---------------------------------------------------------------------------
def _require_keys(obj: Dict[str, Any], allowed: frozenset,
                  where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise _err(f"{where}: unknown fields {', '.join(unknown)}",
                   where=where, unknown=unknown)


def _validate_evidence_list(items: Any, where: str) -> List[Dict[str, str]]:
    if not isinstance(items, list):
        raise _err(f"{where}.evidence must be an array", where=where)
    if len(items) > MAX_EVIDENCE_PER_CANDIDATE:
        raise _err(f"{where}.evidence exceeds {MAX_EVIDENCE_PER_CANDIDATE} "
                   f"items", where=where, count=len(items))
    out: List[Dict[str, str]] = []
    for i, ev in enumerate(items):
        if not isinstance(ev, dict):
            raise _err(f"{where}.evidence[{i}] must be an object", where=where)
        _require_keys(ev, frozenset({"evidenceId", "quote"}),
                      f"{where}.evidence[{i}]")
        evidence_id = str(ev.get("evidenceId") or "").strip()
        quote = str(ev.get("quote") or "")
        if not evidence_id:
            raise _err(f"{where}.evidence[{i}].evidenceId is empty",
                       where=where)
        if len(quote) > MAX_QUOTE_CHARS:
            raise _err(f"{where}.evidence[{i}].quote exceeds "
                       f"{MAX_QUOTE_CHARS} chars", where=where,
                       chars=len(quote))
        out.append({"evidenceId": evidence_id, "quote": quote})
    return out


_CANDIDATE_KEYS = frozenset({"candidateType", "stableKey", "title",
                             "statement", "attributes", "evidence",
                             "confidenceHint", "reason"})


def _validate_candidates(output: Dict[str, Any],
                         allowed_types: frozenset) -> Dict[str, Any]:
    _require_keys(output, frozenset({"candidates"}), "output")
    items = output.get("candidates")
    if not isinstance(items, list):
        raise _err("output.candidates must be an array")
    if len(items) > MAX_CANDIDATES:
        raise _err(f"output.candidates exceeds {MAX_CANDIDATES} items",
                   count=len(items))
    normalized: List[Dict[str, Any]] = []
    for i, cand in enumerate(items):
        where = f"candidates[{i}]"
        if not isinstance(cand, dict):
            raise _err(f"{where} must be an object")
        _require_keys(cand, _CANDIDATE_KEYS, where)
        ctype = str(cand.get("candidateType") or "").strip()
        if ctype not in allowed_types:
            raise _err(f"{where}.candidateType {ctype!r} is not allowed for "
                       f"this task", allowed=sorted(allowed_types))
        statement = str(cand.get("statement") or "").strip()
        if not statement:
            raise _err(f"{where}.statement is empty")
        if len(statement) > MAX_STATEMENT_CHARS:
            raise _err(f"{where}.statement exceeds {MAX_STATEMENT_CHARS} "
                       f"chars", chars=len(statement))
        title = str(cand.get("title") or "").strip()
        if len(title) > MAX_TITLE_CHARS:
            raise _err(f"{where}.title exceeds {MAX_TITLE_CHARS} chars")
        stable_key = str(cand.get("stableKey") or "").strip()
        if len(stable_key) > MAX_KEY_CHARS:
            raise _err(f"{where}.stableKey exceeds {MAX_KEY_CHARS} chars")
        hint = str(cand.get("confidenceHint") or "").strip().lower()
        if hint not in _CONFIDENCE_HINTS:
            raise _err(f"{where}.confidenceHint must be high/medium/low")
        reason = str(cand.get("reason") or "").strip()
        if len(reason) > MAX_REASON_CHARS:
            raise _err(f"{where}.reason exceeds {MAX_REASON_CHARS} chars")
        attributes = cand.get("attributes")
        if not isinstance(attributes, dict):
            raise _err(f"{where}.attributes must be an object")
        attrs = {str(k): str(v) for k, v in attributes.items()}
        if sum(len(k) + len(v) for k, v in attrs.items()) > MAX_ATTRIBUTE_CHARS:
            raise _err(f"{where}.attributes exceed {MAX_ATTRIBUTE_CHARS} "
                       f"chars in total")
        normalized.append({
            "candidateType": ctype, "stableKey": stable_key, "title": title,
            "statement": statement, "attributes": attrs,
            "evidence": _validate_evidence_list(cand.get("evidence"), where),
            "confidenceHint": hint, "reason": reason,
        })
    return {"candidates": normalized}


def _validate_ref(ref: Any, where: str) -> Dict[str, str]:
    if not isinstance(ref, dict):
        raise _err(f"{where} must be an object")
    _require_keys(ref, frozenset({"kind", "id", "key"}), where)
    kind = str(ref.get("kind") or "").strip()
    if kind not in {"candidate", "asset", "revision", "segment", "symbol",
                    "document"}:
        raise _err(f"{where}.kind {kind!r} is not a known reference kind")
    ref_id = str(ref.get("id") or "").strip()
    ref_key = str(ref.get("key") or "").strip()
    if not ref_id and not ref_key:
        raise _err(f"{where} needs an id or a key")
    return {"kind": kind, "id": ref_id, "key": ref_key}


def _validate_relations(output: Dict[str, Any], _: frozenset) -> Dict[str, Any]:
    _require_keys(output, frozenset({"relations"}), "output")
    items = output.get("relations")
    if not isinstance(items, list):
        raise _err("output.relations must be an array")
    if len(items) > MAX_CANDIDATES:
        raise _err(f"output.relations exceeds {MAX_CANDIDATES} items")
    normalized = []
    for i, rel in enumerate(items):
        where = f"relations[{i}]"
        if not isinstance(rel, dict):
            raise _err(f"{where} must be an object")
        _require_keys(rel, frozenset({"relationType", "sourceRef", "targetRef",
                                      "evidence", "confidenceHint", "reason"}),
                      where)
        rtype = str(rel.get("relationType") or "").strip()
        if rtype not in RelationCandidateType.VALUES:
            raise _err(f"{where}.relationType {rtype!r} is unknown")
        reason = str(rel.get("reason") or "").strip()
        if not reason:
            raise _err(f"{where}.reason is empty")
        if len(reason) > MAX_REASON_CHARS:
            raise _err(f"{where}.reason exceeds {MAX_REASON_CHARS} chars")
        hint = str(rel.get("confidenceHint") or "").strip().lower()
        if hint not in _CONFIDENCE_HINTS:
            raise _err(f"{where}.confidenceHint must be high/medium/low")
        normalized.append({
            "relationType": rtype,
            "sourceRef": _validate_ref(rel.get("sourceRef"),
                                       f"{where}.sourceRef"),
            "targetRef": _validate_ref(rel.get("targetRef"),
                                       f"{where}.targetRef"),
            "evidence": _validate_evidence_list(rel.get("evidence"), where),
            "confidenceHint": hint, "reason": reason,
        })
    return {"relations": normalized}


def _validate_conflicts(output: Dict[str, Any], _: frozenset) -> Dict[str, Any]:
    _require_keys(output, frozenset({"conflicts"}), "output")
    items = output.get("conflicts")
    if not isinstance(items, list):
        raise _err("output.conflicts must be an array")
    if len(items) > MAX_CANDIDATES:
        raise _err(f"output.conflicts exceeds {MAX_CANDIDATES} items")
    normalized = []
    for i, conf in enumerate(items):
        where = f"conflicts[{i}]"
        if not isinstance(conf, dict):
            raise _err(f"{where} must be an object")
        _require_keys(conf, frozenset({"category", "refs", "explanation",
                                       "evidence", "confidenceHint",
                                       "reason"}), where)
        category = str(conf.get("category") or "").strip()
        if category not in ConflictCategory.VALUES:
            raise _err(f"{where}.category {category!r} is unknown")
        refs = conf.get("refs")
        if not isinstance(refs, list) or len(refs) < 2 or len(refs) > 4:
            raise _err(f"{where}.refs must list 2-4 competing references")
        explanation = str(conf.get("explanation") or "").strip()
        if not explanation:
            raise _err(f"{where}.explanation is empty")
        if len(explanation) > MAX_STATEMENT_CHARS:
            raise _err(f"{where}.explanation exceeds {MAX_STATEMENT_CHARS} "
                       f"chars")
        hint = str(conf.get("confidenceHint") or "").strip().lower()
        if hint not in _CONFIDENCE_HINTS:
            raise _err(f"{where}.confidenceHint must be high/medium/low")
        reason = str(conf.get("reason") or "").strip()
        if len(reason) > MAX_REASON_CHARS:
            raise _err(f"{where}.reason exceeds {MAX_REASON_CHARS} chars")
        normalized.append({
            "category": category,
            "refs": [_validate_ref(r, f"{where}.refs[{j}]")
                     for j, r in enumerate(refs)],
            "explanation": explanation,
            "evidence": _validate_evidence_list(conf.get("evidence"), where),
            "confidenceHint": hint, "reason": reason,
        })
    return {"conflicts": normalized}


# ---------------------------------------------------------------------------
# The schema registry
# ---------------------------------------------------------------------------
class _SchemaDef:
    def __init__(self, name: str, version: str, json_schema: Dict[str, Any],
                 validator: Callable[[Dict[str, Any], frozenset],
                                     Dict[str, Any]],
                 allowed_types: frozenset) -> None:
        self.name = name
        self.version = version
        self.json_schema = json_schema
        self.validator = validator
        self.allowed_types = allowed_types

    def structured(self) -> StructuredSchema:
        return StructuredSchema(name=self.name, version=self.version,
                                json_schema=self.json_schema, strict=True)


_SCHEMAS: Dict[str, _SchemaDef] = {}


def _register(defn: _SchemaDef) -> None:
    _SCHEMAS[defn.name] = defn


_register(_SchemaDef(
    "engineering-candidates", "1",
    _candidates_envelope(sorted(EngineeringConceptType.VALUES)),
    _validate_candidates, EngineeringConceptType.VALUES))

_register(_SchemaDef(
    "document-classification", "1",
    _candidates_envelope(sorted(DocumentClassificationType.VALUES)),
    _validate_candidates, DocumentClassificationType.VALUES))

_register(_SchemaDef(
    "revision-status", "1",
    _candidates_envelope(sorted(RevisionStatusVocabulary.VALUES)),
    _validate_candidates, RevisionStatusVocabulary.VALUES))

_register(_SchemaDef(
    "relation-candidates", "1", _relations_envelope(),
    _validate_relations, RelationCandidateType.VALUES))

_register(_SchemaDef(
    "conflict-candidates", "1", _conflicts_envelope(),
    _validate_conflicts, ConflictCategory.VALUES))


def get_schema(name: str) -> StructuredSchema:
    defn = _SCHEMAS.get(name)
    if defn is None:
        raise KeyError(f"unknown structured schema: {name!r}")
    return defn.structured()


def allowed_types_for(name: str, task_allowed: frozenset = frozenset()
                      ) -> frozenset:
    """The candidate types a task may produce: the schema's vocabulary,
    optionally narrowed by the task definition (a requirement-extraction task
    only accepts 'requirement' even though the schema knows all ten)."""
    defn = _SCHEMAS.get(name)
    base = defn.allowed_types if defn else frozenset()
    return base & task_allowed if task_allowed else base


def validate_output(name: str, output: Dict[str, Any],
                    task_allowed: frozenset = frozenset()) -> Dict[str, Any]:
    """Validate + normalize a provider's structured output. Raises the typed
    validation error; on success returns the normalized envelope."""
    defn = _SCHEMAS.get(name)
    if defn is None:
        raise KeyError(f"unknown structured schema: {name!r}")
    if not isinstance(output, dict):
        raise _err("structured output must be a JSON object")
    allowed = allowed_types_for(name, task_allowed)
    return defn.validator(output, allowed)


def list_schemas() -> List[Dict[str, str]]:
    return [{"name": s.name, "version": s.version}
            for s in sorted(_SCHEMAS.values(), key=lambda s: s.name)]


__all__ = ["get_schema", "validate_output", "allowed_types_for",
           "list_schemas", "MAX_CANDIDATES", "MAX_EVIDENCE_PER_CANDIDATE",
           "MAX_QUOTE_CHARS", "MAX_REASON_CHARS", "MAX_STATEMENT_CHARS"]
