"""Migration runner — empty database, legacy baseline, idempotency, checksum
immutability, and transactional rollback.

Pure stdlib + sqlite3: no embeddings, no vector store, no model. Every case
builds its own throwaway database file so nothing here can touch real data.
"""
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from openmind import migrations  # noqa: E402
from openmind.migrations import runner  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def _db():
    """A fresh, empty sqlite database file."""
    path = os.path.join(tempfile.mkdtemp(prefix="om_mig_"), "openmind.db")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _fake(version, name, statements, payload=None):
    """A migration built from literal SQL, for cases that must not depend on
    the real (frozen) migration set."""
    sql = tuple(statements)

    def apply(conn):
        for s in sql:
            conn.execute(s)

    return runner.Migration(version, name, payload or "\n".join(sql), apply)


# ---------------------------------------------------------------------------
# 1. Empty database
# ---------------------------------------------------------------------------
conn = _db()
result = migrations.migrate(conn)
tables = _tables(conn)

check("empty db: runner reports the head version", result.version == 2)
check("empty db: both migrations are applied",
      result.applied == ["0001_baseline", "0002_paths_sidecar"])
check("empty db: nothing was reported as already applied", result.already_applied == [])
check("empty db: not flagged as a legacy baseline", result.baselined_legacy is False)
check("empty db: schema_migrations ledger exists", "schema_migrations" in tables)
for t in ("projects", "jobs", "model_config", "file_index", "kv", "ask_history"):
    check(f"empty db: table '{t}' created", t in tables)
check("empty db: ask_history index created",
      conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND "
                   "name='idx_ask_scope'").fetchone() is not None)
check("empty db: ledger rows carry a checksum and a timestamp",
      all(r["checksum"] and r["applied_at"] for r in
          conn.execute("SELECT * FROM schema_migrations").fetchall()))

# ---------------------------------------------------------------------------
# 2. Repeat run is a no-op
# ---------------------------------------------------------------------------
again = migrations.migrate(conn)
check("repeat run: applies nothing", again.applied == [])
check("repeat run: reports both as already applied",
      again.already_applied == ["0001_baseline", "0002_paths_sidecar"])
check("repeat run: version is unchanged", again.version == result.version)

# ---------------------------------------------------------------------------
# 3. Legacy database — has the tables, no ledger. Must be baselined, not wiped.
# ---------------------------------------------------------------------------
legacy = _db()
legacy.executescript(
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'init',
        paths_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL,
        path TEXT, status TEXT NOT NULL, step TEXT DEFAULT '',
        progress_json TEXT NOT NULL DEFAULT '{}',
        log_tail_json TEXT NOT NULL DEFAULT '[]',
        control_json TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT);
    CREATE TABLE file_index (
        project_id TEXT NOT NULL, file_path TEXT NOT NULL,
        file_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'indexed',
        chunk_ids_json TEXT NOT NULL DEFAULT '[]', service TEXT DEFAULT '',
        topics_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, file_path));
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO projects VALUES
        ('p_legacy0001','Legacy Project','ready','[]','2026-01-01T00:00:00',
         '2026-01-01T00:00:00','{"template":"spring-boot"}');
    INSERT INTO jobs VALUES
        ('job_legacy01','p_legacy0001','ingest',NULL,'done','',
         '{"files_done":7}','[]','{}','','2026-01-01T00:00:00',
         '2026-01-01T00:00:00',NULL,NULL);
    INSERT INTO file_index VALUES
        ('p_legacy0001','src/Main.java','abc123','indexed','["c1"]','svc','[]',
         '2026-01-01T00:00:00');
    INSERT INTO kv VALUES ('last_project_folder','/somewhere');
    """
)
legacy.commit()

check("legacy db: detected as legacy before migrating", runner.detect_legacy(legacy) is True)

legacy_result = migrations.migrate(legacy)

check("legacy db: reported as a legacy baseline", legacy_result.baselined_legacy is True)
check("legacy db: brought to head version", legacy_result.version == 2)
check("legacy db: baseline recorded, not skipped",
      "0001_baseline" in legacy_result.applied)
check("legacy db: project row survived",
      legacy.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1)
check("legacy db: project meta survived intact",
      legacy.execute("SELECT meta_json FROM projects WHERE id='p_legacy0001'"
                     ).fetchone()[0] == '{"template":"spring-boot"}')
check("legacy db: project state survived intact",
      legacy.execute("SELECT state FROM projects WHERE id='p_legacy0001'"
                     ).fetchone()[0] == "ready")
check("legacy db: job row survived",
      legacy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1)
check("legacy db: file index row survived",
      legacy.execute("SELECT COUNT(*) FROM file_index").fetchone()[0] == 1)
check("legacy db: kv row survived",
      legacy.execute("SELECT value FROM kv WHERE key='last_project_folder'"
                     ).fetchone()[0] == "/somewhere")
check("legacy db: the missing ask_history table was added",
      "ask_history" in _tables(legacy))
check("legacy db: no longer detected as legacy once baselined",
      runner.detect_legacy(legacy) is False)

# ---------------------------------------------------------------------------
# 4. Checksum immutability
# ---------------------------------------------------------------------------
drift = _db()
original = _fake(1, "widgets", ["CREATE TABLE widgets (id TEXT PRIMARY KEY)"])
migrations.migrate(drift, migrations=[original])

edited = _fake(1, "widgets",
               ["CREATE TABLE widgets (id TEXT PRIMARY KEY, extra TEXT)"])
mismatch = None
try:
    migrations.migrate(drift, migrations=[edited])
except migrations.MigrationChecksumMismatch as exc:
    mismatch = exc

check("checksum drift: an edited applied migration raises", mismatch is not None)
check("checksum drift: the error names the version", getattr(mismatch, "version", None) == 1)
check("checksum drift: the error names the migration",
      getattr(mismatch, "name", None) == "widgets")
check("checksum drift: the error reports both checksums",
      bool(getattr(mismatch, "stored", "")) and bool(getattr(mismatch, "computed", ""))
      and mismatch.stored != mismatch.computed)
check("checksum drift: the message is actionable",
      "immutable" in str(mismatch).lower())
check("checksum drift: an identical re-run still passes",
      migrations.migrate(drift, migrations=[original]).applied == [])

# ---------------------------------------------------------------------------
# 5. Transactional rollback — a failing migration leaves NOTHING behind
# ---------------------------------------------------------------------------
rollback = _db()
good = _fake(1, "good", ["CREATE TABLE good (id TEXT PRIMARY KEY)"])
bad = _fake(2, "bad", [
    "CREATE TABLE bad_partial (id TEXT PRIMARY KEY)",
    "THIS IS NOT VALID SQL",
])

failure = None
try:
    migrations.migrate(rollback, migrations=[good, bad])
except migrations.MigrationApplyError as exc:
    failure = exc

rb_tables = _tables(rollback)
check("rollback: the failing migration raises MigrationApplyError", failure is not None)
check("rollback: the error names the failing version",
      getattr(failure, "version", None) == 2)
check("rollback: the earlier migration's table survived", "good" in rb_tables)
check("rollback: the failing migration's partial table was rolled back",
      "bad_partial" not in rb_tables)
check("rollback: no ledger row was written for the failed migration",
      rollback.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2"
                       ).fetchone()[0] == 0)
check("rollback: the successful migration stays recorded",
      rollback.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=1"
                       ).fetchone()[0] == 1)
check("rollback: version reflects only what actually applied",
      runner.current_version(rollback) == 1)
check("rollback: retrying after the fix applies the remainder",
      migrations.migrate(rollback, migrations=[
          good, _fake(2, "bad", ["CREATE TABLE fixed (id TEXT PRIMARY KEY)"])
      ]).applied == ["0002_bad"])

# ---------------------------------------------------------------------------
# 6. Ordering, duplicate detection, and a newer-than-code database
# ---------------------------------------------------------------------------
order = _db()
applied_order = migrations.migrate(order, migrations=[
    _fake(3, "third", ["CREATE TABLE t3 (id TEXT)"]),
    _fake(1, "first", ["CREATE TABLE t1 (id TEXT)"]),
    _fake(2, "second", ["CREATE TABLE t2 (id TEXT)"]),
]).applied
check("ordering: migrations apply in ascending version order",
      applied_order == ["0001_first", "0002_second", "0003_third"])

ahead = _db()
migrations.migrate(ahead, migrations=[
    _fake(1, "one", ["CREATE TABLE t1 (id TEXT)"]),
    _fake(2, "two", ["CREATE TABLE t2 (id TEXT)"]),
])
ahead_result = migrations.migrate(ahead, migrations=[
    _fake(1, "one", ["CREATE TABLE t1 (id TEXT)"]),
])
check("newer db: an applied version unknown to this build is reported",
      ahead_result.unknown_applied == [2])
check("newer db: reporting it does not destroy or re-run anything",
      ahead_result.applied == [] and "t2" in _tables(ahead))

# ---------------------------------------------------------------------------
# 7. The real migration set is well-formed
# ---------------------------------------------------------------------------
real = migrations.discover()
check("real set: migrations are discovered", len(real) >= 2)
check("real set: versions are unique and ascending",
      [m.version for m in real] == sorted({m.version for m in real}))
check("real set: every migration has a non-empty checksum",
      all(len(m.checksum) == 64 for m in real))
check("real set: v0001 is the baseline", real[0].label == "0001_baseline")

bad_results = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad_results)} passed, {len(bad_results)} failed")
sys.exit(1 if bad_results else 0)
