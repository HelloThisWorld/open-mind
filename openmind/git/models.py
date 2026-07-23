"""Plain data shapes for the Git plane.

These are lightweight dataclasses used between the Git-object-interpretation
modules (diff, hunks, content, snapshots) and the store/service. They carry no
behavior beyond serialization helpers and hold only portable data — never an
absolute path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .vocabularies import ChangeType


@dataclass
class Repository:
    """A discovered/registered Git repository, portable identity only."""
    repository_key: str
    relative_root: str = ""
    object_format: str = "sha1"
    is_bare: bool = False
    default_branch: str = ""
    repository_id: str = ""
    workspace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repositoryId": self.repository_id,
            "repositoryKey": self.repository_key,
            "relativeRoot": self.relative_root,
            "objectFormat": self.object_format,
            "isBare": self.is_bare,
            "defaultBranch": self.default_branch,
        }


@dataclass
class CommitInfo:
    """Bounded commit metadata (spec §14): no author email by default."""
    sha: str
    parents: List[str] = field(default_factory=list)
    subject: str = ""
    author_name: str = ""
    author_time: str = ""
    committer_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "parents": list(self.parents),
            "subject": self.subject,
            "authorName": self.author_name,
            "authorTime": self.author_time,
            "committerTime": self.committer_time,
        }


@dataclass
class ChangedRange:
    """A 1-based, inclusive changed line range on one side of a file."""
    start: int
    count: int

    @property
    def end(self) -> int:
        # A zero-count hunk marks an insertion/deletion point; its "end" is the
        # anchor line itself so intersection tests stay well-defined.
        return self.start + max(self.count, 1) - 1

    def intersects(self, start: int, end: int) -> bool:
        return not (self.end < start or self.start > end)

    def to_dict(self) -> Dict[str, int]:
        return {"start": self.start, "count": self.count}


@dataclass
class FileChange:
    """One changed path in a diff, fully classified (spec §14, §16)."""
    change_type: str = ChangeType.UNKNOWN
    old_path: str = ""
    new_path: str = ""
    old_mode: str = ""
    new_mode: str = ""
    old_blob_sha: str = ""
    new_blob_sha: str = ""
    similarity: int = 0
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    is_symlink: bool = False
    is_submodule: bool = False
    is_lfs_pointer: bool = False
    before_ranges: List[ChangedRange] = field(default_factory=list)
    after_ranges: List[ChangedRange] = field(default_factory=list)
    # Content snapshot hashes (filled by the content reader), plus opaque
    # metadata (LFS oid/size, submodule commits, worktree layer, …).
    old_content_blob_hash: str = ""
    new_content_blob_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"          # ok | partial | unsupported | error
    layer: str = ""             # working-tree provenance (staged/unstaged/untracked)

    @property
    def path(self) -> str:
        """The path that identifies this change now (new side, or old for a
        delete)."""
        return self.new_path or self.old_path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "changeType": self.change_type,
            "oldPath": self.old_path,
            "newPath": self.new_path,
            "oldMode": self.old_mode,
            "newMode": self.new_mode,
            "oldBlobSha": self.old_blob_sha,
            "newBlobSha": self.new_blob_sha,
            "similarity": self.similarity,
            "additions": self.additions,
            "deletions": self.deletions,
            "isBinary": self.is_binary,
            "isSymlink": self.is_symlink,
            "isSubmodule": self.is_submodule,
            "isLfsPointer": self.is_lfs_pointer,
            "beforeRanges": [r.to_dict() for r in self.before_ranges],
            "afterRanges": [r.to_dict() for r in self.after_ranges],
            "oldContentBlobHash": self.old_content_blob_hash,
            "newContentBlobHash": self.new_content_blob_hash,
            "status": self.status,
            "layer": self.layer,
            "metadata": dict(self.metadata),
        }


@dataclass
class DiffResult:
    """The outcome of extracting a diff: the changes plus honest bounds."""
    changes: List[FileChange] = field(default_factory=list)
    partial: bool = False
    warnings: List[str] = field(default_factory=list)
    omitted: int = 0

    def add_warning(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)
        self.partial = True


__all__ = [
    "Repository", "CommitInfo", "ChangedRange", "FileChange", "DiffResult",
]
