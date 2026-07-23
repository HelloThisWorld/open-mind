"""The one and only Git subprocess boundary (spec §5, §8).

Every Git invocation in OpenMind goes through :class:`GitCommandRunner.run`.
There is no other ``subprocess`` call that spawns ``git`` anywhere in the
codebase, and this class exposes no way to run an arbitrary subcommand: the
first positional argument is checked against an explicit read-only allow-list
BEFORE a process is spawned, and forbidden subcommands raise
:class:`GitCommandDenied` without ever reaching Git.

Guarantees enforced here (asserted by ``tests/verify_git_security.py``):

* ``subprocess.run(..., shell=False)`` with an argument LIST — never a shell
  string, so a hostile ref/path can never be word-split or expanded;
* an explicit ``cwd`` (the repository root);
* a bounded timeout (``config.GIT_COMMAND_TIMEOUT``) — a hung Git is killed;
* bounded captured output (``config.GIT_MAX_COMMAND_OUTPUT_BYTES``);
* a controlled, minimal environment that disables prompts, pagers, external
  diff/textconv drivers, credential/askpass helpers and system/global config,
  and pins ``LC_ALL=C`` so output is never localized;
* only the allow-listed read-only subcommands; no fetch/pull/push/checkout/
  reset/clean/merge/rebase/commit/config/remote/... ever runs;
* no remote contact (the allowed commands are all local-only, and network
  transports would need a forbidden subcommand anyway).

This module is deliberately free of any OpenMind persistence import so it can
be unit-tested in isolation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .. import config
from .errors import (GitCommandDenied, GitCommandFailed, GitCommandTimeout,
                     GitOutputTooLarge, GitRepositoryUnsafe, GitUnavailable)

# ---------------------------------------------------------------------------
# The allow-list (spec §5). Read-only families only. Membership here is the
# security boundary — adding a name is a deliberate, reviewed act.
# ---------------------------------------------------------------------------
ALLOWED_SUBCOMMANDS = frozenset({
    "rev-parse",
    "rev-list",
    "merge-base",
    "show",
    "cat-file",
    "diff",
    "diff-tree",
    "status",
    "ls-files",
    "ls-tree",
    "check-ignore",
    "check-attr",
    "for-each-ref",
    "symbolic-ref",
    "version",           # `git version` — capability probe, no repo needed
})

#: Explicitly named so the denial message and the security test can be precise.
#: This is not exhaustive (the allow-list is the real gate — anything not
#: allowed is denied) but it documents intent and lets tests assert each one is
#: refused.
FORBIDDEN_SUBCOMMANDS = frozenset({
    "checkout", "switch", "reset", "restore", "clean", "merge", "rebase",
    "cherry-pick", "revert", "apply", "am", "commit", "tag", "branch",
    "fetch", "pull", "push", "clone", "gc", "repack", "prune", "config",
    "remote", "stash", "update-ref", "update-index", "write-tree",
    "commit-tree", "hash-object", "mv", "rm", "notes", "worktree", "submodule",
    "filter-branch", "reflog", "bisect", "daemon", "credential",
})

#: Global options that must not appear as the "subcommand" slot — e.g.
#: ``git -c core.pager=... <cmd>`` or ``git --exec-path=...``. We forbid any
#: token that is not a bare allowed subcommand in slot 0, so a caller can never
#: inject a ``-c`` override or ``--upload-pack`` style option.
def _is_option_like(token: str) -> bool:
    return token.startswith("-")


@dataclass
class GitCommandResult:
    """The captured outcome of one Git command. Stdout is bytes because much of
    what we read (blobs, NUL-delimited diff records) is not text."""
    args: List[str]
    returncode: int
    stdout: bytes
    stderr: str
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.stdout.decode(encoding, errors)


@dataclass
class GitCommandRunner:
    """A read-only Git command executor bound to one machine's ``git``.

    Stateless except for the resolved git path and a couple of bounds, so a
    single shared instance is safe across threads (``subprocess.run`` is)."""

    git_path: Optional[str] = None
    default_timeout: float = field(default_factory=lambda: config.GIT_COMMAND_TIMEOUT)
    max_output_bytes: int = field(default_factory=lambda: config.GIT_MAX_COMMAND_OUTPUT_BYTES)

    def __post_init__(self) -> None:
        if not self.git_path:
            self.git_path = shutil.which("git")

    # -- capability ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.git_path)

    def _require_git(self) -> str:
        if not self.git_path:
            raise GitUnavailable(
                "the 'git' executable was not found on PATH; Phase 7 Git "
                "features require a local git install")
        return self.git_path

    # -- the environment (spec §5) -----------------------------------------
    @staticmethod
    def _env() -> Dict[str, str]:
        """A minimal, controlled environment. Start from the current env (so
        HOME/SystemRoot resolve) but override every knob that could cause a
        prompt, a pager, an external driver, credential I/O or localized
        output, and force system/global config off so a machine-level
        ``safe.directory`` or alias can't change behavior."""
        env = dict(os.environ)
        env.update({
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ASKPASS": "",           # never pop an askpass helper
            "SSH_ASKPASS": "",
            "GIT_ALLOW_PROTOCOL": "",    # no transport is allowed to run
            "LC_ALL": "C",
            "LANG": "C",
        })
        # Remove any inherited external diff / textconv driver rather than set
        # it empty: a *set-but-empty* GIT_EXTERNAL_DIFF makes git try to spawn
        # the empty string and abort ("cannot spawn"). Unsetting it, plus the
        # explicit --no-ext-diff/--no-textconv flags on every diff, is the
        # correct control. Likewise drop anything that could redirect config or
        # the object/worktree location to a foreign path.
        for var in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS",
                    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(var, None)
        return env

    # -- validation ---------------------------------------------------------
    @classmethod
    def _validate(cls, args: Sequence[str]) -> List[str]:
        clean = [str(a) for a in args]
        if not clean:
            raise GitCommandDenied("no git subcommand supplied")
        sub = clean[0]
        if _is_option_like(sub):
            raise GitCommandDenied(
                f"the first git argument must be a bare subcommand, not the "
                f"option {sub!r} (no -c / --exec-path injection)",
                subcommand=sub)
        if sub in FORBIDDEN_SUBCOMMANDS:
            raise GitCommandDenied(
                f"git subcommand {sub!r} is forbidden: OpenMind is a Git "
                f"reader and never mutates a repository or contacts a remote",
                subcommand=sub)
        if sub not in ALLOWED_SUBCOMMANDS:
            raise GitCommandDenied(
                f"git subcommand {sub!r} is not on the read-only allow-list",
                subcommand=sub, details={"allowed": sorted(ALLOWED_SUBCOMMANDS)})
        # A NUL anywhere in an argument is always a bug/attack — reject it.
        for tok in clean:
            if "\x00" in tok:
                raise GitCommandDenied(
                    "git argument contains a NUL byte", subcommand=sub)
        return clean

    # -- execution ----------------------------------------------------------
    def run(self, repo_root: str, args: Sequence[str], *,
            input_bytes: Optional[bytes] = None,
            timeout: Optional[float] = None,
            check: bool = True) -> GitCommandResult:
        """Execute one allow-listed, read-only Git command in *repo_root*.

        ``args`` is the argument list AFTER ``git`` (``["diff", "--raw", …]``).
        Returns a :class:`GitCommandResult`. With ``check=True`` (default) a
        non-zero exit raises :class:`GitCommandFailed`, except that Git's
        unsafe-repository refusal is translated to :class:`GitRepositoryUnsafe`
        so the caller can tell the user to fix ownership (spec §5).
        """
        git = self._require_git()
        clean = self._validate(args)
        argv = [git, *clean]
        env = self._env()
        to = float(timeout if timeout is not None else self.default_timeout)
        import time as _time
        started = _time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(repo_root),
                input=input_bytes,
                capture_output=True,
                shell=False,            # NEVER a shell (spec §5)
                env=env,
                timeout=to,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandTimeout(
                f"git {clean[0]} timed out after {to:g}s",
                details={"args": clean, "timeout_seconds": to}) from exc
        except FileNotFoundError as exc:      # git vanished between init & run
            raise GitUnavailable("the 'git' executable disappeared") from exc
        duration = _time.monotonic() - started

        stdout = proc.stdout or b""
        if len(stdout) > self.max_output_bytes:
            raise GitOutputTooLarge(
                f"git {clean[0]} produced {len(stdout)} bytes, exceeding the "
                f"{self.max_output_bytes}-byte bound",
                details={"args": clean, "bytes": len(stdout),
                         "limit": self.max_output_bytes})
        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        result = GitCommandResult(args=clean, returncode=proc.returncode,
                                  stdout=stdout, stderr=stderr,
                                  duration=duration)
        if check and proc.returncode != 0:
            low = stderr.lower()
            if "dubious ownership" in low or "unsafe repository" in low or \
               "detected dubious" in low:
                raise GitRepositoryUnsafe(
                    "git refused the repository as unsafe (dubious ownership); "
                    "OpenMind does not add safe.directory automatically — fix "
                    "the repository ownership explicitly",
                    details={"args": clean, "stderr": stderr[:2000],
                             "repo_root": str(repo_root)})
            raise GitCommandFailed(
                f"git {clean[0]} failed (exit {proc.returncode})",
                returncode=proc.returncode, stderr=stderr, args=clean)
        return result


#: A process-wide default runner. Cheap to construct; shared for convenience.
_DEFAULT: Optional[GitCommandRunner] = None


def default_runner() -> GitCommandRunner:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = GitCommandRunner()
    return _DEFAULT


__all__ = [
    "GitCommandRunner", "GitCommandResult", "default_runner",
    "ALLOWED_SUBCOMMANDS", "FORBIDDEN_SUBCOMMANDS",
]
