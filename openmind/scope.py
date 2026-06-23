"""Scope resolution. A scope is a project id.

Resolving a scope yields the list of project ids it covers — for a project that
is just ``[that id]``. Every read (search, glossary, cases, Ask) resolves the
scope to project ids and works per project. The map readers accept a list, so a
multi-project scope could be reintroduced later without changing call sites.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import db


def resolve(scope_id: Optional[str]) -> List[str]:
    """Return the project ids covered by *scope_id*.

    - a project id  -> [that id]
    - None/blank    -> [] (caller decides default)
    """
    if not scope_id:
        return []
    if db.get_project(scope_id):
        return [scope_id]
    return []


def describe(scope_id: Optional[str]) -> Dict[str, object]:
    if not scope_id:
        return {"scope": None, "kind": "none", "project_ids": []}
    p = db.get_project(scope_id)
    if p:
        return {"scope": scope_id, "kind": "project", "name": p["name"],
                "project_ids": [scope_id]}
    return {"scope": scope_id, "kind": "unknown", "project_ids": []}


def require(scope_id: Optional[str]) -> Tuple[List[str], Dict[str, object]]:
    pids = resolve(scope_id)
    return pids, describe(scope_id)
