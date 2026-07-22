"""Formal Requirement Traceability and governed Conflict management:
workspace policy selection, traceability runs, trace paths and steps, trace
evidence joins, gaps, coverage snapshots, canonical Conflicts with object /
Evidence / decision joins.

OpenMind v2 Phase 6. Purely additive and non-destructive: it creates new
tables and their indexes, touches no existing row and drops nothing, so a
Phase 1–5 database migrates to this head with every Asset, Revision,
Segment, Evidence, document, semantic candidate (all three kinds), Lens,
Entity, Claim, Relation, alias, binding, Knowledge Revision, Human
Decision, promotion and projection-state row intact.

WHAT EACH TABLE IS FOR
----------------------
``workspace_traceability_policies``
    One active Traceability Policy per workspace (PRIMARY KEY workspace_id).
    Stores the resolved policy's name, source and checksum — the DEFINITION
    stays in code (built-in) or the organization file; a selection is a
    pointer plus the checksum that pins what was selected.

``traceability_runs``
    One row per trace refresh. Derived-analysis bookkeeping: records the
    Knowledge Revision, policy checksum and engine version analyzed —
    a run itself never mints a Knowledge Revision.

``trace_paths`` / ``trace_path_steps`` / ``trace_path_evidence``
    Persisted formal trace paths. Steps reference canonical entities and
    relations BY ID and never duplicate their semantic content. ``stale_at``
    NULL means current; historical paths keep their rows forever.

``traceability_gaps``
    First-class missing links. ``detection_fingerprint`` ties a gap to the
    exact blocking objects so governance status (accepted / dismissed)
    survives refreshes while the facts are unchanged, and stops applying
    when they change.

``traceability_coverage_snapshots``
    One immutable metrics document per completed refresh. Never
    overwritten; history stays queryable.

``engineering_conflicts`` (+ objects / evidence / decisions)
    Canonical governed Conflicts — deterministic detections, promoted
    Phase 4 Conflict Candidates, or manual records. ``dedup_key`` is the
    deterministic identity (workspace + category + subject + objects +
    property + detector); ``suppression_fingerprint`` (in the decisions
    row's resolution_json and mirrored in conflict metadata) keeps a
    dismissed conflict from being recreated unchanged. Conflict writes are
    graph-governance writes: they run inside the Phase 5 graph transaction
    and mint Knowledge Revisions — which is why these tables carry
    ``knowledge_revision`` stamps but no ledger of their own.

FOREIGN KEYS
------------
Trace/conflict-internal foreign keys (steps -> paths, evidence joins,
objects/decisions -> conflicts) exist so a delete cascades cleanly. There
is deliberately NO foreign key into the canonical graph or the source plane
(paths reference entities/relations by id; integrity is enforced at write
time and staleness reconciliation handles the rest — trace history must
never block or cascade a canonical governance action).

FROZEN once applied: the runner checksums this file's ``upgrade`` source;
express any later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the Phase 6 traceability/conflict schema. Idempotent."""
    statements = (
        # -- workspace policy selection --------------------------------------
        """
        CREATE TABLE IF NOT EXISTS workspace_traceability_policies (
            workspace_id TEXT PRIMARY KEY,
            policy_name TEXT NOT NULL,
            policy_source TEXT NOT NULL,
            policy_checksum TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        # -- runs ------------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS traceability_runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            policy_name TEXT NOT NULL DEFAULT '',
            policy_checksum TEXT NOT NULL DEFAULT '',
            engine_version TEXT NOT NULL DEFAULT '',
            scope_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'planned',
            summary_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_run_ws "
        "ON traceability_runs(workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_trc_run_ws_status "
        "ON traceability_runs(workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_trc_run_revision "
        "ON traceability_runs(workspace_id, knowledge_revision, "
        "policy_checksum)",
        # -- trace paths -----------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS trace_paths (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            root_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL DEFAULT '',
            path_kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'partial',
            completeness REAL NOT NULL DEFAULT 0.0,
            confidence TEXT NOT NULL DEFAULT 'low',
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            policy_checksum TEXT NOT NULL DEFAULT '',
            path_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            stale_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_path_root "
        "ON trace_paths(workspace_id, root_entity_id, stale_at)",
        "CREATE INDEX IF NOT EXISTS idx_trc_path_target "
        "ON trace_paths(workspace_id, target_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_trc_path_status "
        "ON trace_paths(workspace_id, status, stale_at)",
        "CREATE INDEX IF NOT EXISTS idx_trc_path_run "
        "ON trace_paths(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_trc_path_hash "
        "ON trace_paths(workspace_id, path_hash)",
        "CREATE INDEX IF NOT EXISTS idx_trc_path_revision "
        "ON trace_paths(workspace_id, knowledge_revision, policy_checksum)",
        # -- trace path steps ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS trace_path_steps (
            id TEXT PRIMARY KEY,
            trace_path_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            stage TEXT NOT NULL,
            node_kind TEXT NOT NULL DEFAULT 'entity',
            node_id TEXT NOT NULL,
            relation_id TEXT NOT NULL DEFAULT '',
            relation_type TEXT NOT NULL DEFAULT '',
            relation_state TEXT NOT NULL DEFAULT '',
            evidence_status TEXT NOT NULL DEFAULT '',
            authority_status TEXT NOT NULL DEFAULT 'unknown',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(trace_path_id) REFERENCES trace_paths(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_step_path "
        "ON trace_path_steps(trace_path_id, ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_trc_step_node "
        "ON trace_path_steps(node_id)",
        # -- trace path evidence ---------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS trace_path_evidence (
            trace_path_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supporting',
            PRIMARY KEY(trace_path_id, evidence_id, role),
            FOREIGN KEY(trace_path_id) REFERENCES trace_paths(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_path_ev "
        "ON trace_path_evidence(evidence_id)",
        # -- gaps ------------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS traceability_gaps (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            root_entity_id TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            gap_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'low',
            status TEXT NOT NULL DEFAULT 'open',
            reason TEXT NOT NULL DEFAULT '',
            blocking_object_json TEXT NOT NULL DEFAULT '{}',
            detection_fingerprint TEXT NOT NULL DEFAULT '',
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            policy_checksum TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_decision_id TEXT NOT NULL DEFAULT '',
            stale_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_gap_ws_status "
        "ON traceability_gaps(workspace_id, gap_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_trc_gap_root "
        "ON traceability_gaps(workspace_id, root_entity_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_trc_gap_fingerprint "
        "ON traceability_gaps(workspace_id, detection_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_trc_gap_run "
        "ON traceability_gaps(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_trc_gap_stale "
        "ON traceability_gaps(workspace_id, stale_at)",
        # -- coverage snapshots ----------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS traceability_coverage_snapshots (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            policy_name TEXT NOT NULL DEFAULT '',
            policy_checksum TEXT NOT NULL DEFAULT '',
            engine_version TEXT NOT NULL DEFAULT '',
            scope_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            stale_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_trc_cov_ws "
        "ON traceability_coverage_snapshots(workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_trc_cov_revision "
        "ON traceability_coverage_snapshots(workspace_id, "
        "knowledge_revision, policy_checksum)",
        # -- canonical conflicts ---------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_conflicts (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            category TEXT NOT NULL,
            subject_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'open',
            origin TEXT NOT NULL,
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            detector_name TEXT NOT NULL DEFAULT '',
            detector_version TEXT NOT NULL DEFAULT '',
            promoted_from_conflict_candidate_id TEXT NOT NULL DEFAULT '',
            dedup_key TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            stale_at TEXT,
            superseded_by_conflict_id TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_ws_status "
        "ON engineering_conflicts(workspace_id, status, category)",
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_dedup "
        "ON engineering_conflicts(workspace_id, dedup_key)",
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_subject "
        "ON engineering_conflicts(workspace_id, subject_key)",
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_promoted "
        "ON engineering_conflicts(promoted_from_conflict_candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_revision "
        "ON engineering_conflicts(workspace_id, knowledge_revision)",
        # -- conflict objects ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_conflict_objects (
            conflict_id TEXT NOT NULL,
            object_kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'subject',
            PRIMARY KEY(conflict_id, object_kind, object_id, role),
            FOREIGN KEY(conflict_id) REFERENCES engineering_conflicts(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_obj "
        "ON engineering_conflict_objects(object_id)",
        # -- conflict evidence -----------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_conflict_evidence (
            conflict_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supports',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(conflict_id, evidence_id, quote_hash),
            FOREIGN KEY(conflict_id) REFERENCES engineering_conflicts(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_ev "
        "ON engineering_conflict_evidence(evidence_id)",
        # -- conflict decisions ----------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_conflict_decisions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            conflict_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            before_status TEXT NOT NULL DEFAULT '',
            after_status TEXT NOT NULL DEFAULT '',
            resolution_json TEXT NOT NULL DEFAULT '{}',
            knowledge_revision INTEGER NOT NULL DEFAULT 0,
            knowledge_decision_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(conflict_id) REFERENCES engineering_conflicts(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_eng_conf_dec "
        "ON engineering_conflict_decisions(workspace_id, conflict_id, "
        "created_at)",
    )
    for statement in statements:
        conn.execute(statement)
