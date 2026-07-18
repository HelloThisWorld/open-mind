"""Move legacy in-DB project paths into the machine-local sidecar.

The absolute ingest root used to live in ``projects.paths_json``. That put a
machine-specific absolute path inside the portable database, which breaks the
zero-origin-traces constraint: a copied ``data/`` folder carried the original
checkout's location. Paths now live in the machine-local sidecar
(:mod:`openmind.machine`), outside the data directory.

This logic ran unconditionally on every startup before migrations existed. It
is idempotent either way, but recording it here means the full scan of every
project row happens once instead of on every process start.

FROZEN. Applied to real databases; checksummed. Do not edit — add a new
migration instead.
"""
from __future__ import annotations

import json
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    # Imported inside the function, not at module scope: discovery imports every
    # migration module, and a migration should not drag the application layer in
    # just by being enumerated.
    from ... import machine

    rows = conn.execute("SELECT id, paths_json FROM projects").fetchall()
    for row in rows:
        project_id, raw = row[0], row[1]
        try:
            legacy = json.loads(raw or "[]")
        except (TypeError, ValueError):
            legacy = []
        if not legacy:
            continue
        # Never clobber a sidecar entry the user already has on this machine.
        if not machine.get_paths(project_id):
            machine.set_paths(project_id, legacy)
        conn.execute("UPDATE projects SET paths_json='[]' WHERE id=?", (project_id,))
