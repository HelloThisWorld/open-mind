"""The canonical Asset model: assets, revisions, segments, evidence.

OpenMind v2 Phase 2. Additive and non-destructive: it only CREATEs new tables
and indexes (all ``IF NOT EXISTS``), and touches no existing row. A legacy or
Phase 1 database migrates to this head with every project, path, job, file-index
row, Ask exchange, case, template selection, map and Chroma collection intact.

The canonical content identity model:

    assets            one logical engineering object (a source/config file)
      asset_revisions   an immutable observation of that asset's bytes
        segments          a stable structural unit inside one revision
        evidence          a source-locatable citation for a segment

``assets.workspace_id`` references ``projects(id)`` and the whole subtree
cascades on ``ON DELETE CASCADE``, so removing a project (with the connection's
``PRAGMA foreign_keys=ON``) drops its assets, revisions, segments and evidence
in one statement.

Two identity choices worth stating explicitly:

* ``UNIQUE(asset_id, sequence)`` — revision sequence is dense and unique per
  asset.
* ``(asset_id, content_hash)`` is DELIBERATELY NOT UNIQUE — a source may move
  A -> B and back to A, and that revert must be representable as a new revision
  that happens to reuse the old content blob.

FROZEN once applied: the runner checksums this file's ``STATEMENTS``; express any
later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

STATEMENTS = (
    # -- assets --------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        logical_key TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        title TEXT NOT NULL,
        source_kind TEXT NOT NULL DEFAULT 'file',
        source_path TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'active',
        current_revision_id TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(workspace_id, logical_key),
        FOREIGN KEY(workspace_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_ws_state ON assets(workspace_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_assets_ws_type ON assets(workspace_id, asset_type)",

    # -- asset_revisions -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS asset_revisions (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        content_size INTEGER NOT NULL,
        content_blob_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unknown',
        version_label TEXT NOT NULL DEFAULT '',
        source_commit TEXT NOT NULL DEFAULT '',
        supersedes_revision_id TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(asset_id, sequence),
        FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_revisions_asset_seq "
    "ON asset_revisions(asset_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_blob "
    "ON asset_revisions(content_blob_hash)",

    # -- segments ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS segments (
        id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        segment_key TEXT NOT NULL,
        segment_type TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        start_line INTEGER,
        end_line INTEGER,
        symbol TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL,
        content_mode TEXT NOT NULL DEFAULT 'verbatim',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(revision_id, segment_key),
        FOREIGN KEY(revision_id) REFERENCES asset_revisions(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_segments_revision ON segments(revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_segments_symbol ON segments(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_segments_rev_type "
    "ON segments(revision_id, segment_type)",

    # -- evidence ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        segment_id TEXT,
        locator_json TEXT NOT NULL,
        excerpt TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(revision_id) REFERENCES asset_revisions(id) ON DELETE CASCADE,
        FOREIGN KEY(segment_id) REFERENCES segments(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_revision ON evidence(revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_segment ON evidence(segment_id)",
)
