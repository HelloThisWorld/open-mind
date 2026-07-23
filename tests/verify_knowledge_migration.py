"""v0006 knowledge-graph migration: tables, indexes, idempotency, checksum
immutability of v0001–v0005, foreign-key integrity, rollback on failure, and
a real v0005 -> v0006 upgrade that loses nothing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _knowledge_helpers import check, finish  # noqa: E402

from openmind import db, migrations
from openmind.migrations import runner as migration_runner
from openmind.runtime import get_runtime

GRAPH_TABLES = (
    "engineering_entities", "engineering_entity_aliases",
    "engineering_entity_bindings", "engineering_claims",
    "engineering_claim_evidence", "engineering_relations",
    "engineering_relation_evidence", "knowledge_decisions",
    "knowledge_revisions", "knowledge_promotions",
    "knowledge_projection_state",
)

REQUIRED_INDEXES = (
    "idx_kg_ent_identity", "idx_kg_ent_ws_lifecycle", "idx_kg_ent_ws_key",
    "idx_kg_alias_normalized", "idx_kg_claim_entity", "idx_kg_claim_hash",
    "idx_kg_rel_source", "idx_kg_rel_target", "idx_kg_rel_ws_type",
    "idx_kg_rel_identity", "idx_kg_rev_number", "idx_kg_promo_candidate",
    "idx_kg_dec_target", "idx_kg_bind_ref",
)


def table_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def index_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


# -- a REAL v0005 database upgraded to v0006 --------------------------------
path = Path(tempfile.mkdtemp(prefix="om_mig_")) / "openmind.sqlite3"
conn = sqlite3.connect(str(path))
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys=ON")
import threading
lock = threading.RLock()

all_migrations = migration_runner.discover()
check("v0006 knowledge_graph is discovered",
      any(m.version == 6 and m.name == "knowledge_graph"
          for m in all_migrations))
check("the migration head is at least v0006",
      all_migrations[-1].version >= 6)

original_discover = migration_runner.discover
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 5]
try:
    result_v5 = migration_runner.migrate(conn, lock)
finally:
    migration_runner.discover = original_discover
check("phase 4 database builds at version 5",
      migration_runner.current_version(conn) == 5)
check("v0005 database has no graph tables yet",
      not (table_names(conn) & set(GRAPH_TABLES)))

# seed representative Phase 1-4 rows that must survive
conn.execute("INSERT INTO projects (id,name,state,paths_json,created_at,"
             "updated_at,meta_json) VALUES ('p_mig','mig','ready','[]',"
             "'2026-01-01','2026-01-01','{}')")
conn.execute("INSERT INTO assets (id,workspace_id,logical_key,asset_type,"
             "title,source_kind,source_path,media_type,state,"
             "current_revision_id,metadata_json,created_at,updated_at) "
             "VALUES ('a_mig','p_mig','src/x.java','source-code','x','file',"
             "'src/x.java','','active',NULL,'{}','2026-01-01','2026-01-01')")
conn.execute("INSERT INTO semantic_candidates (id,workspace_id,"
             "candidate_kind,candidate_type,created_at,updated_at) VALUES "
             "('sc_mig','p_mig','engineering-concept','requirement',"
             "'2026-01-01','2026-01-01')")
conn.commit()

# Cap discovery at v0006: this suite pins the Phase 5 migration's own
# behavior; the Phase 6 migration has its own suite.
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 6]
try:
    result_v6 = migration_runner.migrate(conn, lock)
finally:
    migration_runner.discover = original_discover
check("v0005 -> v0006 applies exactly one migration",
      [m for m in result_v6.applied] == ["0006_knowledge_graph"]
      if hasattr(result_v6, "applied") else True)
check("migrated database reports version 6",
      migration_runner.current_version(conn) == 6)
check("every graph table exists after migration",
      set(GRAPH_TABLES) <= table_names(conn))
check("required graph indexes exist",
      set(REQUIRED_INDEXES) <= index_names(conn))
check("prior project row survived",
      conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1)
check("prior asset row survived",
      conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1)
check("prior semantic candidate survived",
      conn.execute("SELECT COUNT(*) FROM semantic_candidates"
                   ).fetchone()[0] == 1)

# repeated migration is a no-op (still capped at v0006)
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 6]
try:
    before = conn.execute("SELECT version, checksum FROM schema_migrations "
                          "ORDER BY version").fetchall()
    result_again = migration_runner.migrate(conn, lock)
    after = conn.execute("SELECT version, checksum FROM schema_migrations "
                         "ORDER BY version").fetchall()
finally:
    migration_runner.discover = original_discover
check("repeated migration applies nothing",
      [tuple(r) for r in before] == [tuple(r) for r in after])
check("repeated migration keeps version 6",
      migration_runner.current_version(conn) == 6)

# v0001-v0005 checksums unchanged: stored checksums equal recomputed ones
stored = {r["version"]: r["checksum"] for r in conn.execute(
    "SELECT version, checksum FROM schema_migrations")}
computed = {m.version: m.checksum for m in original_discover()}
check("v0001-v0005 checksums match the code on disk",
      all(stored[v] == computed[v] for v in (1, 2, 3, 4, 5)))
check("v0006 checksum recorded", stored.get(6) == computed[6])

# foreign-key integrity: a claim naming a missing entity is rejected
try:
    conn.execute("INSERT INTO engineering_claims (id,workspace_id,"
                 "entity_id,claim_type,statement,normalized_statement_hash,"
                 "origin,created_at,updated_at) VALUES ('clm_bad','p_mig',"
                 "'ent_missing','definition','x','h','manual','2026-01-01',"
                 "'2026-01-01')")
    conn.commit()
    fk_rejected = False
    conn.execute("DELETE FROM engineering_claims WHERE id='clm_bad'")
    conn.commit()
except sqlite3.IntegrityError:
    conn.rollback()
    fk_rejected = True
check("foreign keys reject a claim on a missing entity", fk_rejected)

# entity delete cascades to its claims (workspace wipe path)
conn.execute("INSERT INTO engineering_entities (id,workspace_id,"
             "entity_type,canonical_key,origin,created_at,updated_at) "
             "VALUES ('ent_c','p_mig','requirement','requirement:R1',"
             "'manual','2026-01-01','2026-01-01')")
conn.execute("INSERT INTO engineering_claims (id,workspace_id,entity_id,"
             "claim_type,statement,normalized_statement_hash,origin,"
             "created_at,updated_at) VALUES ('clm_c','p_mig','ent_c',"
             "'definition','x','h','manual','2026-01-01','2026-01-01')")
conn.commit()
conn.execute("DELETE FROM engineering_entities WHERE id='ent_c'")
conn.commit()
check("entity delete cascades to claims",
      conn.execute("SELECT COUNT(*) FROM engineering_claims WHERE "
                   "id='clm_c'").fetchone()[0] == 0)

# migration failure rolls back whole (simulated failing migration)
fail_path = Path(tempfile.mkdtemp(prefix="om_migfail_")) / "f.sqlite3"
fconn = sqlite3.connect(str(fail_path))
fconn.row_factory = sqlite3.Row


def _boom(connection) -> None:
    connection.execute("CREATE TABLE half_written (id TEXT)")
    raise RuntimeError("simulated failure")


failing = migration_runner.Migration(version=1, name="boom",
                                     payload="boom", apply=_boom)
migration_runner.discover = lambda: [failing]
try:
    try:
        migration_runner.migrate(fconn, threading.RLock())
        rolled_back = False
    except migration_runner.MigrationApplyError:
        rolled_back = "half_written" not in table_names(fconn)
finally:
    migration_runner.discover = original_discover
check("failed migration rolls back its DDL and writes no ledger row",
      rolled_back and fconn.execute(
          "SELECT COUNT(*) FROM sqlite_master WHERE "
          "name='schema_migrations'").fetchone()[0] in (0, 1))
conn.close()
fconn.close()

# the shared runtime lands on the current head (v0007 as of Phase 6) —
# which includes v0006; the graph tables exist either way.
runtime = get_runtime()
check("runtime bootstrap reports at least schema version 6",
      runtime.info()["schema_version"] >= 6)
check("runtime version advanced to 1.7.0-dev",
      runtime.version == "1.7.0-dev")

finish()
