"""Ref validation and safe resolution (spec §6).

A ref that arrives from a CLI flag, a REST body or an MCP argument is
untrusted. Before it is ever handed to Git it is syntactically validated here,
and it is only ever resolved through the option-terminated form

    git rev-parse --verify --end-of-options <ref>^{commit}

so a value like ``--upload-pack=…`` can never become a Git option and a ref is
never interpolated into a shell (there is no shell — see
:mod:`openmind.git.command`). No resolution here contacts a remote; a ref that
does not resolve locally is a typed :class:`RefNotAvailableLocally`, never a
fetch.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .command import GitCommandRunner, default_runner
from .errors import (GitCommandFailed, InvalidRef, MergeBaseUnavailable,
                     RefNotAvailableLocally)

#: A resolved object name (SHA) — 40 hex (SHA-1) or 64 hex (SHA-256).
_HEX = set("0123456789abcdef")


def validate_ref(ref: str) -> str:
    """Return *ref* stripped, or raise :class:`InvalidRef`.

    Rejects: empty; leading ``-`` (would parse as an option); NUL; any ASCII
    control character; a lone ``@`` sequence Git treats specially is left to
    Git's own ``--verify`` to reject as ambiguous rather than guessed here."""
    raw = ref if isinstance(ref, str) else str(ref or "")
    stripped = raw.strip()
    if not stripped:
        raise InvalidRef(raw, "empty ref")
    if stripped.startswith("-"):
        raise InvalidRef(raw, "ref may not begin with '-'")
    if "\x00" in stripped:
        raise InvalidRef(raw, "ref contains a NUL byte")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in stripped):
        raise InvalidRef(raw, "ref contains a control character")
    if stripped.count(" ") and _looks_multitoken(stripped):
        # A space is legal inside some ref expressions Git accepts, but a bare
        # multi-token value is almost always an injection attempt or a mistake.
        raise InvalidRef(raw, "ref contains whitespace")
    return stripped


def _looks_multitoken(value: str) -> bool:
    # Allow the ``^{...}`` / ``@{...}`` peels but reject "a b" style values.
    return " " in value.strip()


def is_object_name(value: str) -> bool:
    """True if *value* is a full hex object name (SHA-1 or SHA-256)."""
    v = (value or "").strip().lower()
    return len(v) in (40, 64) and all(c in _HEX for c in v)


class RefResolver:
    """Resolves refs to commit object names in one repository, safely."""

    def __init__(self, repo_root: str, runner: Optional[GitCommandRunner] = None,
                 *, repository_key: str = "") -> None:
        self.repo_root = str(repo_root)
        self.runner = runner or default_runner()
        self.repository_key = repository_key

    # -- resolution ---------------------------------------------------------
    def resolve_commit(self, ref: str) -> str:
        """Resolve *ref* to a commit object name, or raise. Never fetches."""
        clean = validate_ref(ref)
        spec = clean if clean.endswith("^{commit}") else f"{clean}^{{commit}}"
        try:
            res = self.runner.run(
                self.repo_root,
                ["rev-parse", "--verify", "--quiet", "--end-of-options", spec])
        except GitCommandFailed as exc:
            raise RefNotAvailableLocally(
                clean, repository=self.repository_key) from exc
        out = res.text().strip()
        if not out:
            # --quiet makes an unknown ref exit non-zero with empty stdout; some
            # git versions exit 0 with empty stdout — treat both as not-found.
            raise RefNotAvailableLocally(clean, repository=self.repository_key)
        return out.splitlines()[0].strip()

    def try_resolve_commit(self, ref: str) -> Optional[str]:
        try:
            return self.resolve_commit(ref)
        except (RefNotAvailableLocally, InvalidRef):
            return None

    def rev_parse(self, spec: str) -> str:
        """Resolve an arbitrary (validated) rev-spec to a single object name.
        Used for tree SHAs (``<commit>^{tree}``) and HEAD."""
        clean = validate_ref(spec)
        res = self.runner.run(
            self.repo_root,
            ["rev-parse", "--verify", "--end-of-options", clean])
        return res.text().strip().splitlines()[0].strip()

    # -- merge-base (spec §7) ----------------------------------------------
    def merge_base(self, base: str, head: str) -> str:
        """The best common ancestor of two commits. Raises
        :class:`MergeBaseUnavailable` when none exists locally (typically a
        shallow clone) — never fetches to complete the history."""
        base_c = self.resolve_commit(base)
        head_c = self.resolve_commit(head)
        try:
            res = self.runner.run(
                self.repo_root,
                ["merge-base", "--end-of-options", base_c, head_c])
        except GitCommandFailed as exc:
            raise MergeBaseUnavailable(
                base, head, repository=self.repository_key,
                possibly_shallow=self.is_shallow()) from exc
        out = res.text().strip()
        if not out:
            raise MergeBaseUnavailable(
                base, head, repository=self.repository_key,
                possibly_shallow=self.is_shallow())
        return out.splitlines()[0].strip()

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        """True if *maybe_ancestor* is an ancestor of *descendant*. Used by
        post-merge reconciliation (spec §35)."""
        anc = self.resolve_commit(maybe_ancestor)
        desc = self.resolve_commit(descendant)
        res = self.runner.run(
            self.repo_root,
            ["merge-base", "--is-ancestor", "--end-of-options", anc, desc],
            check=False)
        # exit 0 => ancestor; exit 1 => not; anything else is an error.
        if res.returncode not in (0, 1):
            raise GitCommandFailed(
                "git merge-base --is-ancestor failed",
                returncode=res.returncode, stderr=res.stderr, args=res.args)
        return res.returncode == 0

    # -- repository facts ---------------------------------------------------
    def is_shallow(self) -> bool:
        res = self.runner.run(self.repo_root,
                              ["rev-parse", "--is-shallow-repository"],
                              check=False)
        return res.ok and res.text().strip() == "true"

    def head_commit(self) -> Optional[str]:
        return self.try_resolve_commit("HEAD")

    def symbolic_head(self) -> Optional[str]:
        """The branch HEAD points at (``refs/heads/main``), or None when HEAD
        is detached."""
        res = self.runner.run(self.repo_root,
                              ["symbolic-ref", "--quiet", "HEAD"], check=False)
        return res.text().strip() or None if res.ok else None

    def tree_of(self, commit: str) -> str:
        """The tree object name of a commit."""
        return self.rev_parse(f"{self.resolve_commit(commit)}^{{tree}}")


def list_branches(repo_root: str, runner: Optional[GitCommandRunner] = None,
                  *, limit: int = 1000) -> List[dict]:
    """Local branches as ``[{name, commit, is_head}]`` in name order, bounded.

    Uses ``for-each-ref`` with an explicit NUL-delimited format — never the
    localized ``git branch`` output."""
    runner = runner or default_runner()
    res = runner.run(
        repo_root,
        ["for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(HEAD)",
         "refs/heads/"])
    out: List[dict] = []
    for line in res.text().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00")
        if len(parts) < 2:
            continue
        out.append({"name": parts[0], "commit": parts[1],
                    "is_head": (len(parts) > 2 and parts[2].strip() == "*")})
        if len(out) >= limit:
            break
    out.sort(key=lambda b: b["name"])
    return out


__all__ = [
    "validate_ref", "is_object_name", "RefResolver", "list_branches",
]
