"""Diff extraction (spec §14).

Reads machine-readable, NUL-delimited Git diff output — never localized human
output. The authoritative structural list comes from ``git diff --raw -z`` with
rename/copy detection; per-file changed ranges and content snapshots are filled
in by the builder using :mod:`openmind.git.hunks` and
:mod:`openmind.git.content`.

Produces a :class:`~openmind.git.models.DiffResult` that is honest about its
bounds: exceeding a configured limit sets ``partial`` and records a warning
naming the limit, never a silent truncation.
"""
from __future__ import annotations

from typing import List, Optional

from .. import config
from .command import GitCommandRunner, default_runner
from .content import MODE_SUBMODULE, MODE_SYMLINK
from .errors import GitCommandFailed
from .models import DiffResult, FileChange
from .vocabularies import ChangeType, WorktreeLayer


def _base_flags(detect_renames: bool) -> List[str]:
    flags = ["--raw", "-z", "--no-ext-diff", "--no-textconv", "--no-color"]
    if detect_renames:
        flags += ["-M", "-C"]
    else:
        flags += ["--no-renames"]
    return flags


class DiffExtractor:
    """Structural diff reader for one repository."""

    def __init__(self, repo_root: str, runner: Optional[GitCommandRunner] = None,
                 *, detect_renames: bool = True) -> None:
        self.repo_root = str(repo_root)
        self.runner = runner or default_runner()
        self.detect_renames = detect_renames

    # -- committed diffs ----------------------------------------------------
    def diff_commits(self, base_commit: str, head_commit: str) -> DiffResult:
        """Changes introduced going from *base_commit* to *head_commit*."""
        args = ["diff", *_base_flags(self.detect_renames),
                "--end-of-options", base_commit, head_commit]
        return self._run_raw(args)

    # -- working-tree layers (spec §32) ------------------------------------
    def diff_staged(self) -> DiffResult:
        """HEAD -> index (staged changes)."""
        args = ["diff", "--cached", *_base_flags(self.detect_renames)]
        res = self._run_raw(args)
        for c in res.changes:
            c.layer = WorktreeLayer.STAGED
        return res

    def diff_unstaged(self) -> DiffResult:
        """index -> worktree (unstaged changes to tracked files)."""
        args = ["diff", *_base_flags(self.detect_renames)]
        res = self._run_raw(args)
        for c in res.changes:
            c.layer = WorktreeLayer.UNSTAGED
        return res

    def untracked_paths(self) -> List[str]:
        """Paths Git neither tracks nor ignores, deterministically ordered and
        bounded (spec §32). Ignored files are excluded via
        ``--exclude-standard``."""
        res = self.runner.run(
            self.repo_root,
            ["ls-files", "--others", "--exclude-standard", "-z"])
        paths = [p for p in res.text().split("\x00") if p.strip()]
        paths.sort()
        return paths[:config.GIT_MAX_UNTRACKED_FILES]

    # -- hunk ranges (spec §15) --------------------------------------------
    def blob_ranges(self, old_blob_sha: str, new_blob_sha: str):
        """Changed ranges between two committed blobs, via ``git diff
        --unified=0`` (spec §15). Returns ``(before_ranges, after_ranges)``.

        Diffing the blob objects directly (rather than a pathspec) makes this
        work uniformly for renames-with-content-change, where the path differs
        between the two sides."""
        from .hunks import parse_unified_hunks
        if not old_blob_sha or not new_blob_sha:
            return [], []
        res = self.runner.run(
            self.repo_root,
            ["diff", "--unified=0", "--no-ext-diff", "--no-textconv",
             "--no-color", "--end-of-options", old_blob_sha, new_blob_sha],
            check=False)
        if not res.ok:
            return [], []
        return parse_unified_hunks(res.text(errors="surrogateescape"))

    def has_unmerged(self) -> bool:
        """True if the index has any unmerged (conflicted) entries (spec §14).
        Such a working tree cannot produce a reliable impact report."""
        res = self.runner.run(self.repo_root, ["ls-files", "--unmerged", "-z"],
                              check=False)
        return res.ok and bool(res.text().strip())

    def is_worktree_clean(self) -> bool:
        """True if there are no staged, unstaged or untracked changes."""
        res = self.runner.run(
            self.repo_root, ["status", "--porcelain", "-z", "--untracked-files=normal"])
        return not res.text().strip()

    # -- parsing ------------------------------------------------------------
    def _run_raw(self, args: List[str]) -> DiffResult:
        try:
            res = self.runner.run(self.repo_root, args)
        except GitCommandFailed as exc:
            result = DiffResult()
            result.add_warning(f"diff failed: {exc.message}")
            return result
        return self._parse_raw_z(res.text(errors="surrogateescape"))

    def _parse_raw_z(self, output: str) -> DiffResult:
        """Parse ``git diff --raw -z`` into FileChange rows.

        Record shape (NUL-delimited): a metadata token
        ``:<old_mode> <new_mode> <old_sha> <new_sha> <STATUS>`` followed by one
        path token (two for rename/copy). STATUS is a letter optionally
        followed by a similarity score for R/C."""
        tokens = output.split("\x00")
        result = DiffResult()
        i = 0
        n = len(tokens)
        while i < n:
            meta = tokens[i]
            if not meta:
                i += 1
                continue
            if not meta.startswith(":"):
                # Unexpected stray token; skip rather than misparse.
                i += 1
                continue
            fields = meta[1:].split(" ")
            if len(fields) < 5:
                i += 1
                continue
            old_mode, new_mode, old_sha, new_sha, status = fields[:5]
            change_type = ChangeType.from_status(status)
            similarity = 0
            if status and status[0] in ("R", "C") and status[1:].isdigit():
                similarity = int(status[1:])
            fc = FileChange(
                change_type=change_type,
                old_mode=old_mode, new_mode=new_mode,
                old_blob_sha=_norm_sha(old_sha),
                new_blob_sha=_norm_sha(new_sha),
                similarity=similarity)
            # Path tokens follow the metadata token.
            if change_type in (ChangeType.RENAMED, ChangeType.COPIED):
                if i + 2 < n:
                    fc.old_path = tokens[i + 1]
                    fc.new_path = tokens[i + 2]
                    i += 3
                else:
                    i = n
            else:
                if i + 1 < n:
                    path = tokens[i + 1]
                    if change_type == ChangeType.DELETED:
                        fc.old_path = path
                    elif change_type == ChangeType.ADDED:
                        fc.new_path = path
                    else:
                        fc.old_path = path
                        fc.new_path = path
                    i += 2
                else:
                    i = n
            # Mode-derived flags (independent of content).
            if MODE_SUBMODULE in (old_mode, new_mode):
                fc.is_submodule = True
                if fc.change_type not in (ChangeType.ADDED, ChangeType.DELETED):
                    fc.change_type = ChangeType.SUBMODULE
            if new_mode == MODE_SYMLINK or old_mode == MODE_SYMLINK:
                fc.is_symlink = True
            result.changes.append(fc)
            if len(result.changes) >= config.GIT_MAX_CHANGED_FILES:
                # Count the rest as omitted without buffering them.
                remaining = _count_records(tokens, i)
                if remaining:
                    result.omitted += remaining
                    result.add_warning(
                        f"changed-file limit {config.GIT_MAX_CHANGED_FILES} "
                        f"reached; {remaining} further changes omitted")
                break
        result.changes.sort(key=lambda c: (c.path, c.change_type))
        return result


def _norm_sha(sha: str) -> str:
    s = (sha or "").strip()
    return "" if not s or s == "0" * len(s) else s


def _count_records(tokens: List[str], start: int) -> int:
    """Approximate remaining record count from the tail of the token stream —
    each record is a metadata token plus 1–2 paths; count metadata tokens."""
    return sum(1 for t in tokens[start:] if t.startswith(":"))


__all__ = ["DiffExtractor"]
