"""The local semantic-result cache.

An exact hit means ZERO provider calls for that target — the whole point.
The key is deliberately exhaustive; anything that could change the answer is
part of it:

    provider kind · model name · task type + version · prompt hash ·
    schema version · analyzer version · active-lens definition hash ·
    ordered evidence ids · ordered evidence CONTENT hashes · task options

so a different prompt version, model, lens, or a single changed evidence
byte is a miss. The cached value is the provider's validated structured
output — but reuse still re-runs local validation AND evidence verification
against current workspace ownership: a cache entry is a saved answer, never
saved trust.

Policy: ``local_cache_enabled=false`` disables both reads and writes;
``--force`` bypasses reads (and refreshes the entry on write).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from . import store
from . import ANALYZER_VERSION


def compute_cache_key(*, provider_kind: str, model_name: str, task_type: str,
                      task_version: str, prompt_hash: str,
                      schema_version: str, lens_hash: str,
                      evidence_ids: Sequence[str],
                      evidence_hashes: Sequence[str],
                      options: Optional[Dict[str, Any]] = None) -> str:
    payload = json.dumps({
        "provider_kind": provider_kind,
        "model_name": model_name,
        "task": [task_type, task_version],
        "prompt_hash": prompt_hash,
        "schema_version": schema_version,
        "analyzer_version": ANALYZER_VERSION,
        "lens_hash": lens_hash,
        "evidence_ids": list(evidence_ids),
        "evidence_hashes": list(evidence_hashes),
        "options": options or {},
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lens_definition_hash(lens: Optional[Dict[str, Any]]) -> str:
    """The active lens's identity for cache purposes; '' when no lens is
    active (which is itself a distinct, stable cache state)."""
    if not lens:
        return ""
    return hashlib.sha256(json.dumps(
        lens.get("definition") or {}, sort_keys=True).encode("utf-8")
    ).hexdigest()


def lookup(policy: Dict[str, Any], cache_key: str, *,
           force: bool = False) -> Optional[Dict[str, Any]]:
    """The cached structured output, or None. Disabled cache and --force both
    read nothing."""
    if force or not policy.get("local_cache_enabled", True):
        return None
    entry = store.cache_get(cache_key)
    return dict(entry["output"]) if entry else None


def put(policy: Dict[str, Any], cache_key: str, *, provider_kind: str,
        model_name: str, task_type: str, prompt_hash: str,
        schema_version: str, input_hash: str,
        output: Dict[str, Any]) -> bool:
    """Store a validated output. A disabled cache writes nothing."""
    if not policy.get("local_cache_enabled", True):
        return False
    store.cache_put(cache_key, provider_kind=provider_kind,
                    model_name=model_name, task_type=task_type,
                    prompt_hash=prompt_hash, schema_version=schema_version,
                    input_hash=input_hash, output=output)
    return True


def evidence_content_hashes(workspace_id: str,
                            evidence_ids: Sequence[str]) -> List[str]:
    """The stored content hash per evidence id, in the SAME order. A missing
    row contributes a distinct marker so the key cannot collide with a state
    where the row existed."""
    from .. import db
    out: List[str] = []
    for evidence_id in evidence_ids:
        row = db.get_evidence(workspace_id, evidence_id)
        out.append((row or {}).get("content_hash") or f"missing:{evidence_id}")
    return out


__all__ = ["compute_cache_key", "lens_definition_hash", "lookup", "put",
           "evidence_content_hashes"]
