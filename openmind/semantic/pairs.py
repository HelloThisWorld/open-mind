"""Deterministic, bounded pair generation for relation and conflict analysis.

The rule the spec draws in red ink: NEVER an O(n²) cross-product. A pair
exists only when a deterministic signal links two items:

* ``identifier``        — two active candidates share the same explicit
  ``stable_key`` (across different revisions or assets);
* ``identifier-mention``— one candidate's statement literally contains
  another candidate's explicit identifier;
* ``code-symbol``       — a candidate's statement contains an exact
  workspace code symbol (current revisions only, via the Phase 2 symbol
  table);
* ``subject-title``     — (conflicts) two active candidates of the same type
  share a normalized title or stable key but state different things.

Everything is bounded: candidates considered, pairs per signal, total pairs,
pairs per provider request. The bounds are visible constants, and the runner
reports how many pairs were dropped by them.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List

from .. import db
from . import store
from .verifier import normalize_ws

#: Bounds. Deliberately small — relation analysis multiplies provider spend.
MAX_CANDIDATES_CONSIDERED = 300
MAX_RELATION_PAIRS = 40
MAX_CONFLICT_PAIRS = 20
PAIR_BATCH = 4                     # pairs per provider request

_IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,10}){1,3}\b")


def _candidate_ref(candidate: Dict[str, Any]) -> Dict[str, str]:
    return {"kind": "candidate", "id": candidate["id"],
            "key": candidate.get("stable_key", "")}


def _statement_hash(candidate: Dict[str, Any]) -> str:
    return hashlib.sha256(normalize_ws(
        candidate.get("statement", "")).lower().encode("utf-8")).hexdigest()


def pair_batch_key(pairs: List[Dict[str, Any]]) -> str:
    """Stable identity for one batch of pairs — the checkpoint unit id, so a
    resumed run recreates exactly the same targets."""
    payload = "|".join(sorted(
        f"{p['sourceRef']['kind']}:{p['sourceRef']['id']}:{p['sourceRef']['key']}"
        f"->{p['targetRef']['kind']}:{p['targetRef']['id']}:{p['targetRef']['key']}"
        f"#{p['signal']}" for p in pairs))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def relation_pairs(workspace_id: str) -> Dict[str, Any]:
    """Bounded relation-candidate pairs over the workspace's ACTIVE
    candidates. Returns ``{pairs: [...], dropped: N}``."""
    candidates = store.list_candidates(
        workspace_id, lifecycle_status="active",
        limit=MAX_CANDIDATES_CONSIDERED)
    pairs: List[Dict[str, Any]] = []
    seen: set = set()
    dropped = 0

    def add(source: Dict[str, str], target: Dict[str, str], signal: str,
            shared: str) -> None:
        nonlocal dropped
        key = (source["kind"], source["id"], source["key"],
               target["kind"], target["id"], target["key"])
        rkey = (target["kind"], target["id"], target["key"],
                source["kind"], source["id"], source["key"])
        if key in seen or rkey in seen or source == target:
            return
        if len(pairs) >= MAX_RELATION_PAIRS:
            dropped += 1
            return
        seen.add(key)
        pairs.append({"sourceRef": source, "targetRef": target,
                      "signal": signal, "sharedKey": shared})

    # identifier: same stable_key, different revision or asset
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for cand in candidates:
        key = str(cand.get("stable_key") or "")
        if key:
            by_key.setdefault(key, []).append(cand)
    for key in sorted(by_key):
        group = by_key[key]
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.get("revision_id") != b.get("revision_id"):
                    add(_candidate_ref(a), _candidate_ref(b),
                        "identifier", key)

    # identifier-mention: statement contains another candidate's key
    keys = {k for k in by_key if len(k) >= 4}
    for cand in candidates:
        mentioned = {m.group(0) for m in
                     _IDENT_RE.finditer(cand.get("statement", ""))}
        for key in sorted(mentioned & keys):
            if key == cand.get("stable_key"):
                continue
            for other in by_key[key][:2]:
                add(_candidate_ref(cand), _candidate_ref(other),
                    "identifier-mention", key)

    # code-symbol: statement contains an exact current workspace symbol
    symbols = db.list_workspace_symbols(workspace_id)
    if symbols:
        symbol_names = sorted(symbols, key=len, reverse=True)[:2_000]
        for cand in candidates:
            statement = cand.get("statement", "")
            hits = 0
            for name in symbol_names:
                if len(name) < 4 or hits >= 2:
                    continue
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}"
                             rf"(?![A-Za-z0-9_])", statement):
                    info = symbols[name]
                    add(_candidate_ref(cand),
                        {"kind": "symbol", "id": info.get("segment_id", ""),
                         "key": name},
                        "code-symbol", name)
                    hits += 1
    return {"pairs": pairs, "dropped": dropped}


def conflict_pairs(workspace_id: str) -> Dict[str, Any]:
    """Bounded conflict pairs: same explicit identifier or same normalized
    subject title, same candidate type, DIFFERENT statements."""
    candidates = store.list_candidates(
        workspace_id, lifecycle_status="active",
        limit=MAX_CANDIDATES_CONSIDERED)
    pairs: List[Dict[str, Any]] = []
    seen: set = set()
    dropped = 0

    def add(a: Dict[str, Any], b: Dict[str, Any], signal: str,
            shared: str) -> None:
        nonlocal dropped
        key = tuple(sorted((a["id"], b["id"])))
        if key in seen:
            return
        if len(pairs) >= MAX_CONFLICT_PAIRS:
            dropped += 1
            return
        seen.add(key)
        pairs.append({"sourceRef": _candidate_ref(a),
                      "targetRef": _candidate_ref(b),
                      "signal": signal, "sharedKey": shared})

    def group_and_pair(keyer, signal: str) -> None:
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for cand in candidates:
            key = keyer(cand)
            if key:
                groups.setdefault(key, []).append(cand)
        for key in sorted(groups, key=str):
            group = groups[key]
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if _statement_hash(a) != _statement_hash(b):
                        add(a, b, signal, str(key[-1] if
                                              isinstance(key, tuple) else key))

    group_and_pair(
        lambda c: (c.get("candidate_type"), c.get("stable_key"))
        if c.get("stable_key") else None, "identifier")
    group_and_pair(
        lambda c: (c.get("candidate_type"),
                   normalize_ws(c.get("title", "")).lower())
        if not c.get("stable_key") and len(normalize_ws(
            c.get("title", ""))) >= 8 else None, "subject-title")
    return {"pairs": pairs, "dropped": dropped}


__all__ = ["relation_pairs", "conflict_pairs", "pair_batch_key",
           "MAX_RELATION_PAIRS", "MAX_CONFLICT_PAIRS", "PAIR_BATCH",
           "MAX_CANDIDATES_CONSIDERED"]
