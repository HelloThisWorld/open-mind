"""Server-side filesystem browsing for the two pickers:
  * project tree picker (folders + files, lazy one level at a time)
  * model file picker (folders + .gguf files)

Browsers can't expose real local paths, so the backend lists directory
contents. Read-only: this module never writes to the target filesystem.
"""
from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import config


def _norm(path: str) -> str:
    return str(Path(path)).replace("\\", "/")


def list_drives() -> List[Dict[str, Any]]:
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append({"name": f"{letter}:", "path": _norm(root),
                           "type": "dir", "has_children": True})
    return drives


def _has_children(p: Path) -> bool:
    try:
        with os.scandir(p) as it:
            for _ in it:
                return True
    except Exception:
        return True  # optimistic; expansion will reveal truth
    return False


def list_dir(path: Optional[str], file_ext: Optional[Set[str]] = None,
             dirs_only: bool = False) -> Dict[str, Any]:
    """List immediate children of *path* (one level).

    file_ext: when set, only files with these extensions are returned.
    """
    if not path or path in ("", "/", "\\", "drives", "roots"):
        return {"path": "", "parent": None, "is_root": True,
                "entries": list_drives()}

    p = Path(path)
    if not p.exists():
        return {"path": _norm(path), "parent": None, "error": "not found", "entries": []}
    if p.is_file():
        p = p.parent

    parent = None if p.parent == p else _norm(p.parent)
    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(p) as it:
            items = list(it)
    except PermissionError:
        return {"path": _norm(p), "parent": parent, "error": "permission denied",
                "entries": []}
    except Exception as exc:
        return {"path": _norm(p), "parent": parent, "error": str(exc), "entries": []}

    dirs, files = [], []
    for entry in items:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except Exception:
            is_dir = False
        name = entry.name
        full = _norm(entry.path)
        if is_dir:
            dirs.append({
                "name": name, "path": full, "type": "dir",
                "has_children": _has_children(Path(entry.path)),
                "default_ignored": name in config.DEFAULT_IGNORE_DIRS,
            })
        else:
            if dirs_only:
                continue
            ext = os.path.splitext(name)[1].lower()
            if file_ext is not None and ext not in file_ext:
                continue
            size = 0
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except Exception:
                pass
            files.append({
                "name": name, "path": full, "type": "file",
                "ext": ext, "size": size,
                "indexable": ext in config.INDEX_EXTENSIONS,
            })
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    entries = dirs + files
    return {"path": _norm(p), "parent": parent, "is_root": False, "entries": entries}
