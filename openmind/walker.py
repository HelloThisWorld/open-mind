"""Selection-aware filesystem walker (Invariant 3).

Honors:
  * the project's EXCLUDE set (unchecked paths) — excluded subtrees are PRUNED
    before descent, so they are never read or indexed,
  * .gitignore (pragmatic subset),
  * built-in ignores (target/build/node_modules/.git/...) and binaries.

Also provides per-file content hashing (incremental + checkpoint key,
Invariants 4 & 12) and stable service/repo attribution.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from . import config


def norm(p: str) -> str:
    return str(Path(p)).replace("\\", "/").rstrip("/")


# ---------------------------------------------------------------------------
# Selection (exclude-set)
# ---------------------------------------------------------------------------
class Selection:
    """Tests whether a path is excluded by the persisted exclude-set.

    Default = everything included. A path is excluded iff it equals, or is a
    descendant of, any entry in the exclude-set. The UI keeps the set
    consistent when re-including a node inside an excluded subtree (it removes
    the parent and re-adds the parent's other children).
    """

    def __init__(self, exclude: Optional[List[str]] = None) -> None:
        self.excludes: Set[str] = {norm(e) for e in (exclude or [])}

    def is_excluded(self, path: str) -> bool:
        p = norm(path)
        if p in self.excludes:
            return True
        for e in self.excludes:
            if p == e or p.startswith(e + "/"):
                return True
        return False


# ---------------------------------------------------------------------------
# .gitignore (pragmatic)
# ---------------------------------------------------------------------------
class GitIgnore:
    def __init__(self, root: str) -> None:
        self.root = norm(root)
        self.rules: List[Tuple[str, bool, bool]] = []  # (pattern, negate, dir_only)
        self._load(os.path.join(root, ".gitignore"))

    def _load(self, gitignore_path: str) -> None:
        try:
            with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except Exception:
            return
        for raw in lines:
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            line = line.strip()
            if line:
                self.rules.append((line, negate, dir_only))

    def ignored(self, abs_path: str, is_dir: bool) -> bool:
        rel = norm(abs_path)
        if rel.startswith(self.root + "/"):
            rel = rel[len(self.root) + 1:]
        else:
            return False
        base = rel.split("/")[-1]
        decision = False
        for pat, negate, dir_only in self.rules:
            if dir_only and not is_dir:
                continue
            anchored = pat.startswith("/")
            p = pat.lstrip("/")
            matched = False
            if anchored:
                matched = fnmatch.fnmatch(rel, p) or rel.startswith(p + "/")
            else:
                matched = (fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel, p)
                           or fnmatch.fnmatch(rel, "*/" + p)
                           or rel.startswith(p + "/") or ("/" + p + "/") in ("/" + rel + "/"))
            if matched:
                decision = not negate
        return decision


# ---------------------------------------------------------------------------
# Service / repo attribution
# ---------------------------------------------------------------------------
_REPO_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts",
                 "settings.gradle", "settings.gradle.kts")


def find_repo_root(file_path: str, ingest_root: str) -> str:
    fp = Path(norm(file_path))
    root = Path(norm(ingest_root))
    cur = fp.parent
    best = None
    while True:
        for marker in _REPO_MARKERS:
            if (cur / marker).exists():
                best = cur
                break
        if str(cur) == str(root) or cur.parent == cur:
            break
        cur = cur.parent
    if best is not None:
        return norm(str(best))
    # else: first-level directory under ingest root
    try:
        rel = fp.relative_to(root)
        parts = rel.parts
        if len(parts) >= 2:
            return norm(str(root / parts[0]))
    except Exception:
        pass
    return norm(str(root))


def service_name(repo_root: str) -> str:
    return Path(norm(repo_root)).name


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def hash_file(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------
def iter_files(root: str, exclude: Optional[List[str]] = None,
               respect_gitignore: bool = True) -> Iterator[str]:
    """Yield indexable file paths under *root*, honoring the exclude-set,
    gitignore, and built-in ignores. Excluded/ignored directories are pruned
    (never descended into)."""
    root = norm(root)
    sel = Selection(exclude)
    gi = GitIgnore(root) if respect_gitignore else None

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dn = norm(dirpath)
        # prune directories IN PLACE so excluded subtrees are never read
        kept = []
        for d in dirnames:
            full = norm(os.path.join(dirpath, d))
            if d in config.DEFAULT_IGNORE_DIRS:
                continue
            if sel.is_excluded(full):
                continue
            if gi and gi.ignored(full, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept

        for f in filenames:
            full = norm(os.path.join(dirpath, f))
            ext = os.path.splitext(f)[1].lower()
            if ext not in config.INDEX_EXTENSIONS:
                continue
            if ext in config.BINARY_EXTENSIONS:
                continue
            if sel.is_excluded(full):
                continue
            if gi and gi.ignored(full, is_dir=False):
                continue
            try:
                if os.path.getsize(full) > config.MAX_FILE_BYTES:
                    continue
            except Exception:
                continue
            yield full


def list_files(root: str, exclude: Optional[List[str]] = None) -> List[str]:
    return list(iter_files(root, exclude))


def read_text(path: str) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as fh:
                return fh.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""
