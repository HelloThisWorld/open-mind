"""Repository over the v0007 traceability/conflict tables.

Same placement rationale as :mod:`openmind.knowledge.store`: db.py is the
Phase 1–3 store, semantic/store.py the Phase 4 one, knowledge/store.py the
Phase 5 one, and this module the Phase 6 one — all on the SAME shared WAL
connection and the SAME RLock (``db.shared_connection()``).

TWO WRITE DISCIPLINES
---------------------
*Derived-analysis writes* (runs, trace paths, steps, gaps, coverage
snapshots) commit directly — they are recomputable projections stamped with
the Knowledge Revision they analyzed and must NOT mint revisions.

*Governance writes* (conflicts, conflict decisions, gap governance
decisions, policy selection decisions) run inside the Phase 5
``graph_transaction`` — the caller passes the open transaction, this module
executes the conflict-table SQL through ``tx.execute_counted`` so the write
is counted, atomically committed with its ``knowledge_decisions`` row, and
stamped with exactly one Knowledge Revision.

Workspace scoping is structural: every row carries a validated
``workspace_id`` and every read filters on it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from .. import db


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _cx():
    return db.shared_connection()


def _j(value: Any) -> str:
    return json.dumps(value if value is not None else {})


def _load(text: Any, default: Any) -> Any:
    try:
        out = json.loads(text or "")
    except Exception:
        return default
    return out if out is not None else default


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------
def _policy_row(row) -> Dict[str, Any]:
    return {
        "workspace_id": row["workspace_id"],
        "policy_name": row["policy_name"],
        "policy_source": row["policy_source"],
        "policy_checksum": row["policy_checksum"],
        "options": _load(row["options_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _run_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "knowledge_revision": row["knowledge_revision"],
        "policy_name": row["policy_name"],
        "policy_checksum": row["policy_checksum"],
        "engine_version": row["engine_version"],
        "scope": _load(row["scope_json"], {}),
        "status": row["status"],
        "summary": _load(row["summary_json"], {}),
        "error": row["error"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _path_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "root_entity_id": row["root_entity_id"],
        "target_entity_id": row["target_entity_id"],
        "path_kind": row["path_kind"], "status": row["status"],
        "completeness": row["completeness"],
        "confidence": row["confidence"],
        "knowledge_revision": row["knowledge_revision"],
        "policy_checksum": row["policy_checksum"],
        "path_hash": row["path_hash"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "stale_at": row["stale_at"],
    }


def _step_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "trace_path_id": row["trace_path_id"],
        "ordinal": row["ordinal"], "stage": row["stage"],
        "node_kind": row["node_kind"], "node_id": row["node_id"],
        "relation_id": row["relation_id"],
        "relation_type": row["relation_type"],
        "relation_state": row["relation_state"],
        "evidence_status": row["evidence_status"],
        "authority_status": row["authority_status"],
        "metadata": _load(row["metadata_json"], {}),
    }


def _gap_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "root_entity_id": row["root_entity_id"],
        "stage": row["stage"], "gap_type": row["gap_type"],
        "severity": row["severity"], "status": row["status"],
        "reason": row["reason"],
        "blocking_object": _load(row["blocking_object_json"], {}),
        "detection_fingerprint": row["detection_fingerprint"],
        "knowledge_revision": row["knowledge_revision"],
        "policy_checksum": row["policy_checksum"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "resolved_at": row["resolved_at"],
        "resolution_decision_id": row["resolution_decision_id"],
        "stale_at": row["stale_at"],
    }


def _snapshot_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "knowledge_revision": row["knowledge_revision"],
        "policy_name": row["policy_name"],
        "policy_checksum": row["policy_checksum"],
        "engine_version": row["engine_version"],
        "scope": _load(row["scope_json"], {}),
        "metrics": _load(row["metrics_json"], {}),
        "stale_at": row["stale_at"],
        "created_at": row["created_at"],
    }


def _conflict_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "category": row["category"], "subject_key": row["subject_key"],
        "title": row["title"], "description": row["description"],
        "severity": row["severity"], "status": row["status"],
        "origin": row["origin"],
        "knowledge_revision": row["knowledge_revision"],
        "detector_name": row["detector_name"],
        "detector_version": row["detector_version"],
        "promoted_from_conflict_candidate_id":
            row["promoted_from_conflict_candidate_id"],
        "dedup_key": row["dedup_key"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "resolved_at": row["resolved_at"], "stale_at": row["stale_at"],
        "superseded_by_conflict_id": row["superseded_by_conflict_id"],
    }


def _conflict_decision_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "conflict_id": row["conflict_id"], "decision": row["decision"],
        "actor": row["actor"], "note": row["note"],
        "before_status": row["before_status"],
        "after_status": row["after_status"],
        "resolution": _load(row["resolution_json"], {}),
        "knowledge_revision": row["knowledge_revision"],
        "knowledge_decision_id": row["knowledge_decision_id"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Workspace policy selection
# ---------------------------------------------------------------------------
def get_workspace_policy(workspace_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM workspace_traceability_policies WHERE "
            "workspace_id=?", (workspace_id,)).fetchone()
    return _policy_row(row) if row else None


def set_workspace_policy_tx(tx, *, policy_name: str, policy_source: str,
                            policy_checksum: str,
                            options: Optional[Dict[str, Any]] = None) -> None:
    """Upsert the selection INSIDE a graph transaction (policy change is a
    governance write; see the module docstring)."""
    ts = _now()
    tx.execute_counted(
        "INSERT INTO workspace_traceability_policies (workspace_id,"
        "policy_name,policy_source,policy_checksum,options_json,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(workspace_id) DO UPDATE SET "
        "policy_name=excluded.policy_name, "
        "policy_source=excluded.policy_source, "
        "policy_checksum=excluded.policy_checksum, "
        "options_json=excluded.options_json, "
        "updated_at=excluded.updated_at",
        (tx.workspace_id, policy_name, policy_source, policy_checksum,
         _j(options or {}), ts, ts), "trace_policies")


# ---------------------------------------------------------------------------
# Runs (derived analysis — direct commits)
# ---------------------------------------------------------------------------
def create_run(workspace_id: str, *, knowledge_revision: int,
               policy_name: str, policy_checksum: str, engine_version: str,
               scope: Optional[Dict[str, Any]] = None,
               status: str = "planned") -> Dict[str, Any]:
    run_id = db.new_id("trun_")
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO traceability_runs (id,workspace_id,"
            "knowledge_revision,policy_name,policy_checksum,engine_version,"
            "scope_json,status,summary_json,error,created_at,started_at,"
            "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, workspace_id, int(knowledge_revision), policy_name,
             policy_checksum, engine_version, _j(scope or {}), status,
             "{}", "", ts, None, None))
        conn.commit()
    return get_run(workspace_id, run_id)


def update_run(workspace_id: str, run_id: str, **fields: Any) -> None:
    sets, args = [], []
    for key, value in fields.items():
        if key in ("scope", "summary"):
            key, value = f"{key}_json", _j(value)
        sets.append(f"{key}=?")
        args.append(value)
    if not sets:
        return
    args.extend([run_id, workspace_id])
    conn, lock = _cx()
    with lock:
        conn.execute(
            f"UPDATE traceability_runs SET {', '.join(sets)} "
            f"WHERE id=? AND workspace_id=?", args)
        conn.commit()


def get_run(workspace_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM traceability_runs WHERE id=? AND workspace_id=?",
            (run_id, workspace_id)).fetchone()
    return _run_row(row) if row else None


def list_runs(workspace_id: str, *, status: Optional[str] = None,
              limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM traceability_runs WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_run_row(r) for r in rows]


def latest_completed_run(workspace_id: str) -> Optional[Dict[str, Any]]:
    """The newest run that finished DONE or PARTIAL — the no-op reference."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM traceability_runs WHERE workspace_id=? AND "
            "status IN ('done','partial') ORDER BY created_at DESC, id DESC "
            "LIMIT 1", (workspace_id,)).fetchone()
    return _run_row(row) if row else None


# ---------------------------------------------------------------------------
# Trace paths (derived analysis — direct commits)
# ---------------------------------------------------------------------------
def insert_paths(workspace_id: str,
                 paths: Sequence[Dict[str, Any]]) -> List[str]:
    """Batch-insert paths with their steps and evidence joins. Each entry:
    the path fields plus ``steps`` and ``evidence`` lists."""
    ts = _now()
    ids: List[str] = []
    conn, lock = _cx()
    with lock:
        try:
            for path in paths:
                path_id = path.get("id") or db.new_id("tr_")
                conn.execute(
                    "INSERT INTO trace_paths (id,workspace_id,run_id,"
                    "root_entity_id,target_entity_id,path_kind,status,"
                    "completeness,confidence,knowledge_revision,"
                    "policy_checksum,path_hash,metadata_json,created_at,"
                    "stale_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path_id, workspace_id, path.get("run_id", ""),
                     path["root_entity_id"],
                     path.get("target_entity_id", ""),
                     path["path_kind"], path.get("status", "partial"),
                     float(path.get("completeness", 0.0)),
                     path.get("confidence", "low"),
                     int(path.get("knowledge_revision", 0)),
                     path.get("policy_checksum", ""),
                     path.get("path_hash", ""),
                     _j(path.get("metadata") or {}), ts, None))
                for step in path.get("steps") or []:
                    conn.execute(
                        "INSERT INTO trace_path_steps (id,trace_path_id,"
                        "ordinal,stage,node_kind,node_id,relation_id,"
                        "relation_type,relation_state,evidence_status,"
                        "authority_status,metadata_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (db.new_id("trs_"), path_id,
                         int(step["ordinal"]), step["stage"],
                         step.get("node_kind", "entity"), step["node_id"],
                         step.get("relation_id", ""),
                         step.get("relation_type", ""),
                         step.get("relation_state", ""),
                         step.get("evidence_status", ""),
                         step.get("authority_status", "unknown"),
                         _j(step.get("metadata") or {})))
                for ev in path.get("evidence") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO trace_path_evidence "
                        "(trace_path_id,evidence_id,role) VALUES (?,?,?)",
                        (path_id, ev["evidence_id"],
                         ev.get("role", "supporting")))
                ids.append(path_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def get_path(workspace_id: str, path_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM trace_paths WHERE id=? AND workspace_id=?",
            (path_id, workspace_id)).fetchone()
        if not row:
            return None
        steps = conn.execute(
            "SELECT * FROM trace_path_steps WHERE trace_path_id=? "
            "ORDER BY ordinal", (path_id,)).fetchall()
        evidence = conn.execute(
            "SELECT * FROM trace_path_evidence WHERE trace_path_id=? "
            "ORDER BY evidence_id, role", (path_id,)).fetchall()
    out = _path_row(row)
    out["steps"] = [_step_row(s) for s in steps]
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"]}
                       for e in evidence]
    return out


def list_paths(workspace_id: str, *, root_entity_id: Optional[str] = None,
               target_entity_id: Optional[str] = None,
               path_kind: Optional[str] = None,
               status: Optional[str] = None,
               current_only: bool = True,
               run_id: Optional[str] = None,
               limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM trace_paths WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("root_entity_id", root_entity_id),
                     ("target_entity_id", target_entity_id),
                     ("path_kind", path_kind), ("status", status),
                     ("run_id", run_id)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    if current_only:
        q += " AND stale_at IS NULL"
    q += " ORDER BY root_entity_id, path_kind, id LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_path_row(r) for r in rows]


def path_steps(path_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM trace_path_steps WHERE trace_path_id=? "
            "ORDER BY ordinal", (path_id,)).fetchall()
    return [_step_row(r) for r in rows]


def path_evidence(path_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM trace_path_evidence WHERE trace_path_id=? "
            "ORDER BY evidence_id, role", (path_id,)).fetchall()
    return [{"evidence_id": e["evidence_id"], "role": e["role"]}
            for e in rows]


def stale_paths_for_roots(workspace_id: str,
                          root_entity_ids: Sequence[str]) -> int:
    """Mark every CURRENT path of these roots stale. Returns affected."""
    if not root_entity_ids:
        return 0
    ts = _now()
    total = 0
    conn, lock = _cx()
    with lock:
        for chunk_start in range(0, len(root_entity_ids), 400):
            chunk = list(root_entity_ids)[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"UPDATE trace_paths SET stale_at=?, status='stale' "
                f"WHERE workspace_id=? AND stale_at IS NULL AND "
                f"root_entity_id IN ({marks})",
                [ts, workspace_id, *chunk])
            total += max(0, cur.rowcount)
        conn.commit()
    return total


def stale_all_paths(workspace_id: str) -> int:
    ts = _now()
    conn, lock = _cx()
    with lock:
        cur = conn.execute(
            "UPDATE trace_paths SET stale_at=?, status='stale' "
            "WHERE workspace_id=? AND stale_at IS NULL", (ts, workspace_id))
        conn.commit()
    return max(0, cur.rowcount)


def restamp_paths_run(workspace_id: str, root_entity_ids: Sequence[str],
                      run_id: str, knowledge_revision: int) -> int:
    """Revalidation stamp for UNAFFECTED roots on an incremental refresh:
    their current paths join the new run without recomputation."""
    if not root_entity_ids:
        return 0
    total = 0
    conn, lock = _cx()
    with lock:
        for chunk_start in range(0, len(root_entity_ids), 400):
            chunk = list(root_entity_ids)[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"UPDATE trace_paths SET run_id=?, knowledge_revision=? "
                f"WHERE workspace_id=? AND stale_at IS NULL AND "
                f"root_entity_id IN ({marks})",
                [run_id, int(knowledge_revision), workspace_id, *chunk])
            total += max(0, cur.rowcount)
        conn.commit()
    return total


def paths_with_unusable_relations(workspace_id: str) -> List[Dict[str, Any]]:
    """CURRENT paths having at least one step whose relation is no longer
    active-graph (stale / superseded / withdrawn / rejected) — the trace
    staleness frontier, one indexed query. A step whose relation row is
    MISSING entirely is reported with reason ``broken``."""
    conn, lock = _cx()
    with lock:
        stale_rows = conn.execute(
            "SELECT DISTINCT p.id AS path_id FROM trace_paths p "
            "JOIN trace_path_steps s ON s.trace_path_id = p.id "
            "JOIN engineering_relations r ON r.id = s.relation_id "
            "WHERE p.workspace_id=? AND p.stale_at IS NULL AND "
            "s.relation_id != '' AND "
            "(r.lifecycle_status != 'active' OR "
            "r.relation_state IN ('rejected','stale','superseded'))",
            (workspace_id,)).fetchall()
        broken_rows = conn.execute(
            "SELECT DISTINCT p.id AS path_id FROM trace_paths p "
            "JOIN trace_path_steps s ON s.trace_path_id = p.id "
            "LEFT JOIN engineering_relations r ON r.id = s.relation_id "
            "WHERE p.workspace_id=? AND p.stale_at IS NULL AND "
            "s.relation_id != '' AND r.id IS NULL",
            (workspace_id,)).fetchall()
        missing_entity_rows = conn.execute(
            "SELECT DISTINCT p.id AS path_id FROM trace_paths p "
            "JOIN trace_path_steps s ON s.trace_path_id = p.id "
            "LEFT JOIN engineering_entities e ON e.id = s.node_id "
            "WHERE p.workspace_id=? AND p.stale_at IS NULL AND "
            "s.node_kind='entity' AND e.id IS NULL",
            (workspace_id,)).fetchall()
    out: Dict[str, str] = {}
    for row in stale_rows:
        out[row["path_id"]] = "stale"
    for row in broken_rows:
        out[row["path_id"]] = "broken"
    for row in missing_entity_rows:
        out[row["path_id"]] = "broken"
    return [{"path_id": path_id, "reason": reason}
            for path_id, reason in sorted(out.items())]


def mark_paths(workspace_id: str, path_ids: Sequence[str],
               *, status: str, stale: bool = True) -> int:
    if not path_ids:
        return 0
    ts = _now()
    total = 0
    conn, lock = _cx()
    with lock:
        for chunk_start in range(0, len(path_ids), 400):
            chunk = list(path_ids)[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            if stale:
                cur = conn.execute(
                    f"UPDATE trace_paths SET status=?, stale_at=? "
                    f"WHERE workspace_id=? AND id IN ({marks})",
                    [status, ts, workspace_id, *chunk])
            else:
                cur = conn.execute(
                    f"UPDATE trace_paths SET status=? "
                    f"WHERE workspace_id=? AND id IN ({marks})",
                    [status, workspace_id, *chunk])
            total += max(0, cur.rowcount)
        conn.commit()
    return total


def changed_entity_ids_since(workspace_id: str,
                             revision: int) -> List[str]:
    """Entity ids touched after *revision*: their own row, a claim of
    theirs, or a relation endpoint. The affected-root seed set, three
    indexed reads."""
    conn, lock = _cx()
    with lock:
        entities = conn.execute(
            "SELECT id FROM engineering_entities WHERE workspace_id=? AND "
            "updated_knowledge_revision > ?",
            (workspace_id, int(revision))).fetchall()
        claims = conn.execute(
            "SELECT DISTINCT entity_id FROM engineering_claims WHERE "
            "workspace_id=? AND updated_knowledge_revision > ?",
            (workspace_id, int(revision))).fetchall()
        relations = conn.execute(
            "SELECT source_entity_id, target_entity_id FROM "
            "engineering_relations WHERE workspace_id=? AND "
            "updated_knowledge_revision > ?",
            (workspace_id, int(revision))).fetchall()
    seeds: set = {r[0] for r in entities}
    seeds.update(r[0] for r in claims)
    for row in relations:
        seeds.add(row[0])
        seeds.add(row[1])
    return sorted(seeds)


# ---------------------------------------------------------------------------
# Gaps (detection = derived analysis; governance = graph transaction)
# ---------------------------------------------------------------------------
def insert_gap(workspace_id: str, gap: Dict[str, Any]) -> str:
    gap_id = gap.get("id") or db.new_id("tg_")
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO traceability_gaps (id,workspace_id,run_id,"
            "root_entity_id,stage,gap_type,severity,status,reason,"
            "blocking_object_json,detection_fingerprint,knowledge_revision,"
            "policy_checksum,metadata_json,created_at,updated_at,"
            "resolved_at,resolution_decision_id,stale_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gap_id, workspace_id, gap.get("run_id", ""),
             gap.get("root_entity_id", ""), gap.get("stage", ""),
             gap["gap_type"], gap.get("severity", "low"),
             gap.get("status", "open"), gap.get("reason", ""),
             _j(gap.get("blocking_object") or {}),
             gap.get("detection_fingerprint", ""),
             int(gap.get("knowledge_revision", 0)),
             gap.get("policy_checksum", ""),
             _j(gap.get("metadata") or {}), ts, ts, None, "", None))
        conn.commit()
    return gap_id


def update_gap(workspace_id: str, gap_id: str, **fields: Any) -> None:
    sets, args = [], []
    for key, value in fields.items():
        if key in ("blocking_object", "metadata"):
            key, value = f"{key}_json", _j(value)
        sets.append(f"{key}=?")
        args.append(value)
    sets.append("updated_at=?")
    args.append(_now())
    args.extend([gap_id, workspace_id])
    conn, lock = _cx()
    with lock:
        conn.execute(
            f"UPDATE traceability_gaps SET {', '.join(sets)} "
            f"WHERE id=? AND workspace_id=?", args)
        conn.commit()


def update_gap_tx(tx, gap_id: str, **fields: Any) -> None:
    """Gap governance update INSIDE a graph transaction."""
    sets, args = [], []
    for key, value in fields.items():
        if key in ("blocking_object", "metadata"):
            key, value = f"{key}_json", _j(value)
        sets.append(f"{key}=?")
        args.append(value)
    sets.append("updated_at=?")
    args.append(_now())
    args.extend([gap_id, tx.workspace_id])
    tx.execute_counted(
        f"UPDATE traceability_gaps SET {', '.join(sets)} "
        f"WHERE id=? AND workspace_id=?", args, "trace_gaps")


def get_gap(workspace_id: str, gap_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM traceability_gaps WHERE id=? AND workspace_id=?",
            (gap_id, workspace_id)).fetchone()
    return _gap_row(row) if row else None


def find_gap_by_fingerprint(workspace_id: str,
                            fingerprint: str) -> Optional[Dict[str, Any]]:
    """The newest gap row carrying this detection fingerprint."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM traceability_gaps WHERE workspace_id=? AND "
            "detection_fingerprint=? ORDER BY created_at DESC, id DESC "
            "LIMIT 1", (workspace_id, fingerprint)).fetchone()
    return _gap_row(row) if row else None


def list_gaps(workspace_id: str, *, gap_type: Optional[str] = None,
              status: Optional[str] = None,
              root_entity_id: Optional[str] = None,
              severity: Optional[str] = None,
              current_only: bool = True,
              run_id: Optional[str] = None,
              limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM traceability_gaps WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("gap_type", gap_type), ("status", status),
                     ("root_entity_id", root_entity_id),
                     ("severity", severity), ("run_id", run_id)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    if current_only:
        q += " AND stale_at IS NULL"
    q += (" ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' "
          "THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
          "gap_type, root_entity_id, id LIMIT ? OFFSET ?")
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_gap_row(r) for r in rows]


def count_gaps(workspace_id: str, *, status: Optional[str] = None,
               current_only: bool = True) -> int:
    q = "SELECT COUNT(*) FROM traceability_gaps WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    if current_only:
        q += " AND stale_at IS NULL"
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def stale_gaps_for_roots(workspace_id: str,
                         root_entity_ids: Sequence[str]) -> int:
    """Mark current gaps of these roots stale ahead of their rebuild.
    Governance-decided gaps (accepted/dismissed) are NOT staled — their
    status must survive a rebuild; the detector reconciles them by
    fingerprint instead."""
    if not root_entity_ids:
        return 0
    ts = _now()
    total = 0
    conn, lock = _cx()
    with lock:
        for chunk_start in range(0, len(root_entity_ids), 400):
            chunk = list(root_entity_ids)[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"UPDATE traceability_gaps SET stale_at=? "
                f"WHERE workspace_id=? AND stale_at IS NULL AND "
                f"status IN ('open','resolved') AND "
                f"root_entity_id IN ({marks})",
                [ts, workspace_id, *chunk])
            total += max(0, cur.rowcount)
        conn.commit()
    return total


def restamp_gaps_run(workspace_id: str, root_entity_ids: Sequence[str],
                     run_id: str, knowledge_revision: int) -> int:
    if not root_entity_ids:
        return 0
    total = 0
    conn, lock = _cx()
    with lock:
        for chunk_start in range(0, len(root_entity_ids), 400):
            chunk = list(root_entity_ids)[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = conn.execute(
                f"UPDATE traceability_gaps SET run_id=?, "
                f"knowledge_revision=? WHERE workspace_id=? AND "
                f"stale_at IS NULL AND root_entity_id IN ({marks})",
                [run_id, int(knowledge_revision), workspace_id, *chunk])
            total += max(0, cur.rowcount)
        conn.commit()
    return total


# ---------------------------------------------------------------------------
# Coverage snapshots (derived analysis — direct commits, never overwritten)
# ---------------------------------------------------------------------------
def insert_snapshot(workspace_id: str, *, run_id: str,
                    knowledge_revision: int, policy_name: str,
                    policy_checksum: str, engine_version: str,
                    scope: Optional[Dict[str, Any]] = None,
                    metrics: Optional[Dict[str, Any]] = None) -> str:
    snapshot_id = db.new_id("tcov_")
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO traceability_coverage_snapshots (id,workspace_id,"
            "run_id,knowledge_revision,policy_name,policy_checksum,"
            "engine_version,scope_json,metrics_json,stale_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, workspace_id, run_id, int(knowledge_revision),
             policy_name, policy_checksum, engine_version, _j(scope or {}),
             _j(metrics or {}), None, _now()))
        conn.commit()
    return snapshot_id


def stale_current_snapshots(workspace_id: str) -> int:
    """Mark existing current snapshots stale (a policy/engine change or a
    fresh completed refresh supersedes them). Rows are kept forever."""
    ts = _now()
    conn, lock = _cx()
    with lock:
        cur = conn.execute(
            "UPDATE traceability_coverage_snapshots SET stale_at=? "
            "WHERE workspace_id=? AND stale_at IS NULL", (ts, workspace_id))
        conn.commit()
    return max(0, cur.rowcount)


def latest_snapshot(workspace_id: str, *, current_only: bool = True
                    ) -> Optional[Dict[str, Any]]:
    q = ("SELECT * FROM traceability_coverage_snapshots WHERE "
         "workspace_id=?")
    args: List[Any] = [workspace_id]
    if current_only:
        q += " AND stale_at IS NULL"
    q += " ORDER BY created_at DESC, id DESC LIMIT 1"
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return _snapshot_row(row) if row else None


def list_snapshots(workspace_id: str, *, limit: int = 50,
                   offset: int = 0) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM traceability_coverage_snapshots WHERE "
            "workspace_id=? ORDER BY created_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (workspace_id, max(0, int(limit)),
             max(0, int(offset)))).fetchall()
    return [_snapshot_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Conflicts (governance writes — INSIDE graph transactions)
# ---------------------------------------------------------------------------
def insert_conflict_tx(tx, conflict: Dict[str, Any]) -> str:
    """Insert one canonical conflict with its object and evidence joins,
    inside the caller's graph transaction."""
    conflict_id = conflict.get("id") or db.new_id("ecf_")
    ts = _now()
    tx.execute_counted(
        "INSERT INTO engineering_conflicts (id,workspace_id,category,"
        "subject_key,title,description,severity,status,origin,"
        "knowledge_revision,detector_name,detector_version,"
        "promoted_from_conflict_candidate_id,dedup_key,metadata_json,"
        "created_at,updated_at,resolved_at,stale_at,"
        "superseded_by_conflict_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conflict_id, tx.workspace_id, conflict["category"],
         conflict.get("subject_key", ""), conflict.get("title", ""),
         conflict.get("description", ""),
         conflict.get("severity", "medium"),
         conflict.get("status", "open"), conflict["origin"],
         tx.revision_number, conflict.get("detector_name", ""),
         conflict.get("detector_version", ""),
         conflict.get("promoted_from_conflict_candidate_id", ""),
         conflict.get("dedup_key", ""), _j(conflict.get("metadata") or {}),
         ts, ts, None, None, None), "conflicts")
    for obj in conflict.get("objects") or []:
        tx.execute_counted(
            "INSERT OR IGNORE INTO engineering_conflict_objects "
            "(conflict_id,object_kind,object_id,role) VALUES (?,?,?,?)",
            (conflict_id, obj["object_kind"], obj["object_id"],
             obj.get("role", "subject")), "conflict_objects")
    for ev in conflict.get("evidence") or []:
        tx.execute_counted(
            "INSERT OR IGNORE INTO engineering_conflict_evidence "
            "(conflict_id,evidence_id,role,quote,quote_hash) "
            "VALUES (?,?,?,?,?)",
            (conflict_id, ev["evidence_id"], ev.get("role", "supports"),
             ev.get("quote", ""), ev.get("quote_hash", "")),
            "conflict_evidence")
    return conflict_id


def update_conflict_tx(tx, conflict_id: str, **fields: Any) -> None:
    sets, args = [], []
    for key, value in fields.items():
        if key == "metadata":
            key, value = "metadata_json", _j(value)
        sets.append(f"{key}=?")
        args.append(value)
    sets.append("updated_at=?")
    args.append(_now())
    sets.append("knowledge_revision=?")
    args.append(tx.revision_number)
    args.extend([conflict_id, tx.workspace_id])
    tx.execute_counted(
        f"UPDATE engineering_conflicts SET {', '.join(sets)} "
        f"WHERE id=? AND workspace_id=?", args, "conflicts")


def insert_conflict_decision_tx(tx, *, conflict_id: str, decision: str,
                                actor: str, note: str, before_status: str,
                                after_status: str,
                                resolution: Optional[Dict[str, Any]] = None,
                                knowledge_decision_id: str = "") -> str:
    decision_id = db.new_id("ecd_")
    tx.execute_counted(
        "INSERT INTO engineering_conflict_decisions (id,workspace_id,"
        "conflict_id,decision,actor,note,before_status,after_status,"
        "resolution_json,knowledge_revision,knowledge_decision_id,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, tx.workspace_id, conflict_id, decision, actor, note,
         before_status, after_status, _j(resolution or {}),
         tx.revision_number, knowledge_decision_id, _now()),
        "conflict_decisions")
    return decision_id


def touch_conflict_observation(workspace_id: str, conflict_id: str,
                               knowledge_revision: int) -> None:
    """Record that an unchanged conflict was observed again. Deliberately
    OUTSIDE any graph transaction: observing the identical conflict is not
    a graph change and must not mint a Knowledge Revision (spec §24)."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT metadata_json FROM engineering_conflicts WHERE id=? AND "
            "workspace_id=?", (conflict_id, workspace_id)).fetchone()
        if not row:
            return
        metadata = _load(row["metadata_json"], {})
        metadata["last_observed_at"] = _now()
        metadata["last_observed_revision"] = int(knowledge_revision)
        conn.execute(
            "UPDATE engineering_conflicts SET metadata_json=? WHERE id=? "
            "AND workspace_id=?",
            (_j(metadata), conflict_id, workspace_id))
        conn.commit()


def get_conflict(workspace_id: str,
                 conflict_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_conflicts WHERE id=? AND "
            "workspace_id=?", (conflict_id, workspace_id)).fetchone()
        if not row:
            return None
        objects = conn.execute(
            "SELECT * FROM engineering_conflict_objects WHERE conflict_id=? "
            "ORDER BY role, object_kind, object_id",
            (conflict_id,)).fetchall()
        evidence = conn.execute(
            "SELECT * FROM engineering_conflict_evidence WHERE "
            "conflict_id=? ORDER BY evidence_id, quote_hash",
            (conflict_id,)).fetchall()
        decisions = conn.execute(
            "SELECT * FROM engineering_conflict_decisions WHERE "
            "conflict_id=? ORDER BY created_at, id",
            (conflict_id,)).fetchall()
    out = _conflict_row(row)
    out["objects"] = [{"object_kind": o["object_kind"],
                       "object_id": o["object_id"], "role": o["role"]}
                      for o in objects]
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    out["decisions"] = [_conflict_decision_row(d) for d in decisions]
    return out


def find_conflict_by_dedup_key(workspace_id: str, dedup_key: str,
                               *, active_only: bool = True
                               ) -> Optional[Dict[str, Any]]:
    q = ("SELECT * FROM engineering_conflicts WHERE workspace_id=? AND "
         "dedup_key=?")
    args: List[Any] = [workspace_id, dedup_key]
    if active_only:
        q += " AND status NOT IN ('superseded')"
    q += " ORDER BY created_at DESC, id DESC LIMIT 1"
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return _conflict_row(row) if row else None


def find_conflict_by_candidate(workspace_id: str,
                               candidate_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_conflicts WHERE workspace_id=? AND "
            "promoted_from_conflict_candidate_id=? "
            "ORDER BY created_at LIMIT 1",
            (workspace_id, candidate_id)).fetchone()
    return _conflict_row(row) if row else None


def list_conflicts(workspace_id: str, *, status: Optional[str] = None,
                   category: Optional[str] = None,
                   origin: Optional[str] = None,
                   severity: Optional[str] = None,
                   subject_key: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM engineering_conflicts WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("status", status), ("category", category),
                     ("origin", origin), ("severity", severity),
                     ("subject_key", subject_key)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_conflict_row(r) for r in rows]


def count_conflicts(workspace_id: str,
                    *, status: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) FROM engineering_conflicts WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def list_conflict_decisions(workspace_id: str, *,
                            conflict_id: Optional[str] = None,
                            limit: int = 200) -> List[Dict[str, Any]]:
    q = ("SELECT * FROM engineering_conflict_decisions WHERE "
         "workspace_id=?")
    args: List[Any] = [workspace_id]
    if conflict_id:
        q += " AND conflict_id=?"
        args.append(conflict_id)
    q += " ORDER BY created_at, id LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_conflict_decision_row(r) for r in rows]


def conflict_evidence(conflict_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM engineering_conflict_evidence WHERE "
            "conflict_id=? ORDER BY evidence_id, quote_hash",
            (conflict_id,)).fetchall()
    return [{"evidence_id": e["evidence_id"], "role": e["role"],
             "quote": e["quote"], "quote_hash": e["quote_hash"]}
            for e in rows]


def conflict_objects(conflict_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM engineering_conflict_objects WHERE conflict_id=? "
            "ORDER BY role, object_kind, object_id",
            (conflict_id,)).fetchall()
    return [{"object_kind": o["object_kind"], "object_id": o["object_id"],
             "role": o["role"]} for o in rows]


# ---------------------------------------------------------------------------
# Workspace wipe (project delete / terminate)
# ---------------------------------------------------------------------------
def clear_workspace_traceability(workspace_id: str) -> None:
    """Wipe every Phase 6 row of a workspace. Conflict deletion cascades to
    objects/evidence/decisions; path deletion cascades to steps/evidence."""
    conn, lock = _cx()
    with lock:
        try:
            for table in ("engineering_conflicts", "trace_paths",
                          "traceability_gaps",
                          "traceability_coverage_snapshots",
                          "traceability_runs",
                          "workspace_traceability_policies"):
                conn.execute(f"DELETE FROM {table} WHERE workspace_id=?",
                             (workspace_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


__all__ = [
    "get_workspace_policy", "set_workspace_policy_tx",
    "create_run", "update_run", "get_run", "list_runs",
    "latest_completed_run",
    "insert_paths", "get_path", "list_paths", "path_steps", "path_evidence",
    "stale_paths_for_roots", "stale_all_paths", "restamp_paths_run",
    "paths_with_unusable_relations", "mark_paths",
    "changed_entity_ids_since",
    "insert_gap", "update_gap", "update_gap_tx", "get_gap",
    "find_gap_by_fingerprint", "list_gaps", "count_gaps",
    "stale_gaps_for_roots", "restamp_gaps_run",
    "insert_snapshot", "stale_current_snapshots", "latest_snapshot",
    "list_snapshots",
    "insert_conflict_tx", "update_conflict_tx",
    "insert_conflict_decision_tx", "touch_conflict_observation",
    "get_conflict", "find_conflict_by_dedup_key",
    "find_conflict_by_candidate", "list_conflicts", "count_conflicts",
    "list_conflict_decisions", "conflict_evidence", "conflict_objects",
    "clear_workspace_traceability",
]
