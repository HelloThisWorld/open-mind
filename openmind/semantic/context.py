"""Bounded context assembly for analysis targets + immutable evidence
recovery.

WHAT A TARGET'S PACKET MAY CONTAIN — AND NOTHING ELSE
-----------------------------------------------------
For one target Segment: its own Evidence text, a bounded number of NEIGHBOR
segments (by ordinal, same revision), the heading path / code symbol as
structural metadata, matching deterministic glossary TERM NAMES, and exact
identifiers found in the text. Never the whole document, never another
asset, never the repository.

EVIDENCE RECOVERY
-----------------
:func:`resolve_evidence_text` is the verifier's and the packet builder's
shared source of truth: workspace-scoped lookup (a foreign evidence id
resolves to ``None``), then the same immutable recovery the AssetService
uses — the segment's own content blob for document blocks, the revision
blob's line range for code — with hash verification. The live file on disk
is never consulted.

TOKEN ESTIMATION
----------------
:func:`estimate_tokens` is a LOCAL chars/4 heuristic. Everything that
reports it labels it ``estimated``; it is never presented as provider-billed
usage.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .. import content_store, db, mapio, segmentation

#: Bounds on assembled context (spec: bounded parent + neighbors, never the
#: whole document).
MAX_NEIGHBORS = 2
MAX_GLOSSARY_TERMS = 12
MAX_IDENTIFIERS = 16
_IDENTIFIER_SCAN_RE = re.compile(
    r"\b[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,10}){1,3}\b")


def estimate_tokens(text: str) -> int:
    """chars/4 — a deterministic local ESTIMATE, clearly not billing truth."""
    return max(1, len(text or "") // 4)


# ---------------------------------------------------------------------------
# Immutable evidence recovery
# ---------------------------------------------------------------------------
def resolve_evidence_text(workspace_id: str,
                          evidence_id: str) -> Optional[str]:
    """The exact immutable content one Evidence row cites, or None.

    None means: not found IN THIS WORKSPACE (scoping is the repository
    JOIN's), or the snapshot cannot be recovered/verifies corrupt. Never
    falls back to the live file.
    """
    ev = db.get_evidence(workspace_id, evidence_id)
    if not ev:
        return None
    segment = (db.get_segment(workspace_id, ev["segment_id"])
               if ev.get("segment_id") else None)
    block_blob = (segment or {}).get("content_blob_hash", "")
    expected = ev.get("content_hash", "")
    try:
        if block_blob:
            text = content_store.get(workspace_id, block_blob).decode(
                "utf-8", "replace")
            if expected and segmentation.hash_text_utf8(text) != expected:
                return None
            return text
        rev = db.get_revision(workspace_id, ev["revision_id"])
        blob = (rev or {}).get("content_blob_hash", "")
        if not blob:
            return None
        locator = ev.get("locator") or {}
        snap = content_store.get(workspace_id, blob).decode("utf-8", "replace")
        recovered = segmentation.slice_lines(
            snap, int(locator.get("startLine", 0) or 0),
            int(locator.get("endLine", 0) or 0))
        if expected and segmentation.hash_text_utf8(recovered) != expected:
            return None
        return recovered
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Target context assembly
# ---------------------------------------------------------------------------
def _segment_untrusted_entry(workspace_id: str,
                             segment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One segment as an untrusted-content entry: its evidence id, locator
    and immutable text. Segments without evidence contribute nothing."""
    ev = db.get_evidence_for_segment(workspace_id, segment["id"])
    if not ev:
        return None
    text = resolve_evidence_text(workspace_id, ev["id"])
    if text is None or not text.strip():
        return None
    return {"evidenceId": ev["id"], "locator": dict(ev.get("locator") or {}),
            "text": text}


def _glossary_terms_in(workspace_id: str, text: str) -> List[str]:
    """Deterministic glossary TERM NAMES appearing in *text* (exact token
    containment, case-preserving, bounded). Definitions are project content
    and stay out of the structural context."""
    try:
        doc = mapio.load_glossary(workspace_id)
    except Exception:
        return []
    terms = list((doc or {}).get("terms") or {})
    found = [t for t in terms
             if t and re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}"
                                rf"(?![A-Za-z0-9])", text)]
    return sorted(found)[:MAX_GLOSSARY_TERMS]


def _identifiers_in(text: str) -> List[str]:
    seen: List[str] = []
    for match in _IDENTIFIER_SCAN_RE.finditer(text or ""):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
        if len(seen) >= MAX_IDENTIFIERS:
            break
    return seen


def build_segment_context(workspace_id: str, revision_id: str,
                          segment_id: str, *, asset: Dict[str, Any],
                          neighbors: int = MAX_NEIGHBORS) -> Optional[Dict[str, Any]]:
    """Everything one extraction target may show the model.

    Returns ``{untrusted: [...], context: {...}, evidence_ids: [...],
    estimated_tokens: N}`` or None when the target segment has no
    recoverable evidence text (nothing to analyze).
    """
    segment = db.get_segment(workspace_id, segment_id)
    if not segment or segment["revision_id"] != revision_id:
        return None
    main = _segment_untrusted_entry(workspace_id, segment)
    if main is None:
        return None

    untrusted: List[Dict[str, Any]] = [main]
    if neighbors > 0:
        # Neighbors by ordinal within the same revision, nearest first,
        # bounded — the ONLY additional content an extraction target gets.
        all_segments = db.list_segments(workspace_id, revision_id,
                                        limit=10_000)
        ordinal = segment["ordinal"]
        ranked = sorted(
            (s for s in all_segments
             if s["id"] != segment_id
             and abs(s["ordinal"] - ordinal) <= neighbors),
            key=lambda s: abs(s["ordinal"] - ordinal))
        for s in ranked[:neighbors]:
            entry = _segment_untrusted_entry(workspace_id, s)
            if entry is not None:
                untrusted.append(entry)

    meta = segment.get("metadata") or {}
    heading_path = list(meta.get("heading_path") or [])
    context: Dict[str, Any] = {
        "headingPath": heading_path,
        "symbol": segment.get("symbol", ""),
        "segmentType": segment.get("segment_type", ""),
        "logicalKey": asset.get("logical_key", ""),
        "assetType": asset.get("asset_type", ""),
        "glossaryTerms": _glossary_terms_in(workspace_id, main["text"]),
        "identifiers": _identifiers_in(main["text"]),
        "neighborsIncluded": len(untrusted) - 1,
    }
    total_chars = sum(len(e["text"]) for e in untrusted)
    return {
        "untrusted": untrusted,
        "context": context,
        "evidence_ids": [e["evidenceId"] for e in untrusted],
        "estimated_tokens": estimate_tokens("x" * total_chars),
    }


def build_revision_context(workspace_id: str, revision_id: str, *,
                           asset: Dict[str, Any],
                           max_segments: int = 6) -> Optional[Dict[str, Any]]:
    """Context for a REVISION-level task (document classification, revision
    status): the first bounded content-bearing segments — enough to read a
    title block and opening sections, never the whole document."""
    segments = db.list_segments(workspace_id, revision_id, limit=200)
    untrusted: List[Dict[str, Any]] = []
    for s in segments:
        if len(untrusted) >= max_segments:
            break
        entry = _segment_untrusted_entry(workspace_id, s)
        if entry is not None:
            untrusted.append(entry)
    if not untrusted:
        return None
    context = {
        "logicalKey": asset.get("logical_key", ""),
        "assetType": asset.get("asset_type", ""),
        "segmentType": "document",
        "neighborsIncluded": len(untrusted) - 1,
    }
    total_chars = sum(len(e["text"]) for e in untrusted)
    return {
        "untrusted": untrusted,
        "context": context,
        "evidence_ids": [e["evidenceId"] for e in untrusted],
        "estimated_tokens": estimate_tokens("x" * total_chars),
    }


__all__ = ["resolve_evidence_text", "build_segment_context",
           "build_revision_context", "estimate_tokens", "MAX_NEIGHBORS"]
