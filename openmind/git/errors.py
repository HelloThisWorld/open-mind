"""Typed errors for the Git plane and the Overlay plane.

Same taxonomy discipline as the rest of OpenMind: every failure a caller must
react to differently is its own class, all extending
:class:`openmind.domain.errors.OpenMindError`, so the CLI, REST and MCP
adapters translate them uniformly (machine-readable ``code``, honest
``http_status``, stable ``exit_code``).

The ``code`` strings here are part of the Phase 7 contract — the spec names
several of them verbatim (``ref_not_available_locally``,
``merge_base_unavailable``, ``base_commit_mismatch``, ``baseline_dirty``,
``working_tree_unmerged``, …) — so tests assert on them and they must not
drift.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..domain.errors import OpenMindError


class GitError(OpenMindError):
    """Base class for every Git-plane error."""

    code = "git_error"
    exit_code = 4
    http_status = 500


# ---------------------------------------------------------------------------
# Command boundary
# ---------------------------------------------------------------------------
class GitUnavailable(GitError):
    """The ``git`` executable is not available on this machine."""

    code = "git_unavailable"
    exit_code = 3
    http_status = 503


class GitCommandDenied(GitError):
    """A caller asked the runner to execute a subcommand or option that the
    read-only allow-list forbids. Raised BEFORE any process is spawned — this
    is the guard that keeps Phase 7 a Git reader."""

    code = "git_command_denied"
    exit_code = 2
    http_status = 400

    def __init__(self, message: str, *, subcommand: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if subcommand:
            merged.setdefault("subcommand", subcommand)
        super().__init__(message, details=merged)
        self.subcommand = subcommand


class GitCommandFailed(GitError):
    """A permitted Git command ran but exited non-zero (and the failure is not
    one of the semantically-typed cases below)."""

    code = "git_command_failed"
    exit_code = 4
    http_status = 500

    def __init__(self, message: str, *, returncode: int = 0,
                 stderr: str = "", args: Optional[List[str]] = None) -> None:
        super().__init__(
            message,
            details={"returncode": returncode,
                     "stderr": (stderr or "")[:2000],
                     "args": list(args or [])})
        self.returncode = returncode
        self.stderr = stderr


class GitCommandTimeout(GitError):
    """A Git command exceeded its bounded timeout and was terminated."""

    code = "git_command_timeout"
    exit_code = 5
    http_status = 504


class GitOutputTooLarge(GitError):
    """A Git command produced more output than the configured bound."""

    code = "git_output_too_large"
    exit_code = 4
    http_status = 500


class GitRepositoryUnsafe(GitError):
    """Git refused the repository because its owner differs from the current
    user. OpenMind does NOT bypass this by writing ``safe.directory``; the user
    must fix ownership explicitly."""

    code = "git_repository_unsafe"
    exit_code = 2
    http_status = 400


# ---------------------------------------------------------------------------
# Refs & objects
# ---------------------------------------------------------------------------
class InvalidRef(GitError):
    """A caller-supplied ref is syntactically unsafe (empty, ``-``-leading,
    contains NUL/control characters). Rejected before it can reach Git."""

    code = "invalid_ref"
    exit_code = 2
    http_status = 400

    def __init__(self, ref: str, reason: str = "") -> None:
        super().__init__(
            f"invalid git ref {ref!r}" + (f": {reason}" if reason else ""),
            details={"ref": ref, "reason": reason})
        self.ref = ref


class RefNotAvailableLocally(GitError):
    """A ref does not resolve in the local repository. OpenMind never fetches
    to obtain it."""

    code = "ref_not_available_locally"
    exit_code = 1
    http_status = 404

    def __init__(self, ref: str, *, repository: str = "") -> None:
        super().__init__(
            f"ref not available locally: {ref!r}",
            details={"ref": ref, "repository": repository})
        self.ref = ref


class MergeBaseUnavailable(GitError):
    """No merge-base exists locally for the requested base/head — commonly a
    shallow clone. OpenMind never fetches to complete history."""

    code = "merge_base_unavailable"
    exit_code = 1
    http_status = 409

    def __init__(self, base: str, head: str, *, repository: str = "",
                 possibly_shallow: bool = False) -> None:
        super().__init__(
            f"no local merge-base for {base!r}..{head!r}",
            details={"base": base, "head": head, "repository": repository,
                     "possibly_shallow_clone": bool(possibly_shallow)})
        self.base = base
        self.head = head


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
class RepositoryNotFound(GitError):
    """No registered Git repository resolves the given key or path in this
    workspace."""

    code = "git_repository_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, key: str, *, workspace_id: str = "") -> None:
        super().__init__(
            f"git repository not found: {key!r}",
            details={"repository_key": key, "workspace_id": workspace_id})
        self.key = key


class RepositoryUnavailable(GitError):
    """A registered repository's local root is not present on this machine
    (e.g. ``data/`` copied to a machine that has not re-pointed the root)."""

    code = "git_repository_unavailable"
    exit_code = 3
    http_status = 409


# ---------------------------------------------------------------------------
# Baseline coherence
# ---------------------------------------------------------------------------
class BaselineError(GitError):
    """Base class for baseline-coherence failures."""

    code = "baseline_error"
    exit_code = 1
    http_status = 409


class BaselineDirty(BaselineError):
    """A baseline cannot be captured because the worktree or index is dirty,
    or an Asset Revision reports dirty source metadata."""

    code = "baseline_dirty"


class BaselineNotCaptured(BaselineError):
    """Overlay planning requires a baseline that has not been captured."""

    code = "baseline_not_captured"


class BaseCommitMismatch(BaselineError):
    """The requested base commit does not match the captured baseline commit —
    computing canonical impact against it would be against the wrong graph."""

    code = "base_commit_mismatch"


class BaseKnowledgeRevisionMissing(BaselineError):
    """The Base Knowledge Revision needed to interpret the overlay is unknown
    (no graph has been projected for this workspace)."""

    code = "base_knowledge_revision_missing"


__all__ = [
    "GitError", "GitUnavailable", "GitCommandDenied", "GitCommandFailed",
    "GitCommandTimeout", "GitOutputTooLarge", "GitRepositoryUnsafe",
    "InvalidRef", "RefNotAvailableLocally", "MergeBaseUnavailable",
    "RepositoryNotFound", "RepositoryUnavailable",
    "BaselineError", "BaselineDirty", "BaselineNotCaptured",
    "BaseCommitMismatch", "BaseKnowledgeRevisionMissing",
]
