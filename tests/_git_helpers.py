"""Shared helpers for the Phase 7 Git/overlay acceptance suites.

Builds throwaway Git repositories in a temp dir (no network) and a tiny
canonical graph so overlay impact can be exercised. Every function is
deterministic and offline.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

_PASS = 0
_FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok  {label}")
    else:
        _FAIL += 1
        print(f"  BAD {label}" + (f" :: {detail}" if detail else ""))
    return bool(cond)


def finish(name: str) -> int:
    print(f"{name}: {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


# ---------------------------------------------------------------------------
# Git fixtures
# ---------------------------------------------------------------------------
def _git(repo: str, args: List[str], *, check_rc: bool = True) -> str:
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME="fx", GIT_AUTHOR_EMAIL="fx@example.test",
               GIT_COMMITTER_NAME="fx", GIT_COMMITTER_EMAIL="fx@example.test",
               GIT_CONFIG_NOSYSTEM="1")
    proc = subprocess.run(["git"] + args, cwd=repo, env=env,
                          capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def new_repo(prefix: str = "om_git_") -> str:
    repo = tempfile.mkdtemp(prefix=prefix)
    _git(repo, ["init", "-q"])
    _git(repo, ["config", "core.autocrlf", "false"])
    _git(repo, ["config", "commit.gpgsign", "false"])
    return repo


def write(repo: str, rel: str, content: str) -> None:
    path = os.path.join(repo, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def commit(repo: str, message: str) -> str:
    _git(repo, ["add", "-A"])
    _git(repo, ["commit", "-qm", message])
    return _git(repo, ["rev-parse", "HEAD"]).strip()


def head_branch(repo: str) -> str:
    return _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()


def checkout_new_branch(repo: str, name: str) -> None:
    _git(repo, ["checkout", "-q", "-b", name])


def checkout(repo: str, name: str) -> None:
    _git(repo, ["checkout", "-q", name])


NAMECHECK_MAIN_JAVA = (
    "package com.acme.namecheck;\n"
    "public class NameCheckService {\n"
    "    public Result execute(Request req) {\n"
    "        int timeout = 3000;\n"
    "        return check(req, timeout);\n"
    "    }\n"
    "    private Result check(Request r, int t) { return new Result(); }\n"
    "}\n")


def build_namecheck_repo() -> Dict[str, str]:
    """A NameCheck service on ``main`` with a ``feature/namecheck`` branch that
    changes the method body and the timeout config. Returns key commits/branch
    names."""
    repo = new_repo()
    write(repo, "src/NameCheckService.java", NAMECHECK_MAIN_JAVA)
    write(repo, "application.properties",
          "namecheck.timeout=3000\nnamecheck.retries=2\n")
    write(repo, "REQ-NC-017.md",
          "# REQ-NC-017\n\nThe NameCheck timeout must be 3000 ms.\n")
    base = commit(repo, "main baseline")
    main_branch = head_branch(repo)
    checkout_new_branch(repo, "feature/namecheck")
    feat_java = NAMECHECK_MAIN_JAVA.replace(
        "int timeout = 3000;",
        "int timeout = 5000;\n        audit(req);").replace(
        "    private Result check",
        "    private void audit(Request r) {}\n    private Result check")
    write(repo, "src/NameCheckService.java", feat_java)
    write(repo, "application.properties",
          "namecheck.timeout=5000\nnamecheck.retries=2\n")
    head = commit(repo, "feature: timeout 5000 + audit")
    checkout(repo, main_branch)
    return {"repo": repo, "main": main_branch, "feature": "feature/namecheck",
            "base_commit": base, "head_commit": head}


def make_workspace(runtime, repo: str, name: str = "git-fx") -> str:
    ws = runtime.workspaces.create(name, path=repo.replace("\\", "/"))
    return ws["id"]


def ingest_and_sync(runtime, pid: str, timeout: float = 180) -> None:
    runtime.ingest.start(pid, wait=True, timeout=timeout)
    runtime.knowledge.sync(pid, actor="fx")


def canonical_counts(conn, lock) -> Dict[str, int]:
    tables = ("assets", "asset_revisions", "segments", "evidence",
              "engineering_entities", "engineering_claims",
              "engineering_relations", "trace_paths", "traceability_gaps",
              "engineering_conflicts", "knowledge_revisions")
    out: Dict[str, int] = {}
    with lock:
        for t in tables:
            try:
                out[t] = conn.execute(
                    f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            except Exception:
                out[t] = -1
    return out
