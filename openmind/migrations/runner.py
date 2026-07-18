"""A small, strict SQLite migration runner.

WHY NOT ALEMBIC
---------------
This is one SQLite file with a handful of tables, already serialized behind a
single WAL connection and one process-level lock. Alembic would add a
dependency, a config file, an env.py and a revision graph to solve a problem we
do not have (multiple backends, branching revisions, autogenerate). What we DO
need is the part Alembic is usually not configured to enforce: a recorded
checksum per migration, so an edit to an already-applied migration is caught
loudly instead of silently diverging one developer's database from another's.
That is ~150 lines.

GUARANTEES
----------
1.  Works on a completely empty database.
2.  Works on a legacy database that already has the current tables but no
    ``schema_migrations`` table (see :func:`detect_legacy` — such a database is
    *baselined*, never recreated).
3.  Never destroys user data. Every migration in this phase is additive.
4.  Applies migrations in ascending numeric order.
5.  Each migration runs inside ONE transaction: on failure it is rolled back
    whole and no ``schema_migrations`` row is written for it.
6.  Records a SHA-256 checksum of the migration's payload.
7.  A changed checksum on an already-applied migration raises
    :class:`MigrationChecksumMismatch` naming the version, the stored checksum
    and the computed one. Migrations are immutable once applied.
8.  The caller passes the lock guarding the shared connection, so the job
    worker and request threads cannot race a migration.
9.  Idempotent: a second run applies nothing and reports the same version.
10. The resulting version is reported through the health service, ``doctor``
    and ``GET /api/health``.

TRANSACTIONS
------------
``sqlite3`` in Python only opens an implicit transaction for DML, not DDL, so a
``CREATE TABLE`` would otherwise run in autocommit and survive a rollback. The
runner therefore switches the connection to explicit-transaction mode
(``isolation_level = None``) for its own duration and issues BEGIN / COMMIT /
ROLLBACK itself, restoring the previous mode afterwards. It never uses
``executescript``, which would COMMIT before running.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import pkgutil
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_MODULE_RE = re.compile(r"^v(\d+)_([a-z0-9_]+)$")

# Tables that identify a pre-migration ("legacy") Open Mind database.
LEGACY_MARKER_TABLES = ("projects", "jobs", "file_index")


class MigrationError(RuntimeError):
    """Base class for every migration failure."""


class MigrationChecksumMismatch(MigrationError):
    """An already-applied migration's payload changed on disk."""

    def __init__(self, version: int, name: str, stored: str, computed: str) -> None:
        super().__init__(
            f"migration {version:04d}_{name} has already been applied, but its "
            f"content changed on disk (stored checksum {stored[:12]}..., "
            f"computed {computed[:12]}...). Applied migrations are immutable: "
            f"revert the edit, or add a NEW migration expressing the change."
        )
        self.version = version
        self.name = name
        self.stored = stored
        self.computed = computed


class MigrationApplyError(MigrationError):
    """A migration raised while being applied; it was rolled back."""

    def __init__(self, version: int, name: str, cause: BaseException) -> None:
        super().__init__(
            f"migration {version:04d}_{name} failed and was rolled back: {cause}"
        )
        self.version = version
        self.name = name
        self.cause = cause


@dataclass(frozen=True)
class Migration:
    """One versioned, checksummed schema change.

    A migration module declares either ``STATEMENTS`` (a sequence of SQL
    statements, executed in order) or an ``upgrade(conn)`` function. The
    checksum covers whichever of the two carries the change, so editing an
    applied migration is always detected.
    """

    version: int
    name: str
    payload: str
    apply: Callable[[sqlite3.Connection], None]

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass
class MigrationResult:
    """What a :func:`migrate` call did — the machine-readable answer that
    ``doctor`` and ``GET /api/health`` report."""

    version: int = 0
    applied: List[str] = field(default_factory=list)
    already_applied: List[str] = field(default_factory=list)
    baselined_legacy: bool = False
    unknown_applied: List[int] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "applied": list(self.applied),
            "already_applied": list(self.already_applied),
            "baselined_legacy": self.baselined_legacy,
            "unknown_applied": list(self.unknown_applied),
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _build(module: Any, version: int, name: str) -> Migration:
    statements: Optional[Sequence[str]] = getattr(module, "STATEMENTS", None)
    upgrade: Optional[Callable[[sqlite3.Connection], None]] = getattr(
        module, "upgrade", None)

    if statements is not None and upgrade is not None:
        raise MigrationError(
            f"migration {version:04d}_{name} declares both STATEMENTS and "
            f"upgrade(); it must declare exactly one")

    if statements is not None:
        sql = tuple(str(s).strip() for s in statements if str(s).strip())
        if not sql:
            raise MigrationError(
                f"migration {version:04d}_{name} has an empty STATEMENTS")

        def _apply(conn: sqlite3.Connection, _sql: Tuple[str, ...] = sql) -> None:
            for statement in _sql:
                conn.execute(statement)

        return Migration(version, name, "\n".join(sql), _apply)

    if upgrade is not None:
        try:
            payload = inspect.getsource(upgrade)
        except (OSError, TypeError) as exc:   # pragma: no cover - source always available
            raise MigrationError(
                f"cannot read the source of {version:04d}_{name}.upgrade() to "
                f"checksum it: {exc}") from exc
        return Migration(version, name, payload, upgrade)

    raise MigrationError(
        f"migration {version:04d}_{name} declares neither STATEMENTS nor upgrade()")


def discover() -> List[Migration]:
    """Every migration under ``openmind.migrations.versions``, ordered by
    version. Raises on a duplicate version — an ambiguous order is a bug, not
    something to resolve arbitrarily."""
    from . import versions as versions_pkg

    found: Dict[int, Migration] = {}
    for info in sorted(pkgutil.iter_modules(versions_pkg.__path__),
                       key=lambda i: i.name):
        match = _MODULE_RE.match(info.name)
        if not match:
            continue
        version, name = int(match.group(1)), match.group(2)
        module = importlib.import_module(f"{versions_pkg.__name__}.{info.name}")
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version].label} and {version:04d}_{name}")
        found[version] = _build(module, version, name)
    return [found[v] for v in sorted(found)]


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------
def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def detect_legacy(conn: sqlite3.Connection) -> bool:
    """True when this database predates migrations: it carries the known
    Open Mind tables but has no ``schema_migrations``.

    Such a database is *baselined* — v0001 is written to the ledger without
    recreating anything, because v0001 is expressed with ``CREATE TABLE IF NOT
    EXISTS`` and is a no-op against it. Existing project, job, file-index,
    cases and Ask data is never touched.
    """
    if _table_exists(conn, "schema_migrations"):
        return False
    return any(_table_exists(conn, t) for t in LEGACY_MARKER_TABLES)


def ensure_ledger(conn: sqlite3.Connection) -> None:
    """Create ``schema_migrations`` if absent. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_migrations(conn: sqlite3.Connection) -> Dict[int, Dict[str, str]]:
    """version -> {name, checksum, applied_at} for everything already applied."""
    if not _table_exists(conn, "schema_migrations"):
        return {}
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations"
    ).fetchall()
    out: Dict[int, Dict[str, str]] = {}
    for row in rows:
        version = int(row[0])
        out[version] = {"name": row[1], "checksum": row[2], "applied_at": row[3]}
    return out


def current_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration version, or 0 on an unmigrated database."""
    applied = applied_migrations(conn)
    return max(applied) if applied else 0


@contextmanager
def _explicit_transactions(conn: sqlite3.Connection):
    """Run with manual BEGIN/COMMIT/ROLLBACK so DDL is transactional too."""
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        yield
    finally:
        conn.isolation_level = previous


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
def migrate(conn: sqlite3.Connection,
            lock: Optional[threading.RLock] = None,
            migrations: Optional[Sequence[Migration]] = None) -> MigrationResult:
    """Bring *conn* up to the newest migration and report what happened.

    *lock* is the lock guarding the shared connection (``db._lock``); pass it so
    no other thread can use the connection mid-migration. *migrations* is an
    injection point for tests — production callers omit it and get
    :func:`discover`.
    """
    # Sorted here, not only in discover(): guarantee 4 is "deterministic numeric
    # order" for EVERY caller, including a test that injects an unordered list.
    pending = sorted(migrations if migrations is not None else discover(),
                     key=lambda m: m.version)
    if lock is None:
        return _migrate_locked(conn, pending)
    with lock:
        return _migrate_locked(conn, pending)


def _migrate_locked(conn: sqlite3.Connection,
                    migrations: Sequence[Migration]) -> MigrationResult:
    result = MigrationResult()
    result.baselined_legacy = detect_legacy(conn)

    with _explicit_transactions(conn):
        ensure_ledger(conn)
        applied = applied_migrations(conn)

        known = {m.version for m in migrations}
        result.unknown_applied = sorted(v for v in applied if v not in known)

        for migration in migrations:
            record = applied.get(migration.version)
            if record is not None:
                # Immutability check BEFORE anything else: a changed payload
                # means the database and the code disagree about what version
                # N even is, so continuing would silently diverge schemas.
                if record["checksum"] != migration.checksum:
                    raise MigrationChecksumMismatch(
                        migration.version, migration.name,
                        record["checksum"], migration.checksum)
                result.already_applied.append(migration.label)
                continue

            conn.execute("BEGIN")
            try:
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version,name,checksum,applied_at)"
                    " VALUES (?,?,?,?)",
                    (migration.version, migration.name, migration.checksum,
                     time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())),
                )
            except Exception as exc:
                conn.execute("ROLLBACK")
                raise MigrationApplyError(
                    migration.version, migration.name, exc) from exc
            conn.execute("COMMIT")
            result.applied.append(migration.label)

        result.version = current_version(conn)

    return result
