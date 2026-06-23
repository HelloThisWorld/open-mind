"""Generated documentation — intentionally a NO-OP in this build.

Document generation previously projected the architecture artifacts
(topology / services / schemas / db-schema / workflows) into human-readable
markdown pages. Those artifacts have been removed in favour of the deterministic
GLOSSARY (:mod:`openmind.glossary`), and producing human-readable description
documents is explicitly OUT OF SCOPE for the glossary build.

So generation is a no-op: ``generate`` writes nothing and clears any pages left
over from an earlier build, and the Documentation tab is an honest empty shell
(it shows "no generated documentation"). The endpoints and the gendocs job are
kept so the app surface is stable; they simply have nothing to produce.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config


def _docs_dir(pid: str) -> Path:
    d = config.project_docs_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate(project_id: str, progress=None) -> Dict[str, Any]:
    """No-op doc generation. Clears any previously generated pages so the tab
    honestly reflects that no documentation is generated in this build."""
    d = _docs_dir(project_id)
    for f in d.glob("*.md"):
        try:
            f.unlink()
        except Exception:
            pass
    if progress:
        progress(0, 0, "")
    return {"pages": [], "count": 0}


def list_docs(project_ids: List[str]) -> List[Dict[str, Any]]:
    """No generated docs in this build (the glossary is the authoritative index)."""
    return []


def get_doc(project_ids: List[str], page: str) -> Optional[Dict[str, Any]]:
    return None
