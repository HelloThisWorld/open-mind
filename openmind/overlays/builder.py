"""Overlay build pipeline (spec §5, §14–§20, §31, §32).

Turns a resolved overlay plan into persisted overlay files, segments and
evidence, entirely from Git reads and the immutable content store. Never
mutates Git, never contacts a remote, never writes a canonical table.

Build steps (mirrored by the ``git_overlay_build`` job, spec §36):

    validating-baseline -> resolving-refs -> calculating-merge-base ->
    reading-diff -> snapshotting-content -> parsing-changes ->
    segmenting-changes -> (embedding-overlay) -> projecting-graph-delta ->
    persisting-overlay -> done

Embedding and graph-delta projection are invoked by the OverlayService after a
successful file/segment build; this module owns everything up to and including
segmentation, plus the overlay source hash used for incrementality.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import config, content_store
from ..git import GIT_ADAPTER_VERSION
from ..git.command import default_runner
from ..git.content import ContentReader
from ..git.diff import DiffExtractor
from ..git.hunks import changed_ranges_from_text
from ..git.models import ChangedRange, FileChange
from ..git.refs import RefResolver
from ..git.repositories import resolve_root
from ..git.snapshots import CommitReader
from ..git.vocabularies import (ChangeType, OverlayKind, OverlayState, Side,
                                WorktreeLayer)
from . import OVERLAY_BUILDER_VERSION
from . import evidence as ev
from . import segmentation as seg
from . import store


@dataclass
class RepoPlan:
    """One repository's resolved participation in an overlay."""
    repository: Dict[str, Any]
    base_ref: str = ""
    head_ref: str = ""
    target_branch: str = ""
    base_commit: str = ""
    head_commit: str = ""
    merge_base_commit: str = ""
    base_tree: str = ""
    head_tree: str = ""
    branch_name: str = ""
    worktree_hash: str = ""
    dirty_state: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildResult:
    overlay_repository_ids: List[str] = field(default_factory=list)
    file_count: int = 0
    segment_count: int = 0
    evidence_count: int = 0
    partial: bool = False
    warnings: List[str] = field(default_factory=list)
    source_hash: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
        self.partial = True


class OverlayBuilder:
    """Builds one overlay's file/segment/evidence layer for one workspace."""

    def __init__(self, workspace_id: str, overlay_id: str,
                 overlay_revision: int, *, runner=None) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id
        self.overlay_revision = overlay_revision
        self.runner = runner or default_runner()
        self._total_blob_bytes = 0

    # -- ref resolution -----------------------------------------------------
    def resolve_repo_plan(self, repository: Dict[str, Any], *, kind: str,
                          base_ref: str = "", head_ref: str = "",
                          target_branch: str = "") -> RepoPlan:
        """Resolve commits, trees and merge-base for one repository (spec §7)."""
        root = resolve_root(self.workspace_id, repository)
        resolver = RefResolver(root, self.runner,
                               repository_key=repository.get("repository_key", ""))
        plan = RepoPlan(repository=repository, base_ref=base_ref,
                        head_ref=head_ref, target_branch=target_branch)
        if kind == OverlayKind.WORKING_TREE:
            head = resolver.head_commit()
            plan.base_commit = head or ""
            plan.head_commit = ""     # worktree, not a commit
            plan.base_tree = resolver.tree_of(head) if head else ""
            symbolic = resolver.symbolic_head()
            plan.branch_name = symbolic.rsplit("/", 1)[-1] if symbolic else ""
            plan.base_ref = "HEAD"
            return plan
        plan.base_commit = resolver.resolve_commit(base_ref)
        plan.head_commit = resolver.resolve_commit(head_ref)
        plan.head_tree = resolver.tree_of(plan.head_commit)
        if kind in (OverlayKind.BRANCH, OverlayKind.PR):
            plan.merge_base_commit = resolver.merge_base(base_ref, head_ref)
            plan.target_branch = target_branch or base_ref
            symbolic = resolver.symbolic_head()
            plan.branch_name = head_ref
        else:  # commit-range
            plan.merge_base_commit = plan.base_commit
        # The diff base is the merge-base (branch/PR) or the base commit.
        diff_base = plan.merge_base_commit or plan.base_commit
        plan.base_tree = resolver.tree_of(diff_base) if diff_base else ""
        return plan

    # -- worktree hashing (spec §32) ---------------------------------------
    def worktree_hash(self, root: str, staged, unstaged, untracked_paths
                      ) -> str:
        """Deterministic worktree hash from HEAD + staged/unstaged/untracked
        content identities — never timestamps."""
        resolver = RefResolver(root, self.runner)
        h = hashlib.sha256()
        h.update((resolver.head_commit() or "").encode())
        for change in sorted(staged, key=lambda c: c.path):
            h.update(b"S")
            h.update(f"{change.path}:{change.new_blob_sha}".encode())
        for change in sorted(unstaged, key=lambda c: c.path):
            h.update(b"U")
            h.update(f"{change.path}:{change.new_content_blob_hash}".encode())
        for p in sorted(untracked_paths):
            h.update(b"T")
            h.update(p.encode())
        return h.hexdigest()

    # -- the build ----------------------------------------------------------
    def build_repo(self, plan: RepoPlan, *, kind: str,
                   result: BuildResult) -> None:
        """Diff, snapshot, segment and persist one repository's changes."""
        repo = plan.repository
        root = resolve_root(self.workspace_id, repo)
        reader = ContentReader(root, self.workspace_id, self.runner)
        dx = DiffExtractor(root, self.runner)

        if kind == OverlayKind.WORKING_TREE:
            if dx.has_unmerged():
                result.warn("working_tree_unmerged")
                return
            staged = dx.diff_staged().changes
            unstaged = dx.diff_unstaged().changes
            untracked = dx.untracked_paths()
            # Snapshot unstaged content hashes first (needed by the worktree
            # hash), then compute it.
            self._fill_worktree_content(root, reader, unstaged)
            plan.worktree_hash = self.worktree_hash(root, staged, unstaged,
                                                    untracked)
            plan.dirty_state = {"staged": len(staged),
                                "unstaged": len(unstaged),
                                "untracked": len(untracked)}
            changes = self._merge_worktree_layers(staged, unstaged, untracked,
                                                  root, reader)
        else:
            diff_base = plan.merge_base_commit or plan.base_commit
            diff = dx.diff_commits(diff_base, plan.head_commit)
            if diff.partial:
                for w in diff.warnings:
                    result.warn(w)
                result.summary["omitted_files"] = diff.omitted
            changes = diff.changes

        ovr_id = store.add_overlay_repository(
            self.overlay_id, repo["id"],
            base_ref=plan.base_ref, head_ref=plan.head_ref,
            base_commit=plan.base_commit, head_commit=plan.head_commit,
            merge_base_commit=plan.merge_base_commit,
            base_tree=plan.base_tree, head_tree=plan.head_tree,
            branch_name=plan.branch_name, target_branch=plan.target_branch,
            worktree_hash=plan.worktree_hash, dirty_state=plan.dirty_state)
        result.overlay_repository_ids.append(ovr_id)

        for fc in changes:
            self._build_file(plan, ovr_id, fc, reader, dx, kind, result)

    # -- per-file -----------------------------------------------------------
    def _build_file(self, plan: RepoPlan, ovr_id: str, fc: FileChange,
                    reader: ContentReader, dx: DiffExtractor, kind: str,
                    result: BuildResult) -> None:
        repo_key = plan.repository.get("repository_key", "")
        # Snapshot committed before/after content (working-tree content is
        # already snapshotted during layer merge).
        before_bytes: Optional[bytes] = None
        after_bytes: Optional[bytes] = None
        if kind != OverlayKind.WORKING_TREE:
            if fc.old_blob_sha:
                fc.old_content_blob_hash, before_bytes = reader.snapshot_blob(
                    fc.old_blob_sha)
            if fc.new_blob_sha:
                fc.new_content_blob_hash, after_bytes = reader.snapshot_blob(
                    fc.new_blob_sha)
        else:
            before_bytes = fc.metadata.get("_before_bytes")
            after_bytes = fc.metadata.get("_after_bytes")
            fc.metadata.pop("_before_bytes", None)
            fc.metadata.pop("_after_bytes", None)

        # Classify special content (spec §12, §16).
        after_info = reader.classify(after_bytes, fc.new_mode)
        before_info = reader.classify(before_bytes, fc.old_mode)
        fc.is_binary = bool(after_info["is_binary"] or before_info["is_binary"])
        fc.is_symlink = bool(fc.is_symlink or after_info["is_symlink"]
                             or before_info["is_symlink"])
        fc.is_submodule = bool(fc.is_submodule or after_info["is_submodule"])
        fc.is_lfs_pointer = bool(after_info["is_lfs_pointer"])
        if after_info["lfs"]:
            fc.metadata["lfs"] = after_info["lfs"]
            fc.status = "unsupported"
        if fc.is_submodule:
            fc.metadata["submodule"] = {"old": fc.old_blob_sha,
                                        "new": fc.new_blob_sha}
            fc.status = "unsupported"

        # Changed ranges (spec §15) — skip for binary/symlink/submodule/lfs.
        before_ranges: List[ChangedRange] = []
        after_ranges: List[ChangedRange] = []
        if not (fc.is_binary or fc.is_submodule or fc.is_lfs_pointer):
            before_ranges, after_ranges = self._ranges(fc, before_bytes,
                                                        after_bytes, dx, kind)
        fc.before_ranges = before_ranges
        fc.after_ranges = after_ranges
        fc.additions = sum(r.count for r in after_ranges)
        fc.deletions = sum(r.count for r in before_ranges)

        changed_ranges_json = {
            "before": [r.to_dict() for r in before_ranges],
            "after": [r.to_dict() for r in after_ranges],
        }
        file_id = store.add_file(self.overlay_id, ovr_id, fc,
                                 changed_ranges=changed_ranges_json)
        result.file_count += 1

        # Segments — only for parseable, non-special, content-bearing changes.
        if fc.is_binary or fc.is_symlink or fc.is_submodule or fc.is_lfs_pointer:
            return
        self._build_segments(plan, file_id, fc, before_bytes, after_bytes,
                             before_ranges, after_ranges, kind, result)

    def _ranges(self, fc: FileChange, before_bytes, after_bytes, dx, kind
                ) -> Tuple[List[ChangedRange], List[ChangedRange]]:
        if kind != OverlayKind.WORKING_TREE and fc.old_blob_sha and fc.new_blob_sha:
            return dx.blob_ranges(fc.old_blob_sha, fc.new_blob_sha)
        # working-tree or add/delete: compute from the bytes we hold.
        bt = before_bytes.decode("utf-8", "replace") if before_bytes else ""
        at = after_bytes.decode("utf-8", "replace") if after_bytes else ""
        return changed_ranges_from_text(bt, at)

    def _build_segments(self, plan: RepoPlan, file_id: str, fc: FileChange,
                        before_bytes, after_bytes, before_ranges, after_ranges,
                        kind: str, result: BuildResult) -> None:
        before_path = fc.old_path or fc.new_path
        after_path = fc.new_path or fc.old_path
        before_segs = seg.segment_side(before_path, before_bytes) \
            if fc.change_type != ChangeType.ADDED else []
        after_segs = seg.segment_side(after_path, after_bytes) \
            if fc.change_type != ChangeType.DELETED else []
        path_moved = fc.change_type in (ChangeType.RENAMED, ChangeType.COPIED) \
            and fc.old_path != fc.new_path
        before_out, after_out = seg.classify_segments(
            before_segs, after_segs, before_ranges, after_ranges,
            path_moved=path_moved)

        cap = config.GIT_MAX_SEGMENTS_PER_FILE
        self._persist_segments(plan, file_id, fc, Side.BEFORE, before_out,
                               before_bytes, kind, result, cap)
        self._persist_segments(plan, file_id, fc, Side.AFTER, after_out,
                               after_bytes, kind, result, cap)

    def _persist_segments(self, plan: RepoPlan, file_id: str, fc: FileChange,
                          side: str, segs, side_bytes, kind: str,
                          result: BuildResult, cap: int) -> None:
        repo_key = plan.repository.get("repository_key", "")
        content_blob_hash = ""
        if side_bytes is not None:
            content_blob_hash = content_store.put(self.workspace_id, side_bytes)
        path = (fc.old_path if side == Side.BEFORE else fc.new_path) or fc.path
        commit = (plan.merge_base_commit or plan.base_commit) \
            if side == Side.BEFORE else plan.head_commit
        persisted = 0
        for s in segs:
            if persisted >= cap:
                result.warn(f"segment limit {cap} reached for {path}")
                break
            sid = store.add_segment(
                self.overlay_id, file_id, side=side,
                segment_key=s["segment_key"], segment_type=s["segment_type"],
                change_class=s.get("change_class", "unchanged"),
                ordinal=s.get("ordinal", 0), start_line=s.get("start_line", 0),
                end_line=s.get("end_line", 0), symbol=s.get("symbol", ""),
                content_hash=s.get("content_hash", ""),
                content_blob_hash=content_blob_hash,
                content_mode=s.get("content_mode", "verbatim"),
                metadata=s.get("metadata", {}))
            result.segment_count += 1
            persisted += 1
            # One before/after Evidence per changed segment (spec §18).
            if s.get("change_class") in ("added", "modified", "deleted"):
                self._persist_evidence(plan, file_id, sid, side, s, path,
                                       commit, kind, result)

    def _persist_evidence(self, plan: RepoPlan, file_id: str, segment_id: str,
                          side: str, s: Dict[str, Any], path: str, commit: str,
                          kind: str, result: BuildResult) -> None:
        repo_key = plan.repository.get("repository_key", "")
        evd = s.get("evidence") or {}
        excerpt = evd.get("excerpt", "")
        content_hash = evd.get("content_hash", s.get("content_hash", ""))
        if kind == OverlayKind.WORKING_TREE:
            locator = ev.worktree_range_locator(
                repository=repo_key, overlay_id=self.overlay_id,
                overlay_revision=self.overlay_revision, side=side,
                base_commit=plan.base_commit, worktree_hash=plan.worktree_hash,
                layer=s.get("metadata", {}).get("layer", "unstaged"),
                path=path, start_line=s.get("start_line", 0),
                end_line=s.get("end_line", 0), symbol=s.get("symbol", ""))
        else:
            locator = ev.blob_range_locator(
                repository=repo_key, overlay_id=self.overlay_id,
                overlay_revision=self.overlay_revision, side=side,
                commit=commit, path=path, start_line=s.get("start_line", 0),
                end_line=s.get("end_line", 0), symbol=s.get("symbol", ""))
        store.add_evidence(self.overlay_id, overlay_file_id=file_id,
                           segment_id=segment_id, side=side, locator=locator,
                           excerpt=excerpt, content_hash=content_hash)
        result.evidence_count += 1

    # -- working-tree helpers ----------------------------------------------
    def _fill_worktree_content(self, root: str, reader: ContentReader,
                               unstaged) -> None:
        import os
        for fc in unstaged:
            abs_path = os.path.join(root, fc.new_path or fc.old_path)
            data = reader.read_worktree_file(abs_path)
            if data is not None:
                fc.new_content_blob_hash = reader.snapshot_bytes(data)

    def _merge_worktree_layers(self, staged, unstaged, untracked_paths, root,
                               reader) -> List[FileChange]:
        """Combine the three working-tree layers into one change list, keeping
        layer provenance (spec §32). The after state is the worktree."""
        import os
        by_path: Dict[str, FileChange] = {}
        for fc in staged:
            fc.layer = WorktreeLayer.STAGED
            fc.metadata["layer"] = WorktreeLayer.STAGED
            by_path[fc.path] = fc
        for fc in unstaged:
            fc.layer = WorktreeLayer.UNSTAGED
            fc.metadata["layer"] = WorktreeLayer.UNSTAGED
            by_path[fc.path] = fc      # unstaged supersedes staged for after-state
        # Attach before/after bytes for parsing.
        for path, fc in by_path.items():
            before = reader.read_index_blob(path) if fc.old_blob_sha or \
                fc.change_type != ChangeType.ADDED else None
            after = reader.read_worktree_file(os.path.join(root, path))
            fc.metadata["_before_bytes"] = before
            fc.metadata["_after_bytes"] = after
        # Untracked -> added files.
        for p in untracked_paths:
            data = reader.read_worktree_file(os.path.join(root, p))
            fc = FileChange(change_type=ChangeType.ADDED, new_path=p,
                            layer=WorktreeLayer.UNTRACKED)
            fc.metadata["layer"] = WorktreeLayer.UNTRACKED
            fc.metadata["_after_bytes"] = data
            fc.metadata["_before_bytes"] = None
            by_path[p] = fc
        return [by_path[k] for k in sorted(by_path)]

    # -- source hash (spec §31) --------------------------------------------
    def source_hash(self, plans: List[RepoPlan], *, kind: str,
                    base_knowledge_revision: int, base_policy_checksum: str,
                    projector_version: str, trace_engine_version: str,
                    detector_versions: str, options: Dict[str, Any]) -> str:
        h = hashlib.sha256()
        h.update(f"adapter:{GIT_ADAPTER_VERSION}".encode())
        h.update(f"builder:{OVERLAY_BUILDER_VERSION}".encode())
        h.update(f"kind:{kind}".encode())
        for plan in sorted(plans, key=lambda p: p.repository.get("repository_key", "")):
            h.update(plan.repository.get("repository_key", "").encode())
            h.update(f"{plan.base_commit}:{plan.head_commit}:"
                     f"{plan.merge_base_commit}:{plan.base_tree}:"
                     f"{plan.head_tree}:{plan.worktree_hash}".encode())
        h.update(f"kr:{base_knowledge_revision}".encode())
        h.update(f"pol:{base_policy_checksum}".encode())
        h.update(f"proj:{projector_version}".encode())
        h.update(f"trace:{trace_engine_version}".encode())
        h.update(f"detect:{detector_versions}".encode())
        h.update(json.dumps(options, sort_keys=True).encode())
        return h.hexdigest()


__all__ = ["OverlayBuilder", "RepoPlan", "BuildResult"]
