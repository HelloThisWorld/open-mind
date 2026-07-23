"""Git repositories, canonical Git baselines and the isolated Git Overlay
plane (OpenMind v2 Phase 7).

Purely additive and non-destructive: it creates new tables and their indexes,
touches no existing row and drops nothing, so a Phase 1–7 database migrates to
this head with every Asset, Revision, Segment, Evidence, document, semantic
candidate, Lens, Entity, Claim, Relation, Knowledge Revision, Human Decision,
trace path, gap, coverage snapshot and canonical Conflict intact.

ISOLATION IS STRUCTURAL
-----------------------
Every table here is an OVERLAY table. Overlay rows may REFERENCE canonical
objects by id (``base_entity_id``, ``base_relation_id``, ``base_trace_path_id``,
``base_conflict_id``, …) but there is deliberately NO foreign key from an
overlay row into a canonical table — an overlay is a provisional projection and
must never be able to cascade a delete, or a write, into the canonical Base
Workspace. Foreign keys exist only WITHIN the overlay graph
(``git_overlay_*`` -> ``git_overlays``) so that deleting one overlay cleanly
removes only its own data.

Absolute repository paths are NEVER stored here (spec §9): a repository is
identified by a portable, workspace-relative ``repository_key`` and its
absolute root is resolved at run time from machine-local configuration.

FROZEN once applied: the runner checksums this file's ``upgrade`` source;
express any later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the Phase 7 Git + Overlay schema. Idempotent."""
    statements = (
        # -- git repositories (portable identity only) -----------------------
        """
        CREATE TABLE IF NOT EXISTS git_repositories (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            repository_key TEXT NOT NULL,
            relative_root TEXT NOT NULL DEFAULT '',
            object_format TEXT NOT NULL DEFAULT 'sha1',
            is_bare INTEGER NOT NULL DEFAULT 0,
            default_branch TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_git_repo_ws_key "
        "ON git_repositories(workspace_id, repository_key)",
        "CREATE INDEX IF NOT EXISTS idx_git_repo_ws "
        "ON git_repositories(workspace_id)",
        # -- canonical git baselines ----------------------------------------
        """
        CREATE TABLE IF NOT EXISTS workspace_git_baselines (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            tree_sha TEXT NOT NULL DEFAULT '',
            branch_name TEXT NOT NULL DEFAULT '',
            head_ref TEXT NOT NULL DEFAULT '',
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            traceability_run_id TEXT NOT NULL DEFAULT '',
            trace_policy_checksum TEXT NOT NULL DEFAULT '',
            graph_projector_version TEXT NOT NULL DEFAULT '',
            trace_engine_version TEXT NOT NULL DEFAULT '',
            asset_state_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_baseline_ws_repo "
        "ON workspace_git_baselines(workspace_id, repository_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_git_baseline_commit "
        "ON workspace_git_baselines(workspace_id, repository_id, commit_sha)",
        # -- overlays --------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlays (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            overlay_kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'planned',
            base_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            base_traceability_run_id TEXT NOT NULL DEFAULT '',
            base_policy_checksum TEXT NOT NULL DEFAULT '',
            overlay_revision INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ready_at TEXT,
            stale_at TEXT,
            closed_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_overlay_ws_state "
        "ON git_overlays(workspace_id, state, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_git_overlay_source_hash "
        "ON git_overlays(workspace_id, source_hash)",
        "CREATE INDEX IF NOT EXISTS idx_git_overlay_revision "
        "ON git_overlays(id, overlay_revision)",
        # -- overlay repositories (one per repo in the change set) -----------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_repositories (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            base_ref TEXT NOT NULL DEFAULT '',
            head_ref TEXT NOT NULL DEFAULT '',
            base_commit TEXT NOT NULL DEFAULT '',
            head_commit TEXT NOT NULL DEFAULT '',
            merge_base_commit TEXT NOT NULL DEFAULT '',
            base_tree TEXT NOT NULL DEFAULT '',
            head_tree TEXT NOT NULL DEFAULT '',
            branch_name TEXT NOT NULL DEFAULT '',
            target_branch TEXT NOT NULL DEFAULT '',
            worktree_hash TEXT NOT NULL DEFAULT '',
            dirty_state_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_repo_overlay "
        "ON git_overlay_repositories(overlay_id)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_repo_repo "
        "ON git_overlay_repositories(repository_id)",
        # -- overlay files ---------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_files (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            overlay_repository_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_path TEXT NOT NULL DEFAULT '',
            new_path TEXT NOT NULL DEFAULT '',
            old_mode TEXT NOT NULL DEFAULT '',
            new_mode TEXT NOT NULL DEFAULT '',
            old_blob_sha TEXT NOT NULL DEFAULT '',
            new_blob_sha TEXT NOT NULL DEFAULT '',
            old_content_blob_hash TEXT NOT NULL DEFAULT '',
            new_content_blob_hash TEXT NOT NULL DEFAULT '',
            is_binary INTEGER NOT NULL DEFAULT 0,
            is_symlink INTEGER NOT NULL DEFAULT 0,
            is_submodule INTEGER NOT NULL DEFAULT 0,
            is_lfs_pointer INTEGER NOT NULL DEFAULT 0,
            similarity INTEGER NOT NULL DEFAULT 0,
            additions INTEGER NOT NULL DEFAULT 0,
            deletions INTEGER NOT NULL DEFAULT 0,
            changed_ranges_json TEXT NOT NULL DEFAULT '{}',
            layer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ok',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_file_overlay "
        "ON git_overlay_files(overlay_id, change_type)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_file_newpath "
        "ON git_overlay_files(overlay_id, new_path)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_file_oldpath "
        "ON git_overlay_files(overlay_id, old_path)",
        # -- overlay segments ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_segments (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            overlay_file_id TEXT NOT NULL,
            side TEXT NOT NULL,
            segment_key TEXT NOT NULL DEFAULT '',
            segment_type TEXT NOT NULL DEFAULT '',
            change_class TEXT NOT NULL DEFAULT 'unchanged',
            ordinal INTEGER NOT NULL DEFAULT 0,
            start_line INTEGER NOT NULL DEFAULT 0,
            end_line INTEGER NOT NULL DEFAULT 0,
            symbol TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            content_blob_hash TEXT NOT NULL DEFAULT '',
            content_mode TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_seg_file "
        "ON git_overlay_segments(overlay_file_id, side, ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_seg_overlay "
        "ON git_overlay_segments(overlay_id, side)",
        # -- overlay evidence ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_evidence (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            overlay_file_id TEXT NOT NULL DEFAULT '',
            segment_id TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL DEFAULT 'after',
            locator_json TEXT NOT NULL DEFAULT '{}',
            excerpt TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_ev_overlay "
        "ON git_overlay_evidence(overlay_id, side)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_ev_file "
        "ON git_overlay_evidence(overlay_file_id)",
        # -- overlay entity deltas ------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_entity_deltas (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            delta_type TEXT NOT NULL,
            base_entity_id TEXT NOT NULL DEFAULT '',
            canonical_key TEXT NOT NULL DEFAULT '',
            entity_type TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_ent_overlay "
        "ON git_overlay_entity_deltas(overlay_id, delta_type)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_ent_base "
        "ON git_overlay_entity_deltas(base_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_ent_key "
        "ON git_overlay_entity_deltas(overlay_id, canonical_key)",
        # -- overlay relation deltas ----------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_relation_deltas (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            delta_type TEXT NOT NULL,
            base_relation_id TEXT NOT NULL DEFAULT '',
            source_ref_json TEXT NOT NULL DEFAULT '{}',
            target_ref_json TEXT NOT NULL DEFAULT '{}',
            relation_type TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_rel_overlay "
        "ON git_overlay_relation_deltas(overlay_id, delta_type)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_rel_base "
        "ON git_overlay_relation_deltas(base_relation_id)",
        # -- overlay trace impacts ------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_trace_impacts (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            root_requirement_id TEXT NOT NULL DEFAULT '',
            impact_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            introduced_gaps_json TEXT NOT NULL DEFAULT '[]',
            resolved_gaps_json TEXT NOT NULL DEFAULT '[]',
            affected_trace_ids_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_trace_overlay "
        "ON git_overlay_trace_impacts(overlay_id, impact_type)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_trace_req "
        "ON git_overlay_trace_impacts(overlay_id, root_requirement_id)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_trace_sev "
        "ON git_overlay_trace_impacts(overlay_id, severity)",
        # -- overlay conflict impacts ---------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_conflict_impacts (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            subject_key TEXT NOT NULL DEFAULT '',
            impact_type TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'info',
            base_conflict_id TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_conf_overlay "
        "ON git_overlay_conflict_impacts(overlay_id, impact_type)",
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_conf_category "
        "ON git_overlay_conflict_impacts(overlay_id, category)",
        # -- overlay reports -------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_reports (
            id TEXT PRIMARY KEY,
            overlay_id TEXT NOT NULL,
            overlay_revision INTEGER NOT NULL DEFAULT 0,
            report_schema_version TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL DEFAULT '{}',
            report_hash TEXT NOT NULL DEFAULT '',
            markdown_blob_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(overlay_id) REFERENCES git_overlays(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_git_ovl_report_overlay "
        "ON git_overlay_reports(overlay_id, overlay_revision)",
        # -- overlay search index bookkeeping -------------------------------
        """
        CREATE TABLE IF NOT EXISTS git_overlay_search_index (
            overlay_id TEXT NOT NULL,
            plane TEXT NOT NULL,
            chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(overlay_id, plane)
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)
