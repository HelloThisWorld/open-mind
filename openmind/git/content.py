"""Reading Git content without a checkout, and classifying it (spec §16).

Committed blobs are read through ``git cat-file --batch`` (batched for
efficiency) and immediately snapshotted into OpenMind's immutable
content-addressed store, so overlay Evidence stays valid even if Git later
garbage-collects the object. Working-tree content is read from the index
(staged), the worktree (unstaged / untracked) and likewise snapshotted.

Special content is treated honestly, never coerced into a text parser:

* ``120000`` symlink  -> the link *target text* is the blob; never followed;
* ``160000`` submodule -> opaque; only the old/new commit is recorded;
* Git LFS pointer      -> detected structurally; oid + size recorded; the LFS
                          object is never downloaded;
* binary               -> hash + metadata only; never embedded or parsed.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .. import config, content_store
from .command import GitCommandRunner, default_runner
from .errors import GitCommandFailed

# Git file modes we care about.
MODE_SYMLINK = "120000"
MODE_SUBMODULE = "160000"
MODE_REGULAR = "100644"
MODE_EXECUTABLE = "100755"

#: A Git LFS pointer is a tiny, fixed-shape text file. Match it structurally so
#: a legitimate small text file that merely mentions git-lfs is not misread.
_LFS_VERSION_RE = re.compile(rb"^version https://git-lfs\.github\.com/spec/")
_LFS_OID_RE = re.compile(rb"(?m)^oid sha256:([0-9a-f]{64})\s*$")
_LFS_SIZE_RE = re.compile(rb"(?m)^size (\d+)\s*$")


def is_lfs_pointer(data: bytes) -> Optional[Dict[str, object]]:
    """Return ``{oid, size}`` if *data* is a Git LFS pointer, else None."""
    if not data or len(data) > 1024 or not _LFS_VERSION_RE.match(data):
        return None
    oid = _LFS_OID_RE.search(data)
    size = _LFS_SIZE_RE.search(data)
    if not oid or not size:
        return None
    return {"oid": oid.group(1).decode("ascii"), "size": int(size.group(1))}


def looks_binary(data: bytes, *, sniff: int = 8192) -> bool:
    """Heuristic matching Git's own: a NUL byte in the first chunk => binary."""
    if not data:
        return False
    return b"\x00" in data[:sniff]


class ContentReader:
    """Batched blob reader for one repository, snapshotting into the store."""

    def __init__(self, repo_root: str, workspace_id: str,
                 runner: Optional[GitCommandRunner] = None) -> None:
        self.repo_root = str(repo_root)
        self.workspace_id = workspace_id
        self.runner = runner or default_runner()
        self._cache: Dict[str, bytes] = {}     # blob sha -> bytes (per build)

    # -- committed blobs ----------------------------------------------------
    def read_blob(self, blob_sha: str, *, max_bytes: Optional[int] = None
                  ) -> Optional[bytes]:
        """The exact bytes of a committed blob, or None if absent/oversize.

        An oversize blob (over ``config.GIT_MAX_BLOB_BYTES`` unless overridden)
        returns None so the caller records it as unanalyzed rather than
        buffering it."""
        sha = (blob_sha or "").strip()
        if not sha or sha == "0" * len(sha):
            return None
        if sha in self._cache:
            return self._cache[sha]
        cap = config.GIT_MAX_BLOB_BYTES if max_bytes is None else max_bytes
        # `cat-file --batch` streams "<sha> <type> <size>\n<payload>\n"; for a
        # single object we can size-check via `-s` first to honor the cap
        # without reading an oversize payload into memory.
        try:
            size_res = self.runner.run(
                self.repo_root, ["cat-file", "-s", sha], check=False)
            if not size_res.ok:
                return None
            size = int(size_res.text().strip() or "0")
        except (GitCommandFailed, ValueError):
            return None
        if size > cap:
            return None
        try:
            res = self.runner.run(
                self.repo_root, ["cat-file", "blob", sha], check=False)
        except GitCommandFailed:
            return None
        if not res.ok:
            return None
        data = res.stdout
        self._cache[sha] = data
        return data

    def snapshot_blob(self, blob_sha: str) -> Tuple[str, Optional[bytes]]:
        """Read a committed blob and store it; return ``(content_hash, bytes)``.
        ``content_hash`` is '' when the blob is absent or oversize."""
        data = self.read_blob(blob_sha)
        if data is None:
            return "", None
        return content_store.put(self.workspace_id, data), data

    # -- working-tree content ----------------------------------------------
    def read_index_blob(self, path: str) -> Optional[bytes]:
        """Staged content: the blob currently in the index for *path*."""
        # `:0:<path>` names the stage-0 index entry.
        res = self.runner.run(
            self.repo_root, ["cat-file", "blob", f":0:{path}"], check=False)
        return res.stdout if res.ok else None

    def read_worktree_file(self, abs_path: str, *,
                           max_bytes: Optional[int] = None) -> Optional[bytes]:
        """Unstaged/untracked content: the file's current bytes on disk.

        This is the ONLY place Phase 7 reads a file from the live filesystem,
        and only for a path Git already reported as changed/untracked. Bounded
        and never follows into a directory."""
        cap = config.GIT_MAX_BLOB_BYTES if max_bytes is None else max_bytes
        import os
        try:
            if os.path.islink(abs_path):
                # Store the link target text, never follow it.
                target = os.readlink(abs_path)
                return target.encode("utf-8", "surrogateescape")
            if not os.path.isfile(abs_path):
                return None
            if os.path.getsize(abs_path) > cap:
                return None
            with open(abs_path, "rb") as fh:
                return fh.read(cap + 1)[:cap]
        except OSError:
            return None

    def snapshot_bytes(self, data: bytes) -> str:
        return content_store.put(self.workspace_id, data)

    # -- classification -----------------------------------------------------
    def classify(self, data: Optional[bytes], mode: str) -> Dict[str, object]:
        """Describe content for a given Git mode without parsing it. Returns
        flags used to route (or refuse to route) it to a parser/embedder."""
        info: Dict[str, object] = {
            "is_symlink": mode == MODE_SYMLINK,
            "is_submodule": mode == MODE_SUBMODULE,
            "is_binary": False,
            "is_lfs_pointer": False,
            "lfs": None,
        }
        if info["is_submodule"] or info["is_symlink"] or data is None:
            return info
        lfs = is_lfs_pointer(data)
        if lfs:
            info["is_lfs_pointer"] = True
            info["lfs"] = lfs
            return info
        info["is_binary"] = looks_binary(data)
        return info


__all__ = [
    "ContentReader", "is_lfs_pointer", "looks_binary",
    "MODE_SYMLINK", "MODE_SUBMODULE", "MODE_REGULAR", "MODE_EXECUTABLE",
]
