"""Baseline: the schema as it stood before migrations existed.

Every statement is ``IF NOT EXISTS``, which is what makes baselining a legacy
database safe: against a database that already has these tables the whole
migration is a no-op, and the runner simply records version 1. No table is
recreated and no row is touched, so existing project, job, file-index, cases
and Ask data survives untouched.

FROZEN. This migration has been applied to real databases; its content is
checksummed and must not change. Express any schema change as a new migration.
"""
from __future__ import annotations

STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'init',
        paths_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        type TEXT NOT NULL,
        path TEXT,
        status TEXT NOT NULL,
        step TEXT DEFAULT '',
        progress_json TEXT NOT NULL DEFAULT '{}',
        log_tail_json TEXT NOT NULL DEFAULT '[]',
        control_json TEXT NOT NULL DEFAULT '{}',
        error TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        config_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_index (
        project_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'indexed',
        chunk_ids_json TEXT NOT NULL DEFAULT '[]',
        service TEXT DEFAULT '',
        topics_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, file_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # Ask conversation history (UI memory): persistent, per-scope, bounded.
    # Distinct from solved cases (curated knowledge in the cases store).
    """
    CREATE TABLE IF NOT EXISTS ask_history (
        exchange_id TEXT PRIMARY KEY,
        scope_id TEXT NOT NULL,
        project_id TEXT,
        job_id TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ask_scope ON ask_history(scope_id)",
)
