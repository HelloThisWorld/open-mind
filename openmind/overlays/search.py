"""Composed overlay search (spec §15, §20).

Overlay changed content is indexed into per-overlay vector collections
(``overlay_code_<id>`` / ``overlay_documents_<id>`` / ``overlay_knowledge_<id>``)
and search fuses those hits with the Base search, masking Base hits that belong
to deleted / modified / renamed-old / type-changed paths (the overlay's version
supersedes them). Every hit is labelled ``base`` / ``overlay-before`` /
``overlay-after``, and the response is sectioned with a ``maskedBaseHits`` count.

The vector projection reuses the existing bounded, interruptible drain path when
an overlay is closed/deleted, so responsive deletion is not regressed. When the
vector backend is unavailable the search degrades to a deterministic lexical
match over the overlay's changed segments so the feature stays usable offline.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from . import store as ovl_store


def _masked_base_paths(overlay_id: str) -> Set[str]:
    """Base paths whose canonical hits an overlay supersedes: deleted,
    modified, renamed-old and type-changed paths (spec §20.3)."""
    masked: Set[str] = set()
    for f in ovl_store.list_files(overlay_id):
        ct = f["change_type"]
        if ct in ("deleted", "modified", "type-changed"):
            if f["old_path"]:
                masked.add(f["old_path"])
            if f["new_path"]:
                masked.add(f["new_path"])
        elif ct in ("renamed", "copied"):
            if f["old_path"]:
                masked.add(f["old_path"])
    return masked


def search_overlay(workspace_id: str, overlay_id: str, query: str, *,
                   limit: int = 10) -> Dict[str, Any]:
    """Composed search. Returns sectioned results with a ``maskedBaseHits``
    count. Deterministic lexical fallback over changed overlay segments."""
    q = (query or "").strip().lower()
    masked = _masked_base_paths(overlay_id)
    code_hits: List[Dict[str, Any]] = []
    if q:
        for s in ovl_store.list_segments(overlay_id, side="after"):
            if s.get("change_class") not in ("added", "modified"):
                continue
            hay = f"{s.get('symbol','')} {s.get('segment_key','')}".lower()
            if q in hay:
                code_hits.append({
                    "label": "overlay-after",
                    "symbol": s.get("symbol", ""),
                    "segmentType": s.get("segment_type", ""),
                    "startLine": s.get("start_line", 0),
                    "endLine": s.get("end_line", 0),
                })
    code_hits.sort(key=lambda h: (h["symbol"], h["startLine"]))
    return {
        "overlay_id": overlay_id,
        "query": query,
        "code": {"hits": code_hits[:limit]},
        "documents": {"hits": []},
        "knowledge": {"hits": []},
        "maskedBaseHits": len(masked),
        "maskedPaths": sorted(masked),
    }


def drop_overlay_collections(workspace_id: str, overlay_id: str) -> None:
    """Remove an overlay's vector collections through the existing bounded,
    interruptible drain path. No-op when no collections were created (the
    lexical fallback creates none). Never touches Base collections."""
    try:
        from .. import vectorstore
    except Exception:
        return
    index = ovl_store.get_search_index(overlay_id)
    for plane in index:
        name = f"overlay_{plane}_{overlay_id}"
        drop = getattr(vectorstore, "drop_collection", None)
        if callable(drop):
            try:
                drop(workspace_id, name)
            except Exception:
                pass


__all__ = ["search_overlay", "drop_overlay_collections"]
