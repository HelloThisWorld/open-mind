"""Machine-local configuration that must NOT travel with the portable project
data (the zero-origin-traces constraint).

The single absolute fact about a project — WHERE its source lives on THIS
machine — is stored here, in a sidecar OUTSIDE :data:`config.DATA_DIR`, keyed by
project id. Everything Open Mind *learns* (RAG chunks + their embedded text, the
glossary, the structure map, the file index, cases) stores only paths RELATIVE
to that root. So a copied ``data/`` folder carries no local path at all: on a
new machine you re-point the root once (via the path picker) and every learned
artifact still opens — only "jump to the real file on disk" needs the root.

Two anchoring primitives bridge the two worlds:
  * :func:`to_rel`   absolute -> root-relative  (for storage + display)
  * :func:`from_rel` root-relative -> absolute  (only to actually read a file)

Both are tolerant of being handed an already-relative (or already-absolute)
path, so legacy artifacts written before this split keep working until a relearn
rewrites them.

This module imports only :mod:`config` and :mod:`walker` (never :mod:`db`) so it
can be used from anywhere in the persistence layer without an import cycle.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import walker

_lock = threading.RLock()

# The sidecar lives OUTSIDE config.DATA_DIR so copying the data folder never
# carries an absolute machine path. Overridable for tests / simulating another
# machine (point it at an empty dir to reproduce a fresh copy).
MACHINE_DIR = Path(os.environ.get("OPENMIND_MACHINE_DIR", str(Path.home() / ".openmind")))
PATHS_FILE = MACHINE_DIR / "paths.json"


def _load() -> Dict[str, Any]:
    try:
        return json.loads(PATHS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _store(data: Dict[str, Any]) -> None:
    MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PATHS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PATHS_FILE)


# ---------------------------------------------------------------------------
# Per-project path specs (the machine-local "separate config")
# ---------------------------------------------------------------------------
def get_paths(project_id: str) -> List[Dict[str, Any]]:
    with _lock:
        entry = _load().get(project_id) or {}
    return list(entry.get("paths") or [])


def set_paths(project_id: str, paths: List[Dict[str, Any]]) -> None:
    with _lock:
        data = _load()
        entry = dict(data.get(project_id) or {})
        if paths:
            entry["paths"] = list(paths)
            data[project_id] = entry
        else:
            # no paths left: keep the entry only if it still holds a github link
            # (so removing the local root doesn't silently drop the repo link),
            # otherwise drop it entirely.
            entry.pop("paths", None)
            if entry:
                data[project_id] = entry
            else:
                data.pop(project_id, None)
        _store(data)


def forget(project_id: str) -> None:
    """Drop a project's machine-local config ENTIRELY — paths and github link
    (called on project delete)."""
    with _lock:
        data = _load()
        if data.pop(project_id, None) is not None:
            _store(data)


# ---------------------------------------------------------------------------
# GitHub source link (machine-local on purpose)
# ---------------------------------------------------------------------------
# A copied data/ folder must carry no origin trace, so the repo a project came
# from is stored HERE, beside the local source root — not in data/. When the
# source isn't on this machine, "jump to source" fetches the file from GitHub
# (audited via netguard) using this link.
def get_github(project_id: str) -> Optional[Dict[str, Any]]:
    """The project's linked GitHub repo spec ({owner, repo, ref, url}) or None."""
    with _lock:
        entry = _load().get(project_id) or {}
    gh = entry.get("github")
    return dict(gh) if gh else None


def set_github(project_id: str, url: str, ref: Optional[str] = None) -> Dict[str, Any]:
    """Parse + persist a GitHub repo link for the project; returns the stored spec.
    Raises ValueError if the URL is not a recognizable GitHub repo reference."""
    spec = parse_github_url(url)
    if ref and ref.strip():
        spec["ref"] = ref.strip()
    with _lock:
        data = _load()
        entry = dict(data.get(project_id) or {})
        entry["github"] = spec
        data[project_id] = entry
        _store(data)
    return spec


def clear_github(project_id: str) -> None:
    """Remove a project's GitHub link, keeping any local paths intact."""
    with _lock:
        data = _load()
        entry = dict(data.get(project_id) or {})
        if entry.pop("github", None) is not None:
            if entry:
                data[project_id] = entry
            else:
                data.pop(project_id, None)
            _store(data)


def set_local_root(project_id: str, path: str) -> str:
    """Point the project's source root at a local folder (re-points project_root),
    preserving any existing github link. Returns the normalized root."""
    root = walker.norm(path)
    set_paths(project_id, [{"path": root, "exclude": []}])
    return root


def parse_github_url(url: str) -> Dict[str, Any]:
    """Normalize a GitHub repo reference to {owner, repo, ref, url}.

    Accepts the common forms a user might paste:
      https://github.com/owner/repo[.git]
      https://github.com/owner/repo/tree/<ref>[/...]
      https://github.com/owner/repo/blob/<ref>/<path>
      git@github.com:owner/repo.git
      owner/repo
    A ref carried by a /tree/ or /blob/ URL is captured; otherwise it defaults to
    HEAD (raw.githubusercontent.com resolves HEAD to the repo's default branch).
    Raises ValueError if no owner/repo can be extracted."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("empty GitHub URL")
    s = raw
    if s.startswith("git@"):                       # scp-style git remote
        s = s.split(":", 1)[1] if ":" in s else s
    else:
        s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", s)   # strip scheme://
        low = s.lower()
        for host in ("www.github.com/", "github.com/"):
            if low.startswith(host):
                s = s[len(host):]
                break
    parts = [p for p in s.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError("not a GitHub repo reference: %r" % raw)
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    ref = "HEAD"
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        ref = parts[3]
    if not owner or not repo:
        raise ValueError("not a GitHub repo reference: %r" % raw)
    return {"owner": owner, "repo": repo, "ref": ref,
            "url": "https://github.com/%s/%s" % (owner, repo)}


def github_raw_url(github: Dict[str, Any], rel_path: str) -> str:
    """Build the raw.githubusercontent.com URL for a project-relative source path."""
    ref = github.get("ref") or "HEAD"
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
        github["owner"], github["repo"], ref, rel)


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------
def project_root(project_id: str) -> str:
    """The absolute anchor all of a project's relative paths hang off of: the
    common directory prefix of its configured path specs (for the usual
    single-path project, simply that path). Returns '' when unset — e.g. data
    copied to a machine that has not pointed the root yet."""
    roots = [walker.norm(p["path"]) for p in get_paths(project_id) if p.get("path")]
    if not roots:
        return ""
    if len(roots) == 1:
        return roots[0]
    return _common_base(roots)


def _common_base(roots: List[str]) -> str:
    parts = [r.split("/") for r in roots]
    common: List[str] = []
    for seg in zip(*parts):
        if all(s == seg[0] for s in seg):
            common.append(seg[0])
        else:
            break
    return "/".join(common)


def _is_absolute(p: str) -> bool:
    # POSIX (/...), UNC (//host/share), or Windows drive (C:/...).
    return bool(p[:1] == "/" or (len(p) >= 2 and p[1] == ":"))


# -- pure helpers (root supplied by the caller; use in hot loops) -----------
def relativize(path: str, root: str) -> str:
    """Absolute -> root-relative. Idempotent: an already-relative path, or one
    not under *root*, is returned normalized but otherwise unchanged."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and (p == r or p.startswith(r + "/")):
        p = p[len(r):].lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return p


def absolutize(path: str, root: str) -> str:
    """Root-relative -> absolute, for reading the file off THIS machine. Returns
    '' for an empty path; returns the input unchanged when it is already
    absolute or *root* is unset (best-effort, never raises)."""
    p = (path or "").replace("\\", "/")
    if not p or _is_absolute(p):
        return p
    r = (root or "").replace("\\", "/").rstrip("/")
    return f"{r}/{p.lstrip('/')}" if r else p


# -- project-keyed convenience wrappers -------------------------------------
def to_rel(project_id: str, path: str) -> str:
    return relativize(path, project_root(project_id))


def from_rel(project_id: str, path: str) -> str:
    return absolutize(path, project_root(project_id))
