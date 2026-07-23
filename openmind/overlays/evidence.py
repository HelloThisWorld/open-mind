"""Overlay Evidence locators (spec §18).

An overlay Evidence row cites a line range within one side of one changed file.
Committed evidence uses a ``git-blob-range`` locator anchored to a commit;
working-tree evidence uses a ``git-worktree-range`` locator anchored to a
worktree hash and a layer. Paths are repository-relative; no absolute path ever
enters the locator. The exact bytes are always recoverable from the immutable
content store via the row's ``content_hash``.

Overlay Evidence ids use the ``oev_`` prefix and are NEVER accepted by canonical
Candidate Promotion (enforced by the promotion path, which only reads canonical
``evidence`` rows).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..git.vocabularies import Side


def blob_range_locator(*, repository: str, overlay_id: str,
                       overlay_revision: int, side: str, commit: str,
                       path: str, start_line: int, end_line: int,
                       symbol: str = "") -> Dict[str, Any]:
    """A committed-blob evidence locator (spec §18)."""
    loc = {
        "kind": "git-blob-range",
        "repository": repository,
        "overlayId": overlay_id,
        "overlayRevision": int(overlay_revision),
        "side": side,
        "commit": commit,
        "path": path,
        "startLine": int(start_line),
        "endLine": int(end_line),
    }
    if symbol:
        loc["symbol"] = symbol
    return loc


def worktree_range_locator(*, repository: str, overlay_id: str,
                           overlay_revision: int, side: str, base_commit: str,
                           worktree_hash: str, layer: str, path: str,
                           start_line: int, end_line: int,
                           symbol: str = "") -> Dict[str, Any]:
    """A working-tree evidence locator (spec §18)."""
    loc = {
        "kind": "git-worktree-range",
        "repository": repository,
        "overlayId": overlay_id,
        "overlayRevision": int(overlay_revision),
        "side": side,
        "baseCommit": base_commit,
        "worktreeHash": worktree_hash,
        "layer": layer,
        "path": path,
        "startLine": int(start_line),
        "endLine": int(end_line),
    }
    if symbol:
        loc["symbol"] = symbol
    return loc


__all__ = ["blob_range_locator", "worktree_range_locator"]
