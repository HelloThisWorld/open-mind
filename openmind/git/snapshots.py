"""Commit and tree snapshots (spec §6, §14).

Reads bounded commit metadata (SHA, parents, subject, timestamp — never an
author email by default, spec §14) and resolves tree object names, all through
the read-only command boundary with NUL-delimited formats.
"""
from __future__ import annotations

from typing import List, Optional

from .. import config
from .command import GitCommandRunner, default_runner
from .errors import GitCommandFailed
from .models import CommitInfo
from .refs import RefResolver

#: A NUL-delimited commit format: sha, parents, subject, author name, author
#: date (ISO strict), committer date. Author EMAIL is deliberately omitted.
_COMMIT_FORMAT = "%H%x00%P%x00%s%x00%an%x00%aI%x00%cI"


class CommitReader:
    def __init__(self, repo_root: str, runner: Optional[GitCommandRunner] = None
                 ) -> None:
        self.repo_root = str(repo_root)
        self.runner = runner or default_runner()

    def commit_info(self, commit: str) -> CommitInfo:
        """Bounded metadata for one commit."""
        res = self.runner.run(
            self.repo_root,
            ["show", "-s", f"--format={_COMMIT_FORMAT}", "--no-color",
             "--end-of-options", commit])
        return self._parse(res.text())

    def _parse(self, text: str) -> CommitInfo:
        parts = text.strip("\n").split("\x00")
        if len(parts) < 6:
            parts += [""] * (6 - len(parts))
        sha, parents, subject, author_name, author_time, committer_time = parts[:6]
        return CommitInfo(
            sha=sha.strip(),
            parents=[p for p in parents.split() if p],
            subject=subject.strip()[:500],
            author_name=author_name.strip()[:200],
            author_time=author_time.strip(),
            committer_time=committer_time.strip())

    def commits_in_range(self, base_commit: str, head_commit: str, *,
                         limit: Optional[int] = None) -> List[CommitInfo]:
        """Commits reachable from head but not from base (``base..head``),
        newest-first, bounded by ``GIT_MAX_COMMITS_PER_OVERLAY``."""
        cap = limit or config.GIT_MAX_COMMITS_PER_OVERLAY
        res = self.runner.run(
            self.repo_root,
            ["rev-list", f"--max-count={cap + 1}", "--end-of-options",
             f"{base_commit}..{head_commit}"], check=False)
        if not res.ok:
            return []
        shas = [s for s in res.text().split() if s]
        infos = [self.commit_info(s) for s in shas[:cap]]
        return infos

    def tree_of(self, commit: str) -> str:
        return RefResolver(self.repo_root, self.runner).tree_of(commit)


__all__ = ["CommitReader"]
