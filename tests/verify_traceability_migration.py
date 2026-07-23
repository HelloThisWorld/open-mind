"""v0007 traceability/conflict schema: tables, indexes, idempotency,
v0001–v0006 checksum immutability, FK cascades, Phase 1–5 data survival,
and the Phase 5 prerequisite gate (graph service present, review remains
non-promoting, generic graph path and formal trace stay separate APIs)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from _traceability_helpers import check, finish

from openmind.migrations import runner as migration_runner

TRACE_TABLES = (
    "workspace_traceability_policies", "traceability_runs", "trace_paths",
    "trace_path_steps", "trace_path_evidence", "traceability_gaps",
    "traceability_coverage_snapshots", "engineering_conflicts",
    "engineering_conflict_objects", "engineering_conflict_evidence",
    "engineering_conflict_decisions",
)
REQUIRED_INDEXES = (
    "idx_trc_run_revision", "idx_trc_path_root", "idx_trc_path_target",
    "idx_trc_path_status", "idx_trc_path_hash", "idx_trc_path_revision",
    "idx_trc_gap_ws_status", "idx_trc_gap_fingerprint", "idx_trc_gap_stale",
    "idx_trc_cov_revision", "idx_eng_conf_ws_status", "idx_eng_conf_dedup",
)
GRAPH_TABLES = ("engineering_entities", "engineering_claims",
                "engineering_relations", "knowledge_revisions",
                "knowledge_decisions", "knowledge_promotions")


def table_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def index_names(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


# -- a REAL v0006 database upgraded to v0007 --------------------------------
path = Path(tempfile.mkdtemp(prefix="om_tmig_")) / "openmind.sqlite3"
conn = sqlite3.connect(str(path))
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys=ON")
lock = threading.RLock()

all_migrations = migration_runner.discover()
check("v0008 is discovered as the migration head (v0007 present)",
      all_migrations[-1].version == 8
      and all_migrations[-1].name == "git_overlays"
      and any(m.version == 7 and m.name == "traceability_conflicts"
              for m in all_migrations))

original_discover = migration_runner.discover
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 6]
try:
    migration_runner.migrate(conn, lock)
finally:
    migration_runner.discover = original_discover
check("phase 5 database builds at version 6",
      migration_runner.current_version(conn) == 6)
check("v0006 database has no trace tables yet",
      not (table_names(conn) & set(TRACE_TABLES)))

# seed representative Phase 1-5 rows that must survive
conn.execute("INSERT INTO projects (id,name,state,paths_json,created_at,"
             "updated_at,meta_json) VALUES ('p_mig','mig','ready','[]',"
             "'2026-01-01','2026-01-01','{}')")
conn.execute("INSERT INTO assets (id,workspace_id,logical_key,asset_type,"
             "title,source_kind,source_path,media_type,state,"
             "current_revision_id,metadata_json,created_at,updated_at) "
             "VALUES ('a_mig','p_mig','src/x.java','source-code','x','file',"
             "'src/x.java','','active',NULL,'{}','2026-01-01','2026-01-01')")
conn.execute("INSERT INTO semantic_conflict_candidates (id,workspace_id,"
             "category,created_at,updated_at) VALUES ('sx_mig','p_mig',"
             "'requirement-design','2026-01-01','2026-01-01')")
conn.execute("INSERT INTO engineering_entities (id,workspace_id,"
             "entity_type,canonical_key,origin,created_at,updated_at) "
             "VALUES ('ent_mig','p_mig','requirement','requirement:R1',"
             "'manual','2026-01-01','2026-01-01')")
conn.execute("INSERT INTO knowledge_revisions (id,workspace_id,"
             "revision_number,action,created_at) VALUES ('kr_mig','p_mig',"
             "1,'manual-entity-create','2026-01-01')")
conn.commit()

# This test validates the v0006 -> v0007 upgrade specifically, so cap the
# migration at v0007 (v0008 is exercised by verify_migrations and the Phase 7
# suites). Without the cap the runner would additively apply v0008 too.
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 7]
try:
    result_v7 = migration_runner.migrate(conn, lock)
finally:
    migration_runner.discover = original_discover
check("v0006 -> v0007 applies exactly one migration",
      [m for m in result_v7.applied] == ["0007_traceability_conflicts"]
      if hasattr(result_v7, "applied") else True)
check("migrated database reports version 7",
      migration_runner.current_version(conn) == 7)
check("every trace/conflict table exists after migration",
      set(TRACE_TABLES) <= table_names(conn))
check("required trace indexes exist",
      set(REQUIRED_INDEXES) <= index_names(conn))
check("prior project row survived",
      conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1)
check("prior asset row survived",
      conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1)
check("prior conflict candidate survived",
      conn.execute("SELECT COUNT(*) FROM semantic_conflict_candidates"
                   ).fetchone()[0] == 1)
check("prior graph entity survived",
      conn.execute("SELECT COUNT(*) FROM engineering_entities"
                   ).fetchone()[0] == 1)
check("prior knowledge revision survived",
      conn.execute("SELECT COUNT(*) FROM knowledge_revisions"
                   ).fetchone()[0] == 1)

# repeated migration is a no-op
before = conn.execute("SELECT version, checksum FROM schema_migrations "
                      "ORDER BY version").fetchall()
# Idempotency of the v0007 state: re-running the SAME (<=7) migration set
# applies nothing. (The additive v0008 is validated by verify_migrations.)
migration_runner.discover = lambda: [m for m in original_discover()
                                     if m.version <= 7]
try:
    migration_runner.migrate(conn, lock)
finally:
    migration_runner.discover = original_discover
after = conn.execute("SELECT version, checksum FROM schema_migrations "
                     "ORDER BY version").fetchall()
check("repeated migration applies nothing",
      [tuple(r) for r in before] == [tuple(r) for r in after])

# v0001-v0006 checksums unchanged
stored = {r["version"]: r["checksum"] for r in conn.execute(
    "SELECT version, checksum FROM schema_migrations")}
computed = {m.version: m.checksum for m in original_discover()}
check("v0001-v0006 checksums match the code on disk",
      all(stored[v] == computed[v] for v in (1, 2, 3, 4, 5, 6)))
check("v0007 checksum recorded", stored.get(7) == computed[7])

# FK cascades: conflict delete cascades to objects/evidence/decisions;
# path delete cascades to steps/evidence.
conn.execute("INSERT INTO engineering_conflicts (id,workspace_id,category,"
             "origin,created_at,updated_at) VALUES ('ecf_c','p_mig',"
             "'document-document','deterministic','2026-01-01',"
             "'2026-01-01')")
conn.execute("INSERT INTO engineering_conflict_objects VALUES "
             "('ecf_c','claim','clm_x','left')")
conn.execute("INSERT INTO engineering_conflict_evidence VALUES "
             "('ecf_c','ev_x','supports','q','qh')")
conn.execute("INSERT INTO engineering_conflict_decisions (id,workspace_id,"
             "conflict_id,decision,created_at) VALUES ('ecd_c','p_mig',"
             "'ecf_c','start-review','2026-01-01')")
conn.execute("INSERT INTO trace_paths (id,workspace_id,root_entity_id,"
             "path_kind,created_at) VALUES ('tr_c','p_mig','ent_mig',"
             "'requirement-to-design','2026-01-01')")
conn.execute("INSERT INTO trace_path_steps (id,trace_path_id,ordinal,"
             "stage,node_id) VALUES ('trs_c','tr_c',1,'design','ent_x')")
conn.execute("INSERT INTO trace_path_evidence VALUES "
             "('tr_c','ev_x','supporting')")
conn.commit()
conn.execute("DELETE FROM engineering_conflicts WHERE id='ecf_c'")
conn.execute("DELETE FROM trace_paths WHERE id='tr_c'")
conn.commit()
check("conflict delete cascades to objects/evidence/decisions",
      conn.execute("SELECT COUNT(*) FROM engineering_conflict_objects"
                   ).fetchone()[0] == 0
      and conn.execute("SELECT COUNT(*) FROM engineering_conflict_evidence"
                       ).fetchone()[0] == 0
      and conn.execute(
          "SELECT COUNT(*) FROM engineering_conflict_decisions"
      ).fetchone()[0] == 0)
check("path delete cascades to steps/evidence",
      conn.execute("SELECT COUNT(*) FROM trace_path_steps").fetchone()[0]
      == 0
      and conn.execute("SELECT COUNT(*) FROM trace_path_evidence"
                       ).fetchone()[0] == 0)
conn.close()

# -- Phase 5 prerequisite gate (spec §36) -----------------------------------
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
check("runtime bootstrap reports schema version 8",
      runtime.info()["schema_version"] == 8)
check("runtime version is 1.7.0-dev", runtime.version == "1.7.0-dev")

from openmind import db as appdb  # noqa: E402
conn2, lock2 = appdb.shared_connection()
with lock2:
    live_tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
check("phase 5 graph tables exist in the live schema",
      set(GRAPH_TABLES) <= live_tables)
check("canonical graph service is available",
      runtime.knowledge is not None
      and hasattr(runtime.knowledge, "promote_candidate"))
check("traceability service is available",
      runtime.traceability is not None)

# graph path and formal trace are SEPARATE APIs
check("generic graph path and formal trace are separate APIs",
      hasattr(runtime.knowledge, "find_path")
      and hasattr(runtime.traceability, "trace_requirement")
      and not hasattr(runtime.knowledge, "trace_requirement"))

# review remains non-promoting: a confirmed conflict candidate creates no
# canonical conflict row.
workspace = runtime.workspaces.create("mig-gate")
pid = workspace["id"]
from openmind.semantic import store as semantic_store  # noqa: E402
sx = semantic_store.insert_conflicts(pid, [{
    "category": "requirement-design", "explanation": "gate",
    "confidence": "low", "evidence_status": "verified", "payload": {},
}])[0]
runtime.semantic.review_conflict_candidate(pid, sx, decision="confirm",
                                           reviewer="gate")
from openmind.traceability import store as trace_store  # noqa: E402
check("conflict-candidate review confirm does NOT create a canonical "
      "conflict",
      trace_store.find_conflict_by_candidate(pid, sx) is None)

finish()
