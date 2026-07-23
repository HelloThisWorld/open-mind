"""Explicit post-merge reconciliation (spec §35).

OpenMind never merges a branch. After the user merges externally and updates the
canonical checkout, this verifies that the overlay's head commit is now an
ancestor of (or otherwise provably present in) the canonical HEAD, that the
canonical worktree is clean, and links the overlay to the resulting canonical
Knowledge Revision. Projected overlay relations are NEVER auto-promoted — only
canonical ingestion, deterministic projection and explicit governance may alter
the Base graph.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..git.command import default_runner
from ..git.diff import DiffExtractor
from ..git.refs import RefResolver
from ..git.repositories import resolve_root
from ..git.vocabularies import OverlayState
from . import store as ovl_store


def reconcile(workspace_id: str, overlay: Dict[str, Any], *, git_service,
              knowledge, traceability, actor: str = "", note: str = ""
              ) -> Dict[str, Any]:
    overlay_id = overlay["id"]
    if overlay["state"] not in (OverlayState.READY, OverlayState.STALE):
        return {"overlay_id": overlay_id, "reconciled": False,
                "reason": "overlay must be ready or stale to reconcile",
                "state": overlay["state"]}
    repos = ovl_store.list_overlay_repositories(overlay_id)
    runner = default_runner()
    checks = []
    for r in repos:
        from ..git import store as git_store
        rec = git_store.get_repository(workspace_id, r["repository_id"])
        if not rec:
            checks.append({"repository": r["repository_id"],
                           "merged": False, "reason": "repository missing"})
            continue
        root = resolve_root(workspace_id, rec)
        resolver = RefResolver(root, runner)
        dx = DiffExtractor(root, runner)
        head_commit = r["head_commit"]
        canonical_head = resolver.head_commit()
        merged = bool(head_commit) and bool(canonical_head) and \
            resolver.is_ancestor(head_commit, canonical_head)
        clean = dx.is_worktree_clean()
        checks.append({
            "repository": rec["repository_key"],
            "headCommit": head_commit, "canonicalHead": canonical_head,
            "merged": merged, "cleanWorktree": clean,
        })

    all_merged = bool(checks) and all(c.get("merged") for c in checks)
    all_clean = all(c.get("cleanWorktree", True) for c in checks)
    if not all_merged:
        return {"overlay_id": overlay_id, "reconciled": False,
                "reason": "overlay head is not an ancestor of canonical HEAD; "
                          "merge externally and update the checkout first",
                "checks": checks}
    if not all_clean:
        return {"overlay_id": overlay_id, "reconciled": False,
                "reason": "canonical worktree is dirty; commit or clean it "
                          "before reconciling",
                "checks": checks}

    merged_kr = 0
    if knowledge is not None:
        try:
            merged_kr = knowledge.get_current_revision(
                workspace_id).get("knowledge_revision", 0)
        except Exception:
            merged_kr = 0
    ovl_store.set_state(workspace_id, overlay_id, OverlayState.MERGED)
    ovl_store.update_overlay(
        workspace_id, overlay_id,
        summary={**overlay.get("summary", {}),
                 "merged": True, "mergedKnowledgeRevision": merged_kr,
                 "reconciledBy": str(actor or "")[:200],
                 "reconcileNote": str(note or "")[:2000]})
    return {"overlay_id": overlay_id, "reconciled": True,
            "state": OverlayState.MERGED, "mergedKnowledgeRevision": merged_kr,
            "checks": checks,
            "note": "projected overlay relations were NOT promoted; only "
                    "canonical ingestion may alter the Base graph"}


__all__ = ["reconcile"]
