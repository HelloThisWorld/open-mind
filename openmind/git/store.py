"""Repository over the v0008 ``git_repositories`` and
``workspace_git_baselines`` tables.

Runs on the SAME shared WAL connection and RLock as every other store
(``db.shared_connection()``). Persists only PORTABLE data — a repository is
identified by its workspace-relative ``repository_key``; its absolute root is
resolved elsewhere (machine-local config), never stored here.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .. import db


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _cx():
    return db.shared_connection()


def _j(value: Any) -> str:
    return json.dumps(value if value is not None else {})


def _load(text: Any, default: Any) -> Any:
    try:
        out = json.loads(text or "")
    except Exception:
        return default
    return out if out is not None else default


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------
def _repo_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "repository_key": row["repository_key"],
        "relative_root": row["relative_root"],
        "object_format": row["object_format"],
        "is_bare": bool(row["is_bare"]),
        "default_branch": row["default_branch"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _baseline_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "repository_id": row["repository_id"],
        "commit_sha": row["commit_sha"],
        "tree_sha": row["tree_sha"],
        "branch_name": row["branch_name"],
        "head_ref": row["head_ref"],
        "knowledge_revision": row["knowledge_revision"],
        "traceability_run_id": row["traceability_run_id"],
        "trace_policy_checksum": row["trace_policy_checksum"],
        "graph_projector_version": row["graph_projector_version"],
        "trace_engine_version": row["trace_engine_version"],
        "asset_state_hash": row["asset_state_hash"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
def upsert_repository(workspace_id: str, repository_key: str, *,
                      relative_root: str = "", object_format: str = "sha1",
                      is_bare: bool = False, default_branch: str = "",
                      metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert or update a repository by ``(workspace_id, repository_key)``.
    Idempotent — re-discovering the same repository updates its facts."""
    conn, lock = _cx()
    ts = _now()
    with lock:
        existing = conn.execute(
            "SELECT id FROM git_repositories WHERE workspace_id=? AND "
            "repository_key=?", (workspace_id, repository_key)).fetchone()
        if existing:
            conn.execute(
                "UPDATE git_repositories SET relative_root=?, object_format=?, "
                "is_bare=?, default_branch=?, metadata_json=?, updated_at=? "
                "WHERE id=?",
                (relative_root, object_format, 1 if is_bare else 0,
                 default_branch, _j(metadata or {}), ts, existing["id"]))
            rid = existing["id"]
        else:
            rid = db.new_id("gr_")
            conn.execute(
                "INSERT INTO git_repositories(id, workspace_id, repository_key, "
                "relative_root, object_format, is_bare, default_branch, "
                "metadata_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, workspace_id, repository_key, relative_root,
                 object_format, 1 if is_bare else 0, default_branch,
                 _j(metadata or {}), ts, ts))
        conn.commit()
        row = conn.execute("SELECT * FROM git_repositories WHERE id=?",
                           (rid,)).fetchone()
    return _repo_row(row)


def get_repository(workspace_id: str, repository_id: str
                   ) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM git_repositories WHERE id=? AND workspace_id=?",
            (repository_id, workspace_id)).fetchone()
    return _repo_row(row) if row else None


def find_repository_by_key(workspace_id: str, repository_key: str
                           ) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM git_repositories WHERE workspace_id=? AND "
            "repository_key=?", (workspace_id, repository_key)).fetchone()
    return _repo_row(row) if row else None


def list_repositories(workspace_id: str, limit: int = 500
                      ) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM git_repositories WHERE workspace_id=? "
            "ORDER BY repository_key LIMIT ?",
            (workspace_id, int(limit))).fetchall()
    return [_repo_row(r) for r in rows]


def count_repositories(workspace_id: str) -> int:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT COUNT(*) c FROM git_repositories WHERE workspace_id=?",
            (workspace_id,)).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
def insert_baseline(workspace_id: str, repository_id: str, *,
                    commit_sha: str, tree_sha: str = "", branch_name: str = "",
                    head_ref: str = "", knowledge_revision: int = 0,
                    traceability_run_id: str = "",
                    trace_policy_checksum: str = "",
                    graph_projector_version: str = "",
                    trace_engine_version: str = "",
                    asset_state_hash: str = "",
                    metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    conn, lock = _cx()
    ts = _now()
    bid = db.new_id("gb_")
    with lock:
        conn.execute(
            "INSERT INTO workspace_git_baselines(id, workspace_id, "
            "repository_id, commit_sha, tree_sha, branch_name, head_ref, "
            "knowledge_revision, traceability_run_id, trace_policy_checksum, "
            "graph_projector_version, trace_engine_version, asset_state_hash, "
            "metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, workspace_id, repository_id, commit_sha, tree_sha,
             branch_name, head_ref, int(knowledge_revision),
             traceability_run_id, trace_policy_checksum,
             graph_projector_version, trace_engine_version, asset_state_hash,
             _j(metadata or {}), ts))
        conn.commit()
        row = conn.execute("SELECT * FROM workspace_git_baselines WHERE id=?",
                           (bid,)).fetchone()
    return _baseline_row(row)


def get_baseline(workspace_id: str, baseline_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM workspace_git_baselines WHERE id=? AND workspace_id=?",
            (baseline_id, workspace_id)).fetchone()
    return _baseline_row(row) if row else None


def latest_baseline(workspace_id: str, repository_id: str
                    ) -> Optional[Dict[str, Any]]:
    """The most recent baseline for a repository, or None."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM workspace_git_baselines WHERE workspace_id=? AND "
            "repository_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (workspace_id, repository_id)).fetchone()
    return _baseline_row(row) if row else None


def list_baselines(workspace_id: str, repository_id: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if repository_id:
            rows = conn.execute(
                "SELECT * FROM workspace_git_baselines WHERE workspace_id=? AND "
                "repository_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (workspace_id, repository_id, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workspace_git_baselines WHERE workspace_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (workspace_id, int(limit))).fetchall()
    return [_baseline_row(r) for r in rows]


def find_baseline_by_commit(workspace_id: str, repository_id: str,
                            commit_sha: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM workspace_git_baselines WHERE workspace_id=? AND "
            "repository_id=? AND commit_sha=? ORDER BY created_at DESC LIMIT 1",
            (workspace_id, repository_id, commit_sha)).fetchone()
    return _baseline_row(row) if row else None


__all__ = [
    "upsert_repository", "get_repository", "find_repository_by_key",
    "list_repositories", "count_repositories",
    "insert_baseline", "get_baseline", "latest_baseline", "list_baselines",
    "find_baseline_by_commit",
]
