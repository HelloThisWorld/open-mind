"""Repository discovery and portable identity (spec §9).

Repositories are found through the workspace's already-registered source paths
(machine-local, Phase 1). Each is given a portable, workspace-relative
``repository_key`` (``git:.``, ``git:services/namecheck``); the absolute root is
resolved at run time from machine-local configuration and NEVER persisted.

Discovery is deduplicated by the repository's resolved Git common directory, so
several registered roots that point into the same repository collapse to one
record.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from .. import machine
from .command import GitCommandRunner, default_runner
from .errors import GitError, RepositoryUnavailable
from .refs import RefResolver
from .store import (find_repository_by_key, get_repository, list_repositories,
                    upsert_repository)


def repository_key_for(workspace_root: str, repo_toplevel: str) -> str:
    """The portable key for a repository at *repo_toplevel*, relative to the
    workspace root. ``git:.`` when the workspace is the repository."""
    rel = machine.relativize(repo_toplevel, workspace_root).strip("/")
    return f"git:{rel or '.'}"


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").rstrip("/")


def _canon(path: str) -> str:
    """Canonicalize a path (resolve symlinks / short names) then normalize its
    separators. This is what makes a repository key stable when the registered
    workspace root and Git's ``--show-toplevel`` disagree on the canonical form
    of the same directory — e.g. macOS ``/var`` -> ``/private/var``, or a
    Windows 8.3 short path — which would otherwise leak an absolute path into
    the key instead of yielding ``git:.``."""
    try:
        return _norm(os.path.realpath(path))
    except OSError:
        return _norm(path)


class RepositoryDiscovery:
    """Discovers and classifies Git repositories for one workspace."""

    def __init__(self, workspace_id: str,
                 runner: Optional[GitCommandRunner] = None) -> None:
        self.workspace_id = workspace_id
        self.runner = runner or default_runner()

    # -- machine-local roots ------------------------------------------------
    def workspace_root(self) -> str:
        return _norm(machine.project_root(self.workspace_id))

    def registered_paths(self) -> List[str]:
        roots = []
        for spec in machine.get_paths(self.workspace_id):
            p = spec.get("path") if isinstance(spec, dict) else None
            if p:
                roots.append(_norm(p))
        return roots

    # -- discovery ----------------------------------------------------------
    def discover(self) -> List[Dict[str, object]]:
        """Find repositories under the registered paths, persist their portable
        records, and return them. Deduplicated by Git common directory."""
        ws_root = self.workspace_root()
        # Canonicalize the workspace root so it shares the same form Git returns
        # for the toplevel; otherwise a symlinked/short-name root leaks an
        # absolute path into the repository key (see _canon).
        ws_root_canon = _canon(ws_root) if ws_root else ""
        seen_common: Dict[str, str] = {}     # common-dir -> repository_key
        found: List[Dict[str, object]] = []
        candidates = self.registered_paths() or ([ws_root] if ws_root else [])
        for path in candidates:
            if not path or not os.path.isdir(path):
                continue
            toplevel = self._toplevel(path)
            if not toplevel:
                continue
            toplevel_canon = _canon(toplevel)
            common = self._common_dir(toplevel) or toplevel
            key = repository_key_for(ws_root_canon or toplevel_canon,
                                     toplevel_canon)
            if common in seen_common:
                continue
            seen_common[common] = key
            facts = self._classify(toplevel)
            rel = machine.relativize(toplevel_canon,
                                     ws_root_canon).strip("/") or "."
            record = upsert_repository(
                self.workspace_id, key, relative_root=rel,
                object_format=facts["object_format"],
                is_bare=facts["is_bare"],
                default_branch=facts["default_branch"],
                metadata={"detached_head": facts["detached_head"]})
            found.append(record)
        found.sort(key=lambda r: r["repository_key"])
        return found

    # -- classification -----------------------------------------------------
    def _toplevel(self, path: str) -> Optional[str]:
        """The repository root containing *path*, or None if not a repo. Works
        for worktrees where ``.git`` is a file (``--show-toplevel`` handles
        it)."""
        res = self.runner.run(path, ["rev-parse", "--show-toplevel"],
                              check=False)
        return _norm(res.text().strip()) if res.ok and res.text().strip() else None

    def _common_dir(self, repo_root: str) -> Optional[str]:
        """The resolved Git common directory — the identity used to dedup
        several worktrees/roots that share one repository."""
        res = self.runner.run(repo_root, ["rev-parse", "--git-common-dir"],
                              check=False)
        if not res.ok or not res.text().strip():
            return None
        common = res.text().strip()
        if not os.path.isabs(common):
            common = os.path.join(repo_root, common)
        try:
            return _norm(os.path.realpath(common))
        except OSError:
            return _norm(common)

    def _classify(self, repo_root: str) -> Dict[str, object]:
        object_format = "sha1"
        res = self.runner.run(repo_root, ["rev-parse", "--show-object-format"],
                              check=False)
        if res.ok and res.text().strip():
            object_format = res.text().strip()
        bare = self.runner.run(repo_root, ["rev-parse", "--is-bare-repository"],
                               check=False)
        is_bare = bare.ok and bare.text().strip() == "true"
        resolver = RefResolver(repo_root, self.runner)
        symbolic = resolver.symbolic_head()
        default_branch = ""
        detached = symbolic is None
        if symbolic:
            default_branch = symbolic.rsplit("/", 1)[-1]
        return {"object_format": object_format, "is_bare": is_bare,
                "default_branch": default_branch, "detached_head": detached}


# ---------------------------------------------------------------------------
# Absolute-root resolution (machine-local; never persisted)
# ---------------------------------------------------------------------------
def resolve_root(workspace_id: str, repository: Dict[str, object]) -> str:
    """Resolve a persisted repository record to its absolute root on THIS
    machine. Raises :class:`RepositoryUnavailable` when the root is not present
    (e.g. ``data/`` copied to a machine that has not re-pointed the source)."""
    ws_root = _norm(machine.project_root(workspace_id))
    rel = str(repository.get("relative_root") or ".")

    def _candidate(root: str) -> str:
        if not root:
            return ""
        return _norm(root if rel in (".", "") else machine.absolutize(rel, root))

    candidate = _candidate(ws_root)
    # Fall back to the canonical root form: the relative root was computed
    # against a canonicalized workspace root, so a symlinked/short-name root
    # only resolves after realpath (macOS /var -> /private/var, etc.).
    if not candidate or not os.path.isdir(candidate):
        candidate = _candidate(_canon(ws_root))
    if not candidate or not os.path.isdir(candidate):
        raise RepositoryUnavailable(
            f"git repository {repository.get('repository_key')!r} is not "
            f"available on this machine (re-point the workspace source root)",
            details={"repository_key": repository.get("repository_key"),
                     "relative_root": rel})
    return candidate


def resolve_root_by_key(workspace_id: str, repository_key: str) -> Tuple[Dict[str, object], str]:
    """Return ``(repository_record, absolute_root)`` for a key, or raise."""
    from .errors import RepositoryNotFound
    record = find_repository_by_key(workspace_id, repository_key)
    if not record:
        raise RepositoryNotFound(repository_key, workspace_id=workspace_id)
    return record, resolve_root(workspace_id, record)


__all__ = [
    "RepositoryDiscovery", "repository_key_for", "resolve_root",
    "resolve_root_by_key",
]
