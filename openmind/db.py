"""SQLite persistence: registry (projects), jobs, model-config,
per-file index (incremental hashes + pause checkpoint), and a kv store.

Thread-safe: a single connection (WAL mode) guarded by a lock, since the
background job worker, SSE streams, and request handlers all touch the DB
concurrently.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config, machine, migrations

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_migration_result: Optional["migrations.MigrationResult"] = None


def init_db() -> None:
    """Open the shared connection and bring the schema up to date.

    The schema itself lives in :mod:`openmind.migrations.versions` — this is
    now a migration run, not a pile of ``CREATE TABLE IF NOT EXISTS``. It stays
    idempotent and safe to call from anywhere (the runtime bootstrap, the
    FastAPI lifespan, a lazy ``_c()``), and it never destroys data: a legacy
    database that predates the ledger is baselined, not recreated.
    """
    global _conn, _migration_result
    with _lock:
        if _conn is None:
            _conn = _connect()
        # The runner takes the same lock; RLock makes the re-entry safe.
        _migration_result = migrations.migrate(_conn, _lock)


def migration_status() -> Dict[str, Any]:
    """What the last :func:`init_db` migration run did, plus the live schema
    version read back from the database. Reported by ``doctor`` and
    ``GET /api/health``."""
    with _lock:
        version = migrations.current_version(_conn) if _conn is not None else 0
    base = _migration_result.as_dict() if _migration_result else {
        "version": version, "applied": [], "already_applied": [],
        "baselined_legacy": False, "unknown_applied": [],
    }
    base["version"] = version
    return base


def _c() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def _project_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "state": row["state"],
        # paths are machine-local (sidecar), never the portable DB column
        "paths": machine.get_paths(row["id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "meta": json.loads(row["meta_json"]),
    }


def create_project(name: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    pid = project_id or new_id("p_")
    ts = now()
    with _lock:
        _c().execute(
            "INSERT INTO projects (id,name,state,paths_json,created_at,updated_at,meta_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, name, "init", "[]", ts, ts, "{}"),
        )
        _c().commit()
    config.ensure_project_dirs(pid)
    return get_project(pid)  # type: ignore[return-value]


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    # a project being deleted is treated as GONE at once (its storage is reclaimed in
    # the background); the row is dropped for real when cleanup finishes.
    if not row or row["state"] == "deleting":
        return None
    return _project_row(row)


def list_projects(state: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        if state:
            rows = _c().execute(
                "SELECT * FROM projects WHERE state=? ORDER BY created_at", (state,)
            ).fetchall()
        else:
            # hide projects being deleted, so a UI refresh never shows them again
            # while the background cleanup runs.
            rows = _c().execute(
                "SELECT * FROM projects WHERE state!='deleting' ORDER BY created_at"
            ).fetchall()
    return [_project_row(r) for r in rows]


def delete_project(project_id: str) -> None:
    """Remove the project entity + its jobs + file index rows + Ask history +
    Asset model rows. (Callers also drop the vector collections and the data dir —
    see jobs.delete_project.)"""
    with _lock:
        # Assets cascade to revisions/segments/evidence via the v0003 FK
        # (ON DELETE CASCADE, foreign_keys=ON). This explicit DELETE runs first
        # so the cleanup is correct even if a future caller has the pragma off,
        # and so the intent is visible beside the other per-project deletes.
        _c().execute("DELETE FROM assets WHERE workspace_id=?", (project_id,))
        _c().execute("DELETE FROM projects WHERE id=?", (project_id,))
        _c().execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        _c().execute("DELETE FROM file_index WHERE project_id=?", (project_id,))
        _c().execute(
            "DELETE FROM ask_history WHERE project_id=? OR scope_id=?",
            (project_id, project_id),
        )
        _c().commit()
    machine.forget(project_id)   # drop the machine-local source-root config too


def set_project_state(project_id: str, state: str) -> None:
    with _lock:
        # 'deleting' is a ONE-WAY tombstone. The background job worker writes a
        # project's state as it unwinds (e.g. -> 'paused'/'ready'/'init' when a job
        # is terminated as part of an async delete); without this guard such a write
        # would revive a deleting project and it would reappear in the listing.
        row = _c().execute("SELECT state FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["state"] == "deleting" and state != "deleting":
            return
        _c().execute(
            "UPDATE projects SET state=?, updated_at=? WHERE id=?",
            (state, now(), project_id),
        )
        _c().commit()


def update_project_meta(project_id: str, meta: Dict[str, Any]) -> None:
    with _lock:
        _c().execute(
            "UPDATE projects SET meta_json=?, updated_at=? WHERE id=?",
            (json.dumps(meta), now(), project_id),
        )
        _c().commit()


def get_project_paths(project_id: str) -> List[Dict[str, Any]]:
    """The project's source-path specs — stored machine-locally (sidecar), so
    they never travel with a copied ``data/`` folder."""
    return machine.get_paths(project_id)


def set_project_paths(project_id: str, paths: List[Dict[str, Any]]) -> None:
    machine.set_paths(project_id, paths)
    # bump the portable record's freshness without persisting the path itself
    with _lock:
        _c().execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now(), project_id)
        )
        _c().commit()


def upsert_project_path(project_id: str, path: str, exclude: List[str]) -> None:
    """Add or update a path entry with its exclude-set (selection)."""
    paths = get_project_paths(project_id)
    norm = path.replace("\\", "/").rstrip("/")
    found = False
    for entry in paths:
        if entry["path"].replace("\\", "/").rstrip("/") == norm:
            entry["exclude"] = sorted(set(exclude))
            entry["updated_at"] = now()
            found = True
            break
    if not found:
        paths.append({
            "path": path,
            "exclude": sorted(set(exclude)),
            "added_at": now(),
            "updated_at": now(),
        })
    set_project_paths(project_id, paths)


def remove_project_path(project_id: str, path: str) -> None:
    norm = path.replace("\\", "/").rstrip("/")
    paths = [p for p in get_project_paths(project_id)
             if p["path"].replace("\\", "/").rstrip("/") != norm]
    set_project_paths(project_id, paths)




# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _job_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "project_id": row["project_id"],
        "type": row["type"],
        "path": row["path"],
        "status": row["status"],
        "step": row["step"],
        "progress": json.loads(row["progress_json"]),
        "log_tail": json.loads(row["log_tail_json"]),
        "control": json.loads(row["control_json"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def create_job(project_id: str, jtype: str, path: Optional[str]) -> Dict[str, Any]:
    jid = new_id("job_")
    ts = now()
    with _lock:
        _c().execute(
            "INSERT INTO jobs (job_id,project_id,type,path,status,step,progress_json,"
            "log_tail_json,control_json,error,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, project_id, jtype, path, "queued", "", "{}", "[]", "{}", "", ts, ts),
        )
        _c().commit()
    return get_job(jid)  # type: ignore[return-value]


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _job_row(row) if row else None


def list_jobs(project_ids: Optional[List[str]] = None,
              status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM jobs"
    clauses, args = [], []
    if project_ids:
        clauses.append("project_id IN (%s)" % ",".join("?" * len(project_ids)))
        args.extend(project_ids)
    if status:
        clauses.append("status=?")
        args.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    with _lock:
        rows = _c().execute(q, args).fetchall()
    return [_job_row(r) for r in rows]


def update_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Update arbitrary job fields. dict/list fields are json-encoded.
    Always bumps updated_at (persist every tick — Invariant 6)."""
    if not fields:
        return get_job(job_id)
    mapping = {
        "progress": "progress_json",
        "log_tail": "log_tail_json",
        "control": "control_json",
    }
    sets, args = [], []
    for k, v in fields.items():
        col = mapping.get(k, k)
        if col.endswith("_json") or isinstance(v, (dict, list)):
            v = json.dumps(v)
        sets.append(f"{col}=?")
        args.append(v)
    sets.append("updated_at=?")
    args.append(now())
    args.append(job_id)
    with _lock:
        _c().execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)
        _c().commit()
    return get_job(job_id)


def active_jobs() -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at"
        ).fetchall()
    return [_job_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Model config (singleton)
# ---------------------------------------------------------------------------
def get_model_config() -> Dict[str, Any]:
    with _lock:
        row = _c().execute("SELECT config_json FROM model_config WHERE id=1").fetchone()
    if not row:
        cfg = dict(config.DEFAULT_MODEL_CONFIG)
        set_model_config(cfg)
        return cfg
    cfg = dict(config.DEFAULT_MODEL_CONFIG)
    cfg.update(json.loads(row["config_json"]))
    return cfg


def set_model_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(config.DEFAULT_MODEL_CONFIG)
    merged.update(cfg)
    # Local-only guarantee (Invariant 1): never persist a non-loopback host. A 0.0.0.0
    # or LAN host would expose the model API (and every prompt's project content).
    merged["host"] = config.coerce_loopback(str(merged.get("host", "127.0.0.1")))
    with _lock:
        _c().execute(
            "INSERT INTO model_config (id,config_json,updated_at) VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,"
            " updated_at=excluded.updated_at",
            (json.dumps(merged), now()),
        )
        _c().commit()
    return merged


# ---------------------------------------------------------------------------
# File index (per-file content hash = incremental + checkpoint store)
# ---------------------------------------------------------------------------
def get_file_index(project_id: str) -> Dict[str, Dict[str, Any]]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM file_index WHERE project_id=?", (project_id,)
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[r["file_path"]] = {
            "file_path": r["file_path"],
            "file_hash": r["file_hash"],
            "status": r["status"],
            "chunk_ids": json.loads(r["chunk_ids_json"]),
            "service": r["service"],
            "topics": json.loads(r["topics_json"]),
            "updated_at": r["updated_at"],
        }
    return out


def upsert_file_index(project_id: str, file_path: str, file_hash: str,
                      chunk_ids: List[str], service: str = "",
                      topics: Optional[List[str]] = None,
                      status: str = "indexed") -> None:
    with _lock:
        _c().execute(
            "INSERT INTO file_index (project_id,file_path,file_hash,status,"
            "chunk_ids_json,service,topics_json,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id,file_path) DO UPDATE SET"
            " file_hash=excluded.file_hash, status=excluded.status,"
            " chunk_ids_json=excluded.chunk_ids_json, service=excluded.service,"
            " topics_json=excluded.topics_json, updated_at=excluded.updated_at",
            (project_id, file_path, file_hash, status, json.dumps(chunk_ids),
             service, json.dumps(topics or []), now()),
        )
        _c().commit()


def delete_file_index_entry(project_id: str, file_path: str) -> None:
    with _lock:
        _c().execute(
            "DELETE FROM file_index WHERE project_id=? AND file_path=?",
            (project_id, file_path),
        )
        _c().commit()


def count_file_index(project_id: str) -> int:
    """How many files are indexed, WITHOUT materializing them.

    ``get_file_index`` builds a dict of every row and json-decodes two columns
    per row; callers that only wanted ``len()`` of it (the project header card)
    paid ~36ms on a 6.5k-file project to learn a number SQLite answers in under
    a millisecond off the (project_id, file_path) primary-key index."""
    with _lock:
        row = _c().execute(
            "SELECT COUNT(*) FROM file_index WHERE project_id=?", (project_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def clear_file_index(project_id: str) -> None:
    with _lock:
        _c().execute("DELETE FROM file_index WHERE project_id=?", (project_id,))
        _c().commit()


# ---------------------------------------------------------------------------
# Ask conversation history (per-scope UI memory; bounded; survives restart)
# ---------------------------------------------------------------------------
ASK_HISTORY_CAP = 10            # retain at least the last N exchanges per scope


def _ask_row(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    base = {
        "id": row["exchange_id"],
        "scope_id": row["scope_id"],
        "project_id": row["project_id"],
        "job_id": row["job_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # payload carries question/answer/meta/etc.; reserved keys never overwritten
    for k, v in payload.items():
        if k not in base:
            base[k] = v
    return base


def ask_create(exchange_id: str, scope_id: str, project_id: Optional[str],
               payload: Dict[str, Any], status: str = "queued") -> Dict[str, Any]:
    ts = now()
    with _lock:
        _c().execute(
            "INSERT INTO ask_history (exchange_id,scope_id,project_id,job_id,status,"
            "payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (exchange_id, scope_id, project_id, None, status, json.dumps(payload), ts, ts),
        )
        _c().commit()
        _ask_trim(scope_id)
    return ask_get(exchange_id)  # type: ignore[return-value]


def ask_get(exchange_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM ask_history WHERE exchange_id=?", (exchange_id,)
        ).fetchone()
    return _ask_row(row) if row else None


def ask_list(scope_id: str, limit: int = ASK_HISTORY_CAP) -> List[Dict[str, Any]]:
    """Return the most recent *limit* exchanges for a scope, oldest-first."""
    with _lock:
        rows = _c().execute(
            "SELECT * FROM ask_history WHERE scope_id=? ORDER BY rowid DESC LIMIT ?",
            (scope_id, limit),
        ).fetchall()
    return [_ask_row(r) for r in reversed(rows)]


def ask_update(exchange_id: str, *, status: Optional[str] = None,
               job_id: Optional[str] = None,
               payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Patch an exchange. *payload* keys are MERGED into the stored payload.

    The read-merge-write is done inside ONE lock so two concurrent updaters (the
    worker finalizing an answer + a request thread stamping saved_case_id) can't
    clobber each other's payload keys (lost-update race)."""
    with _lock:
        row = _c().execute(
            "SELECT payload_json FROM ask_history WHERE exchange_id=?", (exchange_id,)
        ).fetchone()
        if not row:
            return None
        try:
            merged = json.loads(row["payload_json"] or "{}")
        except Exception:
            merged = {}
        if payload:
            merged.update(payload)
        sets, args = [], []
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if job_id is not None:
            sets.append("job_id=?")
            args.append(job_id)
        sets.append("payload_json=?")
        args.append(json.dumps(merged))
        sets.append("updated_at=?")
        args.append(now())
        args.append(exchange_id)
        _c().execute(f"UPDATE ask_history SET {', '.join(sets)} WHERE exchange_id=?", args)
        _c().commit()
    return ask_get(exchange_id)


def ask_claim_case(exchange_id: str, case_id: str) -> Optional[Dict[str, Any]]:
    """Atomically set saved_case_id IFF not already set (idempotent save guard).
    Returns {case_id, claimed}: claimed=True if THIS caller won the claim, False
    if a case was already saved (returns the existing id). None if no exchange."""
    with _lock:
        row = _c().execute(
            "SELECT payload_json FROM ask_history WHERE exchange_id=?", (exchange_id,)
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        existing = payload.get("saved_case_id")
        if existing:
            return {"case_id": existing, "claimed": False}
        payload["saved_case_id"] = case_id
        _c().execute(
            "UPDATE ask_history SET payload_json=?, updated_at=? WHERE exchange_id=?",
            (json.dumps(payload), now(), exchange_id),
        )
        _c().commit()
    return {"case_id": case_id, "claimed": True}


def ask_clear(scope_id: str) -> None:
    with _lock:
        _c().execute("DELETE FROM ask_history WHERE scope_id=?", (scope_id,))
        _c().commit()


def _ask_trim(scope_id: str, keep: int = ASK_HISTORY_CAP) -> None:
    """Drop exchanges beyond the newest *keep* for a scope (called under _lock)."""
    _c().execute(
        "DELETE FROM ask_history WHERE scope_id=? AND exchange_id NOT IN "
        "(SELECT exchange_id FROM ask_history WHERE scope_id=? ORDER BY rowid DESC LIMIT ?)",
        (scope_id, scope_id, keep),
    )
    _c().commit()


# ---------------------------------------------------------------------------
# kv
# ---------------------------------------------------------------------------
def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        row = _c().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO kv (key,value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _c().commit()


# ---------------------------------------------------------------------------
# Canonical Asset model (OpenMind v2 Phase 2)
#
# Assets/revisions/segments/evidence are the durable content-identity model;
# Chroma stays a retrieval projection. Every write goes through the same single
# WAL connection and _lock as the rest of this module. Reads are workspace-scoped
# at the SQL level (a JOIN back to assets.workspace_id), so an id from one
# workspace can never be read through another. See migrations/versions/
# v0003_asset_model.py for the schema.
# ---------------------------------------------------------------------------
def _asset_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "logical_key": row["logical_key"],
        "asset_type": row["asset_type"],
        "title": row["title"],
        "source_kind": row["source_kind"],
        "source_path": row["source_path"],
        "media_type": row["media_type"],
        "state": row["state"],
        "current_revision_id": row["current_revision_id"],
        "metadata": json.loads(row["metadata_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _revision_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "asset_id": row["asset_id"],
        "sequence": row["sequence"],
        "content_hash": row["content_hash"],
        "content_size": row["content_size"],
        "content_blob_hash": row["content_blob_hash"],
        "status": row["status"],
        "version_label": row["version_label"],
        "source_commit": row["source_commit"],
        "supersedes_revision_id": row["supersedes_revision_id"],
        "metadata": json.loads(row["metadata_json"]),
        "created_at": row["created_at"],
    }


def _segment_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "revision_id": row["revision_id"],
        "segment_key": row["segment_key"],
        "segment_type": row["segment_type"],
        "ordinal": row["ordinal"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "symbol": row["symbol"],
        "content_hash": row["content_hash"],
        "content_mode": row["content_mode"],
        "metadata": json.loads(row["metadata_json"]),
    }


def _evidence_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "revision_id": row["revision_id"],
        "segment_id": row["segment_id"],
        "locator": json.loads(row["locator_json"]),
        "excerpt": row["excerpt"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
    }


# -- asset reads ------------------------------------------------------------
def get_asset(workspace_id: str, asset_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM assets WHERE id=? AND workspace_id=?",
            (asset_id, workspace_id),
        ).fetchone()
    return _asset_row(row) if row else None


def find_asset_by_logical_key(workspace_id: str,
                              logical_key: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM assets WHERE workspace_id=? AND logical_key=?",
            (workspace_id, logical_key),
        ).fetchone()
    return _asset_row(row) if row else None


def list_assets(workspace_id: str, asset_type: Optional[str] = None,
                state: Optional[str] = None, limit: int = 100,
                offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM assets WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if asset_type:
        q += " AND asset_type=?"
        args.append(asset_type)
    if state:
        q += " AND state=?"
        args.append(state)
    # Deterministic ordering so a bounded page is stable across calls.
    q += " ORDER BY logical_key LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    with _lock:
        rows = _c().execute(q, args).fetchall()
    return [_asset_row(r) for r in rows]


def count_assets(workspace_id: str, asset_type: Optional[str] = None,
                 state: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) FROM assets WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if asset_type:
        q += " AND asset_type=?"
        args.append(asset_type)
    if state:
        q += " AND state=?"
        args.append(state)
    with _lock:
        row = _c().execute(q, args).fetchone()
    return int(row[0]) if row else 0


def list_asset_index(workspace_id: str) -> Dict[str, Dict[str, Any]]:
    """logical_key -> {asset_id, state, current_revision_id, content_hash} for the
    whole workspace, in one query. The ingestion loop loads this once (like
    :func:`get_file_index`) to decide, per file, whether an Asset already exists
    and whether its current revision already matches — without a query per file
    and without re-hashing unchanged content."""
    with _lock:
        rows = _c().execute(
            "SELECT a.logical_key AS lk, a.id AS aid, a.state AS state, "
            "a.current_revision_id AS crid, r.content_hash AS chash "
            "FROM assets a LEFT JOIN asset_revisions r "
            "ON a.current_revision_id = r.id WHERE a.workspace_id=?",
            (workspace_id,),
        ).fetchall()
    return {
        r["lk"]: {"asset_id": r["aid"], "state": r["state"],
                  "current_revision_id": r["crid"], "content_hash": r["chash"]}
        for r in rows
    }


# -- revision reads ---------------------------------------------------------
def get_revision(workspace_id: str, revision_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT r.* FROM asset_revisions r JOIN assets a ON r.asset_id=a.id "
            "WHERE r.id=? AND a.workspace_id=?",
            (revision_id, workspace_id),
        ).fetchone()
    return _revision_row(row) if row else None


def list_revisions(workspace_id: str, asset_id: str,
                   limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute(
            "SELECT r.* FROM asset_revisions r JOIN assets a ON r.asset_id=a.id "
            "WHERE a.id=? AND a.workspace_id=? ORDER BY r.sequence DESC LIMIT ?",
            (asset_id, workspace_id, max(0, int(limit))),
        ).fetchall()
    return [_revision_row(r) for r in rows]


# -- segment / evidence reads (all workspace-scoped via JOIN) ---------------
def list_segments(workspace_id: str, revision_id: str, limit: int = 200,
                  offset: int = 0) -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute(
            "SELECT s.* FROM segments s "
            "JOIN asset_revisions r ON s.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE s.revision_id=? AND a.workspace_id=? "
            "ORDER BY s.ordinal LIMIT ? OFFSET ?",
            (revision_id, workspace_id, max(0, int(limit)), max(0, int(offset))),
        ).fetchall()
    return [_segment_row(r) for r in rows]


def count_segments(workspace_id: str, revision_id: str) -> int:
    with _lock:
        row = _c().execute(
            "SELECT COUNT(*) FROM segments s "
            "JOIN asset_revisions r ON s.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE s.revision_id=? AND a.workspace_id=?",
            (revision_id, workspace_id),
        ).fetchone()
    return int(row[0]) if row else 0


def get_segment(workspace_id: str, segment_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT s.* FROM segments s "
            "JOIN asset_revisions r ON s.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE s.id=? AND a.workspace_id=?",
            (segment_id, workspace_id),
        ).fetchone()
    return _segment_row(row) if row else None


def get_evidence(workspace_id: str, evidence_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT e.* FROM evidence e "
            "JOIN asset_revisions r ON e.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE e.id=? AND a.workspace_id=?",
            (evidence_id, workspace_id),
        ).fetchone()
    return _evidence_row(row) if row else None


def evidence_ids_for_revision(workspace_id: str,
                              revision_id: str) -> Dict[str, str]:
    """segment_id -> evidence_id for one revision, in a single scoped query, so a
    segment listing can surface each segment's evidence id (the discovery path
    for ``asset evidence``) without a query per segment."""
    with _lock:
        rows = _c().execute(
            "SELECT e.segment_id AS sid, e.id AS eid FROM evidence e "
            "JOIN asset_revisions r ON e.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE e.revision_id=? AND a.workspace_id=? AND e.segment_id IS NOT NULL",
            (revision_id, workspace_id),
        ).fetchall()
    return {r["sid"]: r["eid"] for r in rows}


def get_evidence_for_segment(workspace_id: str,
                             segment_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute(
            "SELECT e.* FROM evidence e "
            "JOIN asset_revisions r ON e.revision_id=r.id "
            "JOIN assets a ON r.asset_id=a.id "
            "WHERE e.segment_id=? AND a.workspace_id=? "
            "ORDER BY e.created_at LIMIT 1",
            (segment_id, workspace_id),
        ).fetchone()
    return _evidence_row(row) if row else None


def asset_stats(workspace_id: str) -> Dict[str, int]:
    """Aggregate counts for the workspace: assets (total/active/removed),
    revisions, segments, evidence. One small query per count, all scoped."""
    with _lock:
        c = _c()
        total = c.execute("SELECT COUNT(*) FROM assets WHERE workspace_id=?",
                          (workspace_id,)).fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM assets WHERE workspace_id=? AND state='active'",
            (workspace_id,)).fetchone()[0]
        removed = c.execute(
            "SELECT COUNT(*) FROM assets WHERE workspace_id=? AND state='removed'",
            (workspace_id,)).fetchone()[0]
        revisions = c.execute(
            "SELECT COUNT(*) FROM asset_revisions r JOIN assets a "
            "ON r.asset_id=a.id WHERE a.workspace_id=?", (workspace_id,)).fetchone()[0]
        segments = c.execute(
            "SELECT COUNT(*) FROM segments s JOIN asset_revisions r "
            "ON s.revision_id=r.id JOIN assets a ON r.asset_id=a.id "
            "WHERE a.workspace_id=?", (workspace_id,)).fetchone()[0]
        evidence = c.execute(
            "SELECT COUNT(*) FROM evidence e JOIN asset_revisions r "
            "ON e.revision_id=r.id JOIN assets a ON r.asset_id=a.id "
            "WHERE a.workspace_id=?", (workspace_id,)).fetchone()[0]
    return {
        "assets_total": int(total), "assets_active": int(active),
        "assets_removed": int(removed), "revisions": int(revisions),
        "segments": int(segments), "evidence": int(evidence),
    }


# -- writes -----------------------------------------------------------------
def upsert_asset(workspace_id: str, logical_key: str, *, asset_type: str,
                 title: str, source_path: str, state: str = "active",
                 media_type: str = "", source_kind: str = "file",
                 metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create or update an Asset row WITHOUT a revision — used to register an
    unsupported file honestly (``state='unsupported'``), so it is recorded but
    never falsely reported as parsed. Never touches revisions/segments/evidence."""
    ts = now()
    with _lock:
        row = _c().execute(
            "SELECT id FROM assets WHERE workspace_id=? AND logical_key=?",
            (workspace_id, logical_key)).fetchone()
        if row:
            _c().execute(
                "UPDATE assets SET asset_type=?, title=?, source_path=?, state=?, "
                "media_type=?, source_kind=?, updated_at=? WHERE id=?",
                (asset_type, title, source_path, state, media_type, source_kind,
                 ts, row["id"]))
            asset_id = row["id"]
        else:
            asset_id = new_id("a_")
            _c().execute(
                "INSERT INTO assets (id,workspace_id,logical_key,asset_type,title,"
                "source_kind,source_path,media_type,state,current_revision_id,"
                "metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, workspace_id, logical_key, asset_type, title, source_kind,
                 source_path, media_type, state, None, json.dumps(metadata or {}),
                 ts, ts))
        _c().commit()
    return get_asset(workspace_id, asset_id)  # type: ignore[return-value]


def mark_asset_removed(workspace_id: str, logical_key: str) -> Optional[str]:
    """Set an Asset's state to ``removed`` (source file gone/excluded), keeping
    ALL of its revisions, segments, evidence and content blobs. Returns the asset
    id if a state change happened, else None (already removed or absent)."""
    with _lock:
        row = _c().execute(
            "SELECT id, state FROM assets WHERE workspace_id=? AND logical_key=?",
            (workspace_id, logical_key),
        ).fetchone()
        if not row or row["state"] == "removed":
            return None
        _c().execute(
            "UPDATE assets SET state='removed', updated_at=? "
            "WHERE id=?", (now(), row["id"]),
        )
        _c().commit()
    return row["id"]


def clear_workspace_assets(workspace_id: str) -> None:
    """Delete every Asset (and, by cascade, its revisions/segments/evidence) for
    a workspace. Used by TERMINATE, which wipes learned data. Content blobs are
    removed separately by the caller (content_store.clear_workspace)."""
    with _lock:
        _c().execute("DELETE FROM assets WHERE workspace_id=?", (workspace_id,))
        _c().commit()


def commit_revision(
    workspace_id: str,
    logical_key: str,
    *,
    asset_type: str,
    title: str,
    source_path: str,
    content_hash: str,
    content_size: int,
    content_blob_hash: str,
    segments: List[Dict[str, Any]],
    media_type: str = "",
    source_kind: str = "file",
    source_commit: str = "",
    revision_status: str = "unknown",
    version_label: str = "",
    revision_metadata: Optional[Dict[str, Any]] = None,
    asset_metadata: Optional[Dict[str, Any]] = None,
    asset_state: str = "active",
) -> Dict[str, Any]:
    """The single transactional writer for the Asset model.

    Creates or reuses the Asset for ``(workspace_id, logical_key)`` and, when the
    observed ``content_hash`` differs from the Asset's current revision, mints the
    next immutable revision together with all of its segments and evidence,
    flips the previous current revision to ``superseded``, and repoints
    ``current_revision_id`` — ALL in one transaction. A revision is therefore
    never made current before its segments and evidence are committed.

    When the content already matches the current revision this creates NO new
    revision (idempotent / revert-safe at the identity layer); it still
    reactivates a ``removed`` asset that has reappeared.

    ``segments`` is a list of drafts; each may carry an ``evidence`` dict
    ``{locator, excerpt, content_hash}``. Returns a summary:
    ``{asset_id, revision, revision_created, asset_created, reactivated,
       segments_created, evidence_created}``.
    """
    ts = now()
    rev_meta = json.dumps(revision_metadata or {})
    with _lock:
        c = _c()
        try:
            existing = c.execute(
                "SELECT * FROM assets WHERE workspace_id=? AND logical_key=?",
                (workspace_id, logical_key),
            ).fetchone()

            reactivated = False
            asset_created = False

            if existing is not None:
                asset_id = existing["id"]
                cur_rev_id = existing["current_revision_id"]
                cur_hash = None
                if cur_rev_id:
                    cr = c.execute(
                        "SELECT content_hash FROM asset_revisions WHERE id=?",
                        (cur_rev_id,)).fetchone()
                    cur_hash = cr["content_hash"] if cr else None
                # Unchanged content that already IS the current revision: no new
                # revision. Reactivate a reappeared (removed) asset.
                if cur_rev_id and cur_hash == content_hash:
                    if existing["state"] != asset_state:
                        reactivated = existing["state"] == "removed"
                        c.execute(
                            "UPDATE assets SET state=?, updated_at=? WHERE id=?",
                            (asset_state, ts, asset_id))
                        c.commit()
                    else:
                        c.commit()
                    rev = c.execute(
                        "SELECT * FROM asset_revisions WHERE id=?",
                        (cur_rev_id,)).fetchone()
                    return {
                        "asset_id": asset_id,
                        "revision": _revision_row(rev) if rev else None,
                        "revision_created": False, "asset_created": False,
                        "reactivated": reactivated,
                        "segments_created": 0, "evidence_created": 0,
                    }
                # Content differs -> a new revision follows. Reactivating flag is
                # true when the asset was removed before this new observation.
                reactivated = existing["state"] == "removed"
                supersedes = cur_rev_id
            else:
                asset_id = new_id("a_")
                asset_created = True
                supersedes = None
                c.execute(
                    "INSERT INTO assets (id,workspace_id,logical_key,asset_type,"
                    "title,source_kind,source_path,media_type,state,"
                    "current_revision_id,metadata_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (asset_id, workspace_id, logical_key, asset_type, title,
                     source_kind, source_path, media_type, asset_state, None,
                     json.dumps(asset_metadata or {}), ts, ts))

            # next dense sequence for this asset
            seq_row = c.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM asset_revisions WHERE asset_id=?",
                (asset_id,)).fetchone()
            sequence = int(seq_row[0]) + 1

            revision_id = new_id("r_")
            c.execute(
                "INSERT INTO asset_revisions (id,asset_id,sequence,content_hash,"
                "content_size,content_blob_hash,status,version_label,source_commit,"
                "supersedes_revision_id,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, asset_id, sequence, content_hash, int(content_size),
                 content_blob_hash, revision_status, version_label, source_commit,
                 supersedes, rev_meta, ts))

            seg_count = 0
            ev_count = 0
            for seg in segments:
                seg_id = new_id("s_")
                c.execute(
                    "INSERT INTO segments (id,revision_id,segment_key,segment_type,"
                    "ordinal,start_line,end_line,symbol,content_hash,content_mode,"
                    "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (seg_id, revision_id, seg["segment_key"], seg["segment_type"],
                     int(seg["ordinal"]), seg.get("start_line"), seg.get("end_line"),
                     seg.get("symbol", ""), seg.get("content_hash", ""),
                     seg.get("content_mode", "verbatim"),
                     json.dumps(seg.get("metadata") or {})))
                seg_count += 1
                ev = seg.get("evidence")
                if ev:
                    c.execute(
                        "INSERT INTO evidence (id,revision_id,segment_id,locator_json,"
                        "excerpt,content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                        (new_id("e_"), revision_id, seg_id,
                         json.dumps(ev.get("locator") or {}), ev.get("excerpt", ""),
                         ev.get("content_hash", ""), ts))
                    ev_count += 1

            if supersedes:
                c.execute(
                    "UPDATE asset_revisions SET status='superseded' WHERE id=?",
                    (supersedes,))

            # repoint current + (re)activate + refresh derived asset fields last,
            # so current_revision_id never names a half-written revision.
            c.execute(
                "UPDATE assets SET current_revision_id=?, state=?, asset_type=?, "
                "title=?, source_path=?, media_type=?, updated_at=? WHERE id=?",
                (revision_id, asset_state, asset_type, title, source_path,
                 media_type, ts, asset_id))
            c.commit()
        except Exception:
            _c().rollback()
            raise

        rev = c.execute("SELECT * FROM asset_revisions WHERE id=?",
                        (revision_id,)).fetchone()
    return {
        "asset_id": asset_id,
        "revision": _revision_row(rev) if rev else None,
        "revision_created": True, "asset_created": asset_created,
        "reactivated": reactivated,
        "segments_created": seg_count, "evidence_created": ev_count,
    }
