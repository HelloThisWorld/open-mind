"""Canonical Engineering Knowledge Graph: Entities, Claims, Relations,
aliases, bindings, Evidence joins, Human Decisions, Knowledge Revisions,
promotion records and deterministic projection state.

OpenMind v2 Phase 5. Purely additive and non-destructive: it creates new
tables and their indexes, touches no existing row and drops nothing, so a
Phase 1–4 database migrates to this head with every project, job, Asset,
Revision, Segment, Evidence, content blob, document parse record, document
index, semantic policy, analysis run, target, candidate (all three kinds),
usage record, cache entry, Project Lens, template selection, Ask exchange,
case and map intact.

WHAT EACH TABLE IS FOR
----------------------
``engineering_entities``
    One canonical engineering concept per row. The logical identity is
    ``UNIQUE(workspace_id, entity_type, canonical_key)`` — the generated id
    (``ent_*``) is storage identity only. ``promoted_from_candidate_id`` is
    PROVENANCE (no foreign key on purpose: the graph must never depend on a
    Phase 4 candidate row's survival, and a candidate delete must never
    cascade into canonical history).

``engineering_entity_aliases``
    Alternative names, normalized for exact lookup. A normalized collision
    across ACTIVE entities is reported by the application layer, never
    silently attached; removed aliases keep their row for audit.

``engineering_entity_bindings``
    Entity -> source-plane anchors (asset / revision / segment / evidence /
    document-block / code-symbol / configuration-key / database-object /
    api-operation / message-topic). ``ref_id`` is workspace-validated at the
    application layer; a binding to a non-current Revision goes ``stale``.

``engineering_claims`` (+ ``engineering_claim_evidence``)
    Bounded statements about one Entity. ``normalized_statement_hash``
    deduplicates identical claims; corrections create a NEW claim and set
    ``superseded_by_claim_id`` on the old one. Every ACTIVE claim has at
    least one evidence join (application-enforced at every write path).

``engineering_relations`` (+ ``engineering_relation_evidence``)
    Typed edges between two entities of the same workspace, with a relation
    state (explicit / inferred / confirmed / rejected / stale / superseded)
    and provenance. Active identity is (workspace, source, target, type) —
    enforced by the application inside the graph transaction, not by a
    partial unique index, because superseded/rejected history rows share the
    same tuple.

``knowledge_decisions``
    One immutable audit row per governance write, with caller-supplied actor
    (never inferred), bounded note and bounded before/after snapshots.

``knowledge_revisions``
    The per-workspace monotonic ledger. ``UNIQUE(workspace_id,
    revision_number)`` is the concurrency backstop: the allocator increments
    inside the same transaction as the graph writes it stamps.

``knowledge_promotions``
    Candidate -> canonical promotion records. Only promotions that HAPPENED
    are stored (a blocked attempt persists nothing).

``knowledge_projection_state``
    Per-workspace deterministic projection watermark: projector version,
    source-knowledge hash, last written revision — what makes ``graph sync``
    an honest no-op when nothing changed.

FOREIGN KEYS
------------
Graph-internal foreign keys (aliases/bindings/claims -> entities, evidence
joins -> claims/relations, relations -> entities) exist so a workspace-level
entity wipe cascades cleanly. There is deliberately NO foreign key to
``semantic_candidates`` (provenance, not ownership) and none to ``evidence``
(graph history must outlive a terminated asset model; integrity is enforced
at write time by the application, and staleness reconciliation handles the
rest).

FROZEN once applied: the runner checksums this file's ``upgrade`` source;
express any later schema change as a NEW migration, never an edit here.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the Phase 5 knowledge-graph schema. Idempotent.

    Every statement lives INSIDE this function on purpose: the runner
    checksums ``inspect.getsource(upgrade)``, so SQL held in a module-level
    constant could be edited after the migration had been applied without the
    immutability guard noticing.
    """
    statements = (
        # -- entities --------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_entities (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            authority_status TEXT NOT NULL DEFAULT 'unknown',
            origin TEXT NOT NULL,
            promoted_from_candidate_id TEXT NOT NULL DEFAULT '',
            created_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            updated_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT,
            superseded_by_entity_id TEXT,
            merged_into_entity_id TEXT
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_ent_identity "
        "ON engineering_entities(workspace_id, entity_type, canonical_key)",
        "CREATE INDEX IF NOT EXISTS idx_kg_ent_ws_lifecycle "
        "ON engineering_entities(workspace_id, entity_type, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_ent_ws_key "
        "ON engineering_entities(workspace_id, canonical_key)",
        "CREATE INDEX IF NOT EXISTS idx_kg_ent_promoted "
        "ON engineering_entities(promoted_from_candidate_id)",
        # -- aliases ---------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_entity_aliases (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            alias_type TEXT NOT NULL DEFAULT 'name',
            origin TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'active',
            evidence_id TEXT NOT NULL DEFAULT '',
            created_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(entity_id) REFERENCES engineering_entities(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_alias_normalized "
        "ON engineering_entity_aliases(workspace_id, normalized_alias, status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_alias_entity "
        "ON engineering_entity_aliases(entity_id, status)",
        # -- bindings --------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_entity_bindings (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            ref_kind TEXT NOT NULL,
            ref_id TEXT NOT NULL DEFAULT '',
            ref_key TEXT NOT NULL DEFAULT '',
            binding_role TEXT NOT NULL DEFAULT 'supporting',
            status TEXT NOT NULL DEFAULT 'active',
            origin TEXT NOT NULL DEFAULT 'manual',
            evidence_id TEXT NOT NULL DEFAULT '',
            created_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            updated_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT,
            FOREIGN KEY(entity_id) REFERENCES engineering_entities(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_bind_entity "
        "ON engineering_entity_bindings(entity_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_bind_ref "
        "ON engineering_entity_bindings(workspace_id, ref_kind, ref_id)",
        "CREATE INDEX IF NOT EXISTS idx_kg_bind_ws_status "
        "ON engineering_entity_bindings(workspace_id, status)",
        # -- claims ----------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_claims (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            statement TEXT NOT NULL,
            normalized_statement_hash TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            authority_status TEXT NOT NULL DEFAULT 'unknown',
            origin TEXT NOT NULL,
            promoted_from_candidate_id TEXT NOT NULL DEFAULT '',
            created_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            updated_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT,
            superseded_by_claim_id TEXT,
            FOREIGN KEY(entity_id) REFERENCES engineering_entities(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_claim_entity "
        "ON engineering_claims(entity_id, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_claim_ws "
        "ON engineering_claims(workspace_id, lifecycle_status, claim_type)",
        "CREATE INDEX IF NOT EXISTS idx_kg_claim_hash "
        "ON engineering_claims(entity_id, normalized_statement_hash)",
        "CREATE INDEX IF NOT EXISTS idx_kg_claim_promoted "
        "ON engineering_claims(promoted_from_candidate_id)",
        """
        CREATE TABLE IF NOT EXISTS engineering_claim_evidence (
            claim_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'primary',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(claim_id, evidence_id, quote_hash),
            FOREIGN KEY(claim_id) REFERENCES engineering_claims(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_claim_ev_evidence "
        "ON engineering_claim_evidence(evidence_id)",
        # -- relations -------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS engineering_relations (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            relation_state TEXT NOT NULL DEFAULT 'inferred',
            confidence TEXT NOT NULL DEFAULT 'low',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            authority_status TEXT NOT NULL DEFAULT 'unknown',
            origin TEXT NOT NULL,
            promoted_from_relation_candidate_id TEXT NOT NULL DEFAULT '',
            created_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            updated_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stale_at TEXT,
            superseded_by_relation_id TEXT,
            FOREIGN KEY(source_entity_id) REFERENCES engineering_entities(id)
                ON DELETE CASCADE,
            FOREIGN KEY(target_entity_id) REFERENCES engineering_entities(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_source "
        "ON engineering_relations(source_entity_id, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_target "
        "ON engineering_relations(target_entity_id, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_ws_type "
        "ON engineering_relations(workspace_id, relation_type, "
        "relation_state, lifecycle_status)",
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_identity "
        "ON engineering_relations(workspace_id, source_entity_id, "
        "target_entity_id, relation_type)",
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_promoted "
        "ON engineering_relations(promoted_from_relation_candidate_id)",
        """
        CREATE TABLE IF NOT EXISTS engineering_relation_evidence (
            relation_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'primary',
            quote TEXT NOT NULL DEFAULT '',
            quote_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(relation_id, evidence_id, quote_hash),
            FOREIGN KEY(relation_id) REFERENCES engineering_relations(id)
                ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_rel_ev_evidence "
        "ON engineering_relation_evidence(evidence_id)",
        # -- human decisions -------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS knowledge_decisions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            knowledge_revision_id TEXT NOT NULL DEFAULT '',
            decision_type TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            source_command TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_dec_ws "
        "ON knowledge_decisions(workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_kg_dec_target "
        "ON knowledge_decisions(workspace_id, target_kind, target_id)",
        "CREATE INDEX IF NOT EXISTS idx_kg_dec_revision "
        "ON knowledge_decisions(knowledge_revision_id)",
        # -- knowledge revisions ---------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS knowledge_revisions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            parent_revision_number INTEGER NOT NULL DEFAULT 0,
            change_set_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            object_counts_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_rev_number "
        "ON knowledge_revisions(workspace_id, revision_number)",
        "CREATE INDEX IF NOT EXISTS idx_kg_rev_ws "
        "ON knowledge_revisions(workspace_id, created_at)",
        # -- promotions ------------------------------------------------------
        """
        CREATE TABLE IF NOT EXISTS knowledge_promotions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            candidate_kind TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            target_kind TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            policy_version TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            knowledge_revision_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_kg_promo_candidate "
        "ON knowledge_promotions(workspace_id, candidate_kind, candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_kg_promo_target "
        "ON knowledge_promotions(workspace_id, target_kind, target_id)",
        # -- deterministic projection state ----------------------------------
        """
        CREATE TABLE IF NOT EXISTS knowledge_projection_state (
            workspace_id TEXT PRIMARY KEY,
            projector_version TEXT NOT NULL DEFAULT '',
            source_knowledge_hash TEXT NOT NULL DEFAULT '',
            last_knowledge_revision INTEGER NOT NULL DEFAULT 0,
            last_synced_at TEXT
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)
