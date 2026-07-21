"""Semantic plane: policies, analysis runs/targets, candidates, usage, cache,
Project Lenses.

OpenMind v2 Phase 4. Purely additive and non-destructive: it creates new
tables and their indexes, touches no existing row and drops nothing, so a
Phase 1–3 database migrates to this head with every project, path, job, Asset
and Revision history row, Segment, Evidence, document parse record, document
index, vector collection, content blob, glossary/structure map, case, Ask
exchange and template selection intact.

WHAT EACH TABLE IS FOR
----------------------
``workspace_semantic_policies``
    One row per workspace: data classification (defaults RESTRICTED),
    whether remote model use is allowed (defaults FALSE — cloud reasoning is
    opt-in per workspace, never ambient), the selected provider profile NAME
    (profiles themselves are machine-local files and never enter this
    database), cache enablement, per-task model overrides and budgets.

``semantic_analysis_runs`` / ``semantic_analysis_targets``
    A run is the header (scope, provider, versions, budget, summary); a
    target is the resumable checkpoint unit — one (revision, segment, task)
    with its own input hash and status, so a restart resumes exactly the
    targets that had not finished, and a finished target is never re-billed.

``semantic_candidates`` (+ ``semantic_candidate_evidence``)
    Locally VERIFIED model proposals. Every active candidate is joined to the
    immutable Evidence rows that support it, with the exact bounded quotes
    and their hashes. ``revision_id`` denormalizes the source revision so
    staleness reconciliation is one indexed UPDATE, not a JSON scan.

``semantic_relation_candidates`` (+ ``semantic_relation_evidence``)
    Proposed relations between references (candidates, assets, revisions,
    segments, code symbols, document keys) — refs are JSON for generality,
    with the two most load-bearing dependencies (source/target candidate id)
    denormalized into indexed columns for transitive staleness.

``semantic_conflict_candidates`` (+ ``semantic_conflict_evidence``)
    Two references that APPEAR to disagree, with category and a bounded
    explanation. Category and status vocabularies are enforced by the
    application layer; nothing here is ever a confirmed conflict.

``semantic_usage``
    The per-request ledger: tokens (NULL when the provider did not report a
    number — never a false zero), latency, retries and an estimated cost that
    is NULL unless a price was actually known (``cost_source`` says whether
    it came from the provider, local configuration, or is unknown).

``semantic_cache``
    Local result cache keyed by the full composite key (provider, model,
    task+prompt+schema+analyzer versions, lens hash, ordered evidence ids and
    content hashes, options). Content-addressed and machine-local.

``project_lenses``
    Adaptive Project Lens records: source (builtin/organization/induced),
    status (provisional → validated → approved → active), the closed
    definition document, the deterministic validation report and full model
    provenance for induced lenses.

FROZEN once applied: the runner checksums this file's ``upgrade`` source;
express any later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the Phase 4 semantic-plane schema. Idempotent.

    Every statement lives INSIDE this function on purpose: the runner
    checksums ``inspect.getsource(upgrade)``, so SQL held in a module-level
    constant could be edited after the migration had been applied without the
    immutability guard noticing.
    """
    statements = (
        # -- workspace policy ------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS workspace_semantic_policies (
            workspace_id TEXT PRIMARY KEY,
            data_classification TEXT NOT NULL DEFAULT 'restricted',
            allow_remote INTEGER NOT NULL DEFAULT 0,
            provider_profile TEXT NOT NULL DEFAULT '',
            local_cache_enabled INTEGER NOT NULL DEFAULT 1,
            task_models_json TEXT NOT NULL DEFAULT '{}',
            budgets_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        # -- analysis runs ---------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_analysis_runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            job_id TEXT NOT NULL DEFAULT '',
            run_type TEXT NOT NULL DEFAULT 'analysis',
            scope_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'planned',
            provider_profile TEXT NOT NULL DEFAULT '',
            provider_kind TEXT NOT NULL DEFAULT '',
            model_tier TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            lens_id TEXT,
            task_set_json TEXT NOT NULL DEFAULT '[]',
            task_version TEXT NOT NULL DEFAULT '',
            prompt_set_version TEXT NOT NULL DEFAULT '',
            analyzer_version TEXT NOT NULL DEFAULT '',
            input_hash TEXT NOT NULL DEFAULT '',
            budget_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_runs_ws "
        "ON semantic_analysis_runs(workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_sem_runs_status "
        "ON semantic_analysis_runs(status)",
        # -- analysis targets (the resumable checkpoint unit) ----------------
        """
        CREATE TABLE IF NOT EXISTS semantic_analysis_targets (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            revision_id TEXT NOT NULL DEFAULT '',
            segment_id TEXT NOT NULL DEFAULT '',
            task_type TEXT NOT NULL,
            input_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            result_hash TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES semantic_analysis_runs(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_targets_run "
        "ON semantic_analysis_targets(run_id, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sem_targets_unit "
        "ON semantic_analysis_targets(run_id, revision_id, segment_id, task_type)",
        # -- candidates ------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_candidates (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            revision_id TEXT NOT NULL DEFAULT '',
            candidate_kind TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            stable_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            statement TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            model_confidence_hint TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            evidence_status TEXT NOT NULL DEFAULT 'rejected',
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            reviewer TEXT NOT NULL DEFAULT '',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            task_version TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            analyzer_version TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_cand_ws "
        "ON semantic_candidates(workspace_id, lifecycle_status, candidate_type)",
        "CREATE INDEX IF NOT EXISTS idx_sem_cand_run "
        "ON semantic_candidates(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_sem_cand_revision "
        "ON semantic_candidates(workspace_id, revision_id)",
        "CREATE INDEX IF NOT EXISTS idx_sem_cand_key "
        "ON semantic_candidates(workspace_id, candidate_type, stable_key)",
        """
        CREATE TABLE IF NOT EXISTS semantic_candidate_evidence (
            candidate_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supports',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(candidate_id, evidence_id, quote_hash),
            FOREIGN KEY(candidate_id) REFERENCES semantic_candidates(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_cand_ev_evidence "
        "ON semantic_candidate_evidence(evidence_id)",
        # -- relation candidates ---------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_relation_candidates (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            relation_type TEXT NOT NULL,
            source_ref_json TEXT NOT NULL DEFAULT '{}',
            target_ref_json TEXT NOT NULL DEFAULT '{}',
            source_candidate_id TEXT,
            target_candidate_id TEXT,
            reason TEXT NOT NULL DEFAULT '',
            model_confidence_hint TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            evidence_status TEXT NOT NULL DEFAULT 'rejected',
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            reviewer TEXT NOT NULL DEFAULT '',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_rel_ws "
        "ON semantic_relation_candidates(workspace_id, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_sem_rel_source "
        "ON semantic_relation_candidates(source_candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_sem_rel_target "
        "ON semantic_relation_candidates(target_candidate_id)",
        """
        CREATE TABLE IF NOT EXISTS semantic_relation_evidence (
            relation_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supports',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(relation_id, evidence_id, quote_hash),
            FOREIGN KEY(relation_id) REFERENCES semantic_relation_candidates(id)
                ON DELETE CASCADE
        )
        """,
        # -- conflict candidates ---------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_conflict_candidates (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            refs_json TEXT NOT NULL DEFAULT '[]',
            left_candidate_id TEXT,
            right_candidate_id TEXT,
            explanation TEXT NOT NULL DEFAULT '',
            model_confidence_hint TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL DEFAULT 'low',
            evidence_status TEXT NOT NULL DEFAULT 'rejected',
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            reviewer TEXT NOT NULL DEFAULT '',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_conf_ws "
        "ON semantic_conflict_candidates(workspace_id, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_sem_conf_left "
        "ON semantic_conflict_candidates(left_candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_sem_conf_right "
        "ON semantic_conflict_candidates(right_candidate_id)",
        """
        CREATE TABLE IF NOT EXISTS semantic_conflict_evidence (
            conflict_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supports',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(conflict_id, evidence_id, quote_hash),
            FOREIGN KEY(conflict_id) REFERENCES semantic_conflict_candidates(id)
                ON DELETE CASCADE
        )
        """,
        # -- usage ledger ----------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_usage (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            provider_profile TEXT NOT NULL DEFAULT '',
            provider_kind TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            task_type TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER,
            output_tokens INTEGER,
            cached_tokens INTEGER,
            estimated_cost REAL,
            currency TEXT NOT NULL DEFAULT '',
            cost_source TEXT NOT NULL DEFAULT 'unknown',
            latency_ms INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            request_hash TEXT NOT NULL DEFAULT '',
            response_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_usage_run "
        "ON semantic_usage(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_sem_usage_day "
        "ON semantic_usage(created_at)",
        # -- local semantic cache --------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS semantic_cache (
            cache_key TEXT PRIMARY KEY,
            provider_kind TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            task_type TEXT NOT NULL DEFAULT '',
            prompt_hash TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT '',
            input_hash TEXT NOT NULL DEFAULT '',
            output_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sem_cache_task "
        "ON semantic_cache(task_type, model_name)",
        # -- project lenses --------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS project_lenses (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL DEFAULT '',
            organization_key TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'provisional',
            schema_version TEXT NOT NULL DEFAULT '',
            definition_json TEXT NOT NULL DEFAULT '{}',
            validation_json TEXT NOT NULL DEFAULT '{}',
            provider_profile TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            input_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_lenses_ws "
        "ON project_lenses(workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_lenses_name "
        "ON project_lenses(workspace_id, name)",
    )
    for statement in statements:
        conn.execute(statement)
