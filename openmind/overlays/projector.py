"""Overlay graph-delta projection (spec §16, §22).

Projects ONLY changed content into graph deltas by mapping each changed overlay
segment to the canonical entity it corresponds to, using the SAME deterministic
identity functions the canonical projector used at ingest time
(:mod:`openmind.knowledge.identity`). The model is never used to infer new
``implements`` / ``refines`` / ``verifies`` relations, and nothing here writes a
canonical row — deltas land only in the overlay's own tables.

Mapping is precise, not fuzzy: a changed Java method's symbol yields the exact
canonical key ``code-symbol:asset:<asset_id>:<symbol>``; a changed configuration
key yields ``configuration:asset:<asset_id>:<key>``. If the base entity with
that key exists it is a *modified*/*removed* delta bound to the base id; if not,
it is an *added* delta with a canonical-key CANDIDATE (never asserted as
canonical).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import db, machine
from ..knowledge import identity
from ..knowledge import store as kstore
from ..knowledge.vocabularies import EntityType
from ..git.vocabularies import DeltaType, Side
from . import store as ovl_store


class OverlayProjector:
    """Computes and persists an overlay's entity/relation graph deltas."""

    def __init__(self, workspace_id: str, overlay_id: str) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id

    def project(self, overlay_repositories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Walk changed files/segments and emit entity deltas. Returns a
        bounded summary. Reuses per-file asset resolution."""
        repo_by_id = {r["id"]: r for r in overlay_repositories}
        counts = {DeltaType.ADDED: 0, DeltaType.MODIFIED: 0,
                  DeltaType.REMOVED: 0}
        files = ovl_store.list_files(self.overlay_id)
        # map overlay_repository_id -> repository relative_root for path joins
        repo_root_rel: Dict[str, str] = {}
        for ovr in overlay_repositories:
            rec = db_git_repo(self.workspace_id, ovr["repository_id"])
            repo_root_rel[ovr["id"]] = (rec or {}).get("relative_root", ".")

        for f in files:
            if f["is_binary"] or f["is_submodule"] or f["is_lfs_pointer"] \
                    or f["is_symlink"]:
                continue
            rel_root = repo_root_rel.get(f["overlay_repository_id"], ".")
            asset = self._resolve_asset(rel_root, f["new_path"] or f["old_path"],
                                        f["old_path"])
            self._project_file(f, asset, counts)
        return {"entity_deltas": sum(counts.values()), "by_type": counts}

    # -- per-file -----------------------------------------------------------
    def _project_file(self, f: Dict[str, Any], asset: Optional[Dict[str, Any]],
                      counts: Dict[str, int]) -> None:
        after_segs = ovl_store.list_segments(
            self.overlay_id, overlay_file_id=f["id"], side=Side.AFTER)
        before_segs = ovl_store.list_segments(
            self.overlay_id, overlay_file_id=f["id"], side=Side.BEFORE)

        # Added / modified from the after side.
        for s in after_segs:
            cc = s.get("change_class")
            if cc not in ("added", "modified"):
                continue
            key, etype = self._entity_key_for(asset, s)
            base = kstore.find_entity_by_key(self.workspace_id, etype, key) \
                if key else None
            if base:
                ovl_store.add_entity_delta(
                    self.overlay_id, delta_type=DeltaType.MODIFIED,
                    base_entity_id=base["id"], canonical_key=key,
                    entity_type=etype,
                    before={"content_hash_side": "before"},
                    after={"id": base["id"], "display_name": s.get("symbol"),
                           "content_hash": s.get("content_hash"),
                           "lifecycle_status": "active"},
                    reason=f"segment {s.get('symbol') or s.get('segment_key')} "
                           f"changed ({cc})",
                    confidence="high" if cc == "modified" else "medium")
                counts[DeltaType.MODIFIED] += 1
            else:
                ovl_store.add_entity_delta(
                    self.overlay_id, delta_type=DeltaType.ADDED,
                    canonical_key=key, entity_type=etype,
                    after={"canonical_key": key, "entity_type": etype,
                           "display_name": s.get("symbol"),
                           "origin": "overlay", "lifecycle_status": "active"},
                    reason=f"new {etype} {s.get('symbol') or s.get('segment_key')}",
                    confidence="medium")
                counts[DeltaType.ADDED] += 1

        # Removed from the before side.
        for s in before_segs:
            if s.get("change_class") != "deleted":
                continue
            key, etype = self._entity_key_for(asset, s)
            base = kstore.find_entity_by_key(self.workspace_id, etype, key) \
                if key else None
            if base:
                ovl_store.add_entity_delta(
                    self.overlay_id, delta_type=DeltaType.REMOVED,
                    base_entity_id=base["id"], canonical_key=key,
                    entity_type=etype,
                    before={"id": base["id"], "display_name": s.get("symbol")},
                    reason=f"segment {s.get('symbol') or s.get('segment_key')} "
                           f"deleted",
                    confidence="high")
                counts[DeltaType.REMOVED] += 1

    # -- key derivation -----------------------------------------------------
    def _entity_key_for(self, asset: Optional[Dict[str, Any]],
                        segment: Dict[str, Any]) -> tuple:
        """The exact canonical key + entity type a changed segment maps to.

        Code symbols/types need the asset id (as the canonical projector keys
        them); if the file is not a known canonical asset the key is empty and
        the segment becomes an *added* delta with no base match."""
        symbol = segment.get("symbol") or ""
        seg_type = segment.get("segment_type") or ""
        if not asset:
            return "", EntityType.CODE_SYMBOL
        asset_id = asset["id"]
        if seg_type in ("method", "constructor"):
            return (identity.symbol_entity_key(asset_id, symbol),
                    EntityType.CODE_SYMBOL)
        if seg_type == "type":
            return (identity.symbol_entity_key(asset_id, symbol),
                    EntityType.CODE_SYMBOL)
        # generic file-range segment: bind to the code component (asset).
        return (identity.asset_entity_key(EntityType.CODE_COMPONENT, asset_id),
                EntityType.CODE_COMPONENT)

    def _resolve_asset(self, rel_root: str, new_path: str, old_path: str
                       ) -> Optional[Dict[str, Any]]:
        """Find the base Asset for a changed file by its workspace-relative
        logical key. Tries the repo-root join first, then the bare path."""
        for path in (new_path, old_path):
            if not path:
                continue
            for candidate in self._logical_candidates(rel_root, path):
                asset = db.find_asset_by_logical_key(self.workspace_id,
                                                     candidate)
                if asset:
                    return asset
        return None

    @staticmethod
    def _logical_candidates(rel_root: str, path: str) -> List[str]:
        path = (path or "").replace("\\", "/").lstrip("/")
        out = [path]
        rr = (rel_root or ".").strip("/")
        if rr and rr != ".":
            out.insert(0, f"{rr}/{path}")
        return out


def db_git_repo(workspace_id: str, repository_id: str) -> Optional[Dict[str, Any]]:
    from ..git import store as git_store
    return git_store.get_repository(workspace_id, repository_id)


__all__ = ["OverlayProjector"]
