# Database migrations

OpenMind's SQLite schema evolves through a small, strict migration runner:
[`openmind/migrations/`](../openmind/migrations/). Migrations run automatically
on every `db.init_db()` — which means on every runtime bootstrap, from the CLI,
the MCP server, the FastAPI app and the tests alike.

Check the current version at any time:

```bash
python -m openmind.cli doctor --json | jq '.checks[] | select(.name=="migrations")'
```

---

## Why not Alembic

This is one SQLite file with a handful of tables, already serialized behind a
single WAL connection and one process-level lock. Alembic would add a
dependency, a config file, an `env.py` and a revision graph to solve problems
OpenMind does not have (multiple backends, branching revisions, autogenerate).

What OpenMind *does* need is the part Alembic is usually not configured to
enforce: a recorded checksum per migration, so editing an already-applied
migration is caught loudly instead of silently diverging one developer's
database from another's. That is about 150 lines.

If a future phase brings multiple backends or a real revision graph, this is the
seam to replace.

---

## The ledger

```sql
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,   -- sha256 of the migration payload
    applied_at TEXT NOT NULL
);
```

## Guarantees

1. Works on a completely empty database.
2. Works on a legacy database that has the current tables but no ledger.
3. Never destroys user data.
4. Applies in ascending numeric order — for every caller, including tests that
   inject an unordered list.
5. Each migration runs in **one transaction**; a failure rolls it back whole and
   writes no ledger row for it.
6. Records a SHA-256 checksum of the migration payload.
7. A changed checksum on an applied migration raises
   `MigrationChecksumMismatch` naming the version and both checksums.
8. Runs under the same lock that guards the shared connection, so the job worker
   and request threads cannot race it.
9. Idempotent — a second run applies nothing.
10. The version is reported through `doctor` and `GET /api/health`.

### Transactions and DDL

`sqlite3` only opens an implicit transaction for DML, not DDL, so a
`CREATE TABLE` would otherwise run in autocommit and survive a rollback. The
runner switches the connection to explicit-transaction mode
(`isolation_level = None`) for its own duration and issues `BEGIN` / `COMMIT` /
`ROLLBACK` itself, restoring the previous mode afterwards. It never uses
`executescript`, which would `COMMIT` before running.

---

## Current migrations

| Version | Name | What it does |
| --- | --- | --- |
| 1 | `baseline` | the schema as it stood before migrations existed: `projects`, `jobs`, `model_config`, `file_index`, `kv`, `ask_history` + its index |
| 2 | `paths_sidecar` | moves legacy in-DB project paths into the machine-local sidecar and blanks `projects.paths_json` |
| 3 | `asset_model` | the canonical Asset model: `assets`, `asset_revisions`, `segments`, `evidence` + their indexes. Additive — creates only new tables (all `IF NOT EXISTS`), touches no existing row |
| 4 | `document_ingestion` | document ingestion: `segments.content_blob_hash`, `jobs.payload_json`, `document_parses`, `document_index` + their indexes. Additive — two columns with defaults and two new tables |

`v0003` adds the OpenMind v2 canonical content-identity model. `assets`
references `projects(id)` and the whole subtree cascades on `ON DELETE CASCADE`,
so removing a project drops its assets, revisions, segments and evidence in one
statement (with the connection's `PRAGMA foreign_keys=ON`). `(asset_id,
content_hash)` is deliberately **not** unique so an A → B → A revert is
representable. Content bytes live in an immutable content-addressed blob store
(`data/<workspace>/objects/…`), never in the database — the DB stores only the
SHA-256 hash. See [docs/v2/phase-2-asset-model.md](v2/phase-2-asset-model.md).

`v0004` adds the document-ingestion plane. Two of its four changes deserve a
word about *why*:

* **`segments.content_blob_hash`** — code Evidence is recovered by slicing
  `[startLine, endLine]` out of the revision blob, which is safe forever because
  the byte-to-line mapping never changes. A *document* block cannot be recovered
  that way: re-deriving it needs a parser rerun, and a future parser version may
  legitimately produce different boundaries, which would silently rewrite
  history. Document segments therefore snapshot their exact text as their own
  content-addressed blob. Existing code segments keep `''` and are deliberately
  **not** backfilled.
* **`jobs.payload_json`** — a document import must reach the worker without an
  absolute machine path in the portable database. The free `path` column already
  means something different per job type, so the safe import payload (staged
  blob hash, filename, requested target, import mode) gets its own column.

`document_parses` is a derived parse projection over an immutable Revision, and
its *presence* for an Asset's current Revision is the definition of "this Asset
is a document" — a recorded fact, never an inference from the file extension.
`document_index` names the vector chunks currently live for an Asset, so a new
current Revision replaces exactly its predecessor's chunks. See
[docs/v2/phase-3-document-ingestion.md](v2/phase-3-document-ingestion.md).

Unusually, `v0004` declares `upgrade(conn)` rather than `STATEMENTS`: SQLite has
no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and a plain `ADD COLUMN` raises
"duplicate column name" on a second run. Reading `PRAGMA table_info` first keeps
the whole migration idempotent.

### Upgrading an existing database

Nothing to do — open OpenMind and it migrates itself. Concretely:

```text
empty database    -> v0001..v0004 create every table     -> ledger records 1..4
legacy database   -> v0001 statements are all no-ops,     -> ledger records 1..4
                     v0002..v0004 apply additively           (existing data untouched)
current database  -> nothing to apply                     -> no writes
```

A Phase 1 database (already at v0002) upgrades to v0003 with no data loss:
`v0003` only *creates* the new Asset tables. The Asset rows for existing files
are then backfilled on the next ingestion — without re-embedding unchanged
files, reusing their existing Chroma chunks.

A Phase 2 database (at v0003) upgrades to v0004 with no data loss either:
`v0004` adds two columns whose defaults are correct for every existing row, and
two tables that start empty. No project, path, job, Asset, Revision, Segment,
Evidence row, content blob, file-index row, vector collection, map, case, Ask
exchange or template selection is touched.

A legacy database is **baselined, not recreated**. `v0001` is written entirely
with `CREATE TABLE IF NOT EXISTS`, so against a database that already has those
tables every statement is a no-op and the runner simply records version 1.
Project, job, file-index, cases and Ask data is never touched.
`runner.detect_legacy()` reports this explicitly, and `doctor` surfaces it as
`baselined_legacy`.

`v0002` carries the path-sidecar logic that previously ran unconditionally on
every startup. It is still idempotent, but recording it means the full scan of
every project row happens once instead of on every process start.

---

## Writing a migration

Create `openmind/migrations/versions/v<NNNN>_<name>.py`. Declare **exactly one**
of `STATEMENTS` or `upgrade(conn)`.

SQL — the common case:

```python
"""Add a per-workspace ingest cursor."""
from __future__ import annotations

STATEMENTS = (
    "ALTER TABLE projects ADD COLUMN ingest_cursor TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_projects_state ON projects(state)",
)
```

Python — when the change needs logic:

```python
"""Backfill the ingest cursor from the newest file-index row."""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id FROM projects").fetchall():
        newest = conn.execute(
            "SELECT MAX(updated_at) FROM file_index WHERE project_id=?",
            (row[0],)).fetchone()[0]
        conn.execute("UPDATE projects SET ingest_cursor=? WHERE id=?",
                     (newest or "", row[0]))
```

Rules:

* **One statement per entry** in `STATEMENTS`. The runner executes them
  individually; it does not split a SQL blob, so no fragile `;` parsing.
* **Do not** `commit()`, `BEGIN` or `ROLLBACK` inside a migration — the runner
  owns the transaction.
* Keep imports of application modules *inside* `upgrade()`. Discovery imports
  every migration module, and a migration should not drag the application layer
  in just by being enumerated.
* Prefer additive changes. SQLite cannot drop or retype a column in place; a
  destructive change needs a create-copy-swap and an explicit review.

Then add a case to `tests/verify_migrations.py`.

---

## Checksums: applied migrations are immutable

The checksum covers the migration payload — the joined `STATEMENTS`, or the
source of `upgrade()`. Editing a migration that has already been applied
anywhere raises:

```text
migration 0002_paths_sidecar has already been applied, but its content changed
on disk (stored checksum a1b2c3d4e5f6..., computed 9f8e7d6c5b4a...). Applied
migrations are immutable: revert the edit, or add a NEW migration expressing
the change.
```

This includes comment and whitespace edits, because the checksum is over the
source text. That is deliberate: the ledger's promise is "version N is exactly
this change", and a build cannot tell a cosmetic edit from a semantic one.

**To fix a bad migration that has already shipped:** write a new migration that
corrects it. Do not edit the old one.

**During local development**, before a migration has left your machine, it is
fine to edit it and start from a clean database — delete `data/openmind.db` (or
point `OPENMIND_DATA_DIR` at a temp directory) and let it re-apply.

---

## A database newer than the code

If the ledger contains a version this build does not know about — someone ran a
newer OpenMind against the same data directory — the runner does **not** fail
and does **not** try to undo anything. It reports the unknown versions in
`unknown_applied`, and `doctor` shows a warning:

```text
[ warn ] migrations: schema version 5, but this build does not know
         migration(s) [4, 5] — the database was written by a newer OpenMind
```

Refusing to start would lock a user out of their own data over a situation that
is usually harmless (additive migrations). Reporting it loudly is the honest
middle ground.

---

## Tests

`tests/verify_migrations.py` covers: empty database, repeat no-op, legacy
baseline with data preserved, checksum mismatch, transactional rollback,
ordering, a newer-than-code database, and the shape of the real migration set.

```bash
python tests/verify_migrations.py
python scripts/run_acceptance.py --only verify_migrations
```
