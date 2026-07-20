"""Document ingestion: segment content blobs, parse records, document index.

OpenMind v2 Phase 3. Additive and non-destructive: it adds two columns with
defaults and creates two new tables plus their indexes. It touches no existing
row and drops nothing, so a Phase 1 or Phase 2 database migrates to this head
with every project, path, job, Asset history row, content blob, file-index row,
vector collection, map, case, Ask exchange and template selection intact.

WHY ``upgrade()`` AND NOT ``STATEMENTS``
---------------------------------------
SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. A plain ``ADD
COLUMN`` raises "duplicate column name" on a second run, which would turn a
harmless repeat into a failed migration. Reading ``PRAGMA table_info`` first
makes the column additions idempotent.

WHAT EACH CHANGE IS FOR
-----------------------
``segments.content_blob_hash``
    Phase 2 recovers code Evidence by slicing ``[startLine, endLine]`` out of the
    revision blob — safe forever, because the byte-to-line mapping never changes.
    A DOCUMENT block cannot be recovered that way: re-deriving it needs a parser
    rerun, and a future parser version may legitimately produce different block
    boundaries, which would silently rewrite history. Document segments therefore
    snapshot their exact represented text as its own content-addressed blob and
    record its SHA-256 here. Existing code segments keep ``''`` and continue to
    resolve through the line-range path; they are deliberately NOT backfilled.

``jobs.payload_json``
    A document import must reach the worker WITHOUT an absolute machine path in
    the portable database (the zero-origin-traces constraint). The free ``path``
    column already means something different per job type, so the safe import
    payload (staged blob hash, original filename, requested asset / logical key,
    import mode, parser options) gets its own column.

``document_parses``
    A derived parse projection over an immutable Revision: which parser, at which
    version, produced which status, with which warnings and coverage. The
    presence of a row for an Asset's current Revision is the DEFINITION of "this
    Asset is a document" — a recorded fact, never an inference.

``document_index``
    Which document vector chunks are currently live for an Asset, so a new
    current Revision replaces exactly its predecessor's chunks without touching
    any other document and without a collection scan.

FROZEN once applied: the runner checksums this file's ``upgrade`` source; express
any later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Add the Phase 3 document-ingestion schema. Idempotent.

    Every statement lives INSIDE this function on purpose: the runner checksums
    ``inspect.getsource(upgrade)``, so SQL held in a module-level constant could
    be edited after the migration had been applied without the immutability
    guard noticing.
    """
    # (table, column, DDL) — added only when absent.
    columns = (
        ("segments", "content_blob_hash", "TEXT NOT NULL DEFAULT ''"),
        ("jobs", "payload_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for table, column, decl in columns:
        existing = {row[1] for row in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            # Impossible at this head (v0001 creates jobs, v0003 creates
            # segments), so fail loudly rather than silently skipping a column
            # the application will then query.
            raise sqlite3.OperationalError(
                f"v0004_document_ingestion: table {table!r} is missing; the "
                f"database is not at the expected v0003 head")
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    statements = (
        """
        CREATE TABLE IF NOT EXISTS document_parses (
            revision_id TEXT PRIMARY KEY,
            parser_name TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            unsupported_json TEXT NOT NULL DEFAULT '[]',
            coverage_json TEXT NOT NULL DEFAULT '{}',
            structure_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(revision_id) REFERENCES asset_revisions(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_document_parses_status "
        "ON document_parses(status)",
        "CREATE INDEX IF NOT EXISTS idx_document_parses_parser "
        "ON document_parses(parser_name)",
        """
        CREATE TABLE IF NOT EXISTS document_index (
            workspace_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, asset_id),
            FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_document_index_ws "
        "ON document_index(workspace_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_index_revision "
        "ON document_index(revision_id)",
        "CREATE INDEX IF NOT EXISTS idx_segments_blob "
        "ON segments(content_blob_hash)",
    )
    for statement in statements:
        conn.execute(statement)
