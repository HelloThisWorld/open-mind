"""Repositories over the v0005 semantic-plane tables.

Lives beside :mod:`openmind.db` rather than inside it: db.py is the Phase 1–3
store and this module is the Phase 4 one, but both run on the SAME shared WAL
connection and the SAME lock (``db.shared_connection()``), so the semantic
plane inherits every concurrency property the rest of the persistence layer
has. No second connection, no second lock ordering, no cross-process access.

Workspace scoping is structural, exactly like the Phase 2 reads: every
semantic row carries a validated ``workspace_id`` and every read filters on
it, so a candidate id from workspace A resolves to nothing through workspace
B. Multi-row candidate writes commit in ONE transaction — a candidate is
never visible without its evidence joins.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import db
from .models import LifecycleStatus, ReviewStatus, SemanticRunStatus, TargetStatus


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _cx():
    """(connection, lock) — hold the lock for every statement."""
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
# Workspace semantic policy
# ---------------------------------------------------------------------------
DEFAULT_POLICY: Dict[str, Any] = {
    "data_classification": "restricted",
    "allow_remote": False,
    "provider_profile": "",
    "local_cache_enabled": True,
    "task_models": {},
    "budgets": {},
}


def get_policy(workspace_id: str) -> Dict[str, Any]:
    """The workspace's semantic policy. A workspace with no stored row gets
    the fail-closed defaults (restricted, no remote) WITHOUT writing a row —
    reading a policy must never be a write."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM workspace_semantic_policies WHERE workspace_id=?",
            (workspace_id,)).fetchone()
    if not row:
        out = dict(DEFAULT_POLICY)
        out.update({"workspace_id": workspace_id, "stored": False,
                    "created_at": "", "updated_at": ""})
        return out
    return {
        "workspace_id": row["workspace_id"],
        "data_classification": row["data_classification"],
        "allow_remote": bool(row["allow_remote"]),
        "provider_profile": row["provider_profile"],
        "local_cache_enabled": bool(row["local_cache_enabled"]),
        "task_models": _load(row["task_models_json"], {}),
        "budgets": _load(row["budgets_json"], {}),
        "stored": True,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def set_policy(workspace_id: str, *, data_classification: str,
               allow_remote: bool, provider_profile: str,
               local_cache_enabled: bool, task_models: Dict[str, Any],
               budgets: Dict[str, Any]) -> Dict[str, Any]:
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO workspace_semantic_policies (workspace_id,"
            "data_classification,allow_remote,provider_profile,"
            "local_cache_enabled,task_models_json,budgets_json,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET "
            "data_classification=excluded.data_classification, "
            "allow_remote=excluded.allow_remote, "
            "provider_profile=excluded.provider_profile, "
            "local_cache_enabled=excluded.local_cache_enabled, "
            "task_models_json=excluded.task_models_json, "
            "budgets_json=excluded.budgets_json, "
            "updated_at=excluded.updated_at",
            (workspace_id, data_classification, 1 if allow_remote else 0,
             provider_profile, 1 if local_cache_enabled else 0,
             _j(task_models), _j(budgets), ts, ts))
        conn.commit()
    return get_policy(workspace_id)


# ---------------------------------------------------------------------------
# Analysis runs
# ---------------------------------------------------------------------------
def _run_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "job_id": row["job_id"], "run_type": row["run_type"],
        "scope": _load(row["scope_json"], {}), "status": row["status"],
        "provider_profile": row["provider_profile"],
        "provider_kind": row["provider_kind"],
        "model_tier": row["model_tier"], "model_name": row["model_name"],
        "lens_id": row["lens_id"],
        "task_set": _load(row["task_set_json"], []),
        "task_version": row["task_version"],
        "prompt_set_version": row["prompt_set_version"],
        "analyzer_version": row["analyzer_version"],
        "input_hash": row["input_hash"],
        "budget": _load(row["budget_json"], {}),
        "progress": _load(row["progress_json"], {}),
        "summary": _load(row["summary_json"], {}),
        "error": row["error"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def create_run(workspace_id: str, *, run_type: str, scope: Dict[str, Any],
               provider_profile: str, provider_kind: str, model_tier: str,
               task_set: Sequence[str], task_version: str,
               prompt_set_version: str, analyzer_version: str,
               input_hash: str, budget: Dict[str, Any],
               lens_id: Optional[str] = None,
               status: str = SemanticRunStatus.PLANNED) -> Dict[str, Any]:
    run_id = db.new_id("run_")
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO semantic_analysis_runs (id,workspace_id,job_id,"
            "run_type,scope_json,status,provider_profile,provider_kind,"
            "model_tier,model_name,lens_id,task_set_json,task_version,"
            "prompt_set_version,analyzer_version,input_hash,budget_json,"
            "progress_json,summary_json,error,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, workspace_id, "", run_type, _j(scope), status,
             provider_profile, provider_kind, model_tier, "", lens_id,
             _j(list(task_set)), task_version, prompt_set_version,
             analyzer_version, input_hash, _j(budget), "{}", "{}", "", ts))
        conn.commit()
    return get_run(workspace_id, run_id)  # type: ignore[return-value]


def get_run(workspace_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM semantic_analysis_runs WHERE id=? AND workspace_id=?",
            (run_id, workspace_id)).fetchone()
    return _run_row(row) if row else None


def list_runs(workspace_id: str, limit: int = 50, offset: int = 0,
              status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM semantic_analysis_runs WHERE workspace_id=?"
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


def update_run(run_id: str, **fields: Any) -> None:
    """Update run columns. dict/list values are json-encoded into their
    ``*_json`` column; plain values go in as-is."""
    if not fields:
        return
    mapping = {"scope": "scope_json", "task_set": "task_set_json",
               "budget": "budget_json", "progress": "progress_json",
               "summary": "summary_json"}
    sets, args = [], []
    for key, value in fields.items():
        col = mapping.get(key, key)
        if isinstance(value, (dict, list)):
            value = _j(value)
        sets.append(f"{col}=?")
        args.append(value)
    args.append(run_id)
    conn, lock = _cx()
    with lock:
        conn.execute(
            f"UPDATE semantic_analysis_runs SET {', '.join(sets)} WHERE id=?",
            args)
        conn.commit()


# ---------------------------------------------------------------------------
# Analysis targets
# ---------------------------------------------------------------------------
def _target_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "run_id": row["run_id"],
        "revision_id": row["revision_id"], "segment_id": row["segment_id"],
        "task_type": row["task_type"], "input_hash": row["input_hash"],
        "status": row["status"], "attempt": row["attempt"],
        "result_hash": row["result_hash"], "error": row["error"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def create_targets(run_id: str,
                   targets: Sequence[Dict[str, Any]]) -> List[str]:
    """Bulk-insert a run's targets in one transaction. Returns the ids in
    input order. An existing (run, revision, segment, task) row is kept —
    that is what makes resume idempotent."""
    ts = _now()
    ids: List[str] = []
    conn, lock = _cx()
    with lock:
        try:
            for t in targets:
                existing = conn.execute(
                    "SELECT id FROM semantic_analysis_targets WHERE run_id=? "
                    "AND revision_id=? AND segment_id=? AND task_type=?",
                    (run_id, t.get("revision_id", ""), t.get("segment_id", ""),
                     t["task_type"])).fetchone()
                if existing:
                    ids.append(existing["id"])
                    continue
                tid = db.new_id("tgt_")
                conn.execute(
                    "INSERT INTO semantic_analysis_targets (id,run_id,"
                    "revision_id,segment_id,task_type,input_hash,status,"
                    "attempt,result_hash,error,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, run_id, t.get("revision_id", ""),
                     t.get("segment_id", ""), t["task_type"],
                     t.get("input_hash", ""),
                     t.get("status", TargetStatus.PENDING), 0, "", "", ts, ts))
                ids.append(tid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def list_targets(run_id: str, status: Optional[str] = None,
                 limit: int = 10_000) -> List[Dict[str, Any]]:
    q = "SELECT * FROM semantic_analysis_targets WHERE run_id=?"
    args: List[Any] = [run_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY created_at, id LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_target_row(r) for r in rows]


def update_target(target_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets, args = [], []
    for key, value in fields.items():
        sets.append(f"{key}=?")
        args.append(value)
    sets.append("updated_at=?")
    args.append(_now())
    args.append(target_id)
    conn, lock = _cx()
    with lock:
        conn.execute(
            f"UPDATE semantic_analysis_targets SET {', '.join(sets)} "
            f"WHERE id=?", args)
        conn.commit()


def target_status_counts(run_id: str) -> Dict[str, int]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM semantic_analysis_targets "
            "WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# Candidates (engineering-concept / classification / revision-status)
# ---------------------------------------------------------------------------
def _candidate_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"], "target_id": row["target_id"],
        "revision_id": row["revision_id"],
        "candidate_kind": row["candidate_kind"],
        "candidate_type": row["candidate_type"],
        "stable_key": row["stable_key"], "title": row["title"],
        "statement": row["statement"],
        "payload": _load(row["payload_json"], {}),
        "model_confidence_hint": row["model_confidence_hint"],
        "confidence": row["confidence"],
        "evidence_status": row["evidence_status"],
        "review_status": row["review_status"],
        "review_note": row["review_note"],
        "reviewed_at": row["reviewed_at"], "reviewer": row["reviewer"],
        "lifecycle_status": row["lifecycle_status"],
        "task_version": row["task_version"],
        "prompt_version": row["prompt_version"],
        "analyzer_version": row["analyzer_version"],
        "model_name": row["model_name"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "status": "candidate",   # every consumer sees the candidate contract
    }


def insert_candidates(workspace_id: str,
                      candidates: Sequence[Dict[str, Any]]) -> List[str]:
    """Insert verified candidates WITH their evidence joins, transactionally.

    Each entry: the candidate columns plus ``evidence`` — a list of
    ``{evidence_id, role, quote, quote_hash}``. Dedup happens in the caller
    (the runner) BEFORE this writes; this function only persists.
    """
    ts = _now()
    ids: List[str] = []
    conn, lock = _cx()
    with lock:
        try:
            for cand in candidates:
                cid = cand.get("id") or db.new_id("sc_")
                conn.execute(
                    "INSERT INTO semantic_candidates (id,workspace_id,run_id,"
                    "target_id,revision_id,candidate_kind,candidate_type,"
                    "stable_key,title,statement,payload_json,"
                    "model_confidence_hint,confidence,evidence_status,"
                    "review_status,review_note,reviewed_at,reviewer,"
                    "lifecycle_status,task_version,prompt_version,"
                    "analyzer_version,model_name,created_at,updated_at,stale_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, workspace_id, cand.get("run_id", ""),
                     cand.get("target_id", ""), cand.get("revision_id", ""),
                     cand["candidate_kind"], cand["candidate_type"],
                     cand.get("stable_key", ""), cand.get("title", ""),
                     cand.get("statement", ""), _j(cand.get("payload") or {}),
                     cand.get("model_confidence_hint", ""),
                     cand.get("confidence", "low"),
                     cand.get("evidence_status", "rejected"),
                     ReviewStatus.UNREVIEWED, "", None, "",
                     cand.get("lifecycle_status", LifecycleStatus.ACTIVE),
                     cand.get("task_version", ""),
                     cand.get("prompt_version", ""),
                     cand.get("analyzer_version", ""),
                     cand.get("model_name", ""), ts, ts, None))
                for ev in cand.get("evidence") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO semantic_candidate_evidence "
                        "(candidate_id,evidence_id,role,quote,quote_hash) "
                        "VALUES (?,?,?,?,?)",
                        (cid, ev["evidence_id"], ev.get("role", "supports"),
                         ev.get("quote", ""), ev.get("quote_hash", "")))
                ids.append(cid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def list_candidates(workspace_id: str, *, candidate_kind: Optional[str] = None,
                    candidate_type: Optional[str] = None,
                    review_status: Optional[str] = None,
                    lifecycle_status: Optional[str] = None,
                    run_id: Optional[str] = None,
                    limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM semantic_candidates WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("candidate_kind", candidate_kind),
                     ("candidate_type", candidate_type),
                     ("review_status", review_status),
                     ("lifecycle_status", lifecycle_status),
                     ("run_id", run_id)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_candidate_row(r) for r in rows]


def count_candidates(workspace_id: str, **filters: Optional[str]) -> int:
    q = "SELECT COUNT(*) FROM semantic_candidates WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col in ("candidate_kind", "candidate_type", "review_status",
                "lifecycle_status", "run_id"):
        val = filters.get(col)
        if val:
            q += f" AND {col}=?"
            args.append(val)
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def get_candidate(workspace_id: str,
                  candidate_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM semantic_candidates WHERE id=? AND workspace_id=?",
            (candidate_id, workspace_id)).fetchone()
        if not row:
            return None
        evidence = conn.execute(
            "SELECT * FROM semantic_candidate_evidence WHERE candidate_id=? "
            "ORDER BY evidence_id, quote_hash", (candidate_id,)).fetchall()
    out = _candidate_row(row)
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    return out


def existing_candidate_keys(workspace_id: str,
                            revision_id: str) -> Dict[Tuple[str, str, str], str]:
    """(candidate_type, stable_key, statement-hash-or-title) -> id for ACTIVE
    candidates of one revision, for cheap in-runner dedup before insert."""
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT id, candidate_type, stable_key, title FROM "
            "semantic_candidates WHERE workspace_id=? AND revision_id=? AND "
            "lifecycle_status='active'", (workspace_id, revision_id)).fetchall()
    return {(r["candidate_type"], r["stable_key"], r["title"]): r["id"]
            for r in rows}


# ---------------------------------------------------------------------------
# Relation candidates
# ---------------------------------------------------------------------------
def _relation_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"], "target_id": row["target_id"],
        "relation_type": row["relation_type"],
        "source_ref": _load(row["source_ref_json"], {}),
        "target_ref": _load(row["target_ref_json"], {}),
        "source_candidate_id": row["source_candidate_id"],
        "target_candidate_id": row["target_candidate_id"],
        "reason": row["reason"],
        "model_confidence_hint": row["model_confidence_hint"],
        "confidence": row["confidence"],
        "evidence_status": row["evidence_status"],
        "review_status": row["review_status"],
        "review_note": row["review_note"],
        "reviewed_at": row["reviewed_at"], "reviewer": row["reviewer"],
        "lifecycle_status": row["lifecycle_status"],
        "payload": _load(row["payload_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "status": "candidate",
    }


def insert_relations(workspace_id: str,
                     relations: Sequence[Dict[str, Any]]) -> List[str]:
    ts = _now()
    ids: List[str] = []
    conn, lock = _cx()
    with lock:
        try:
            for rel in relations:
                rid = rel.get("id") or db.new_id("sr_")
                conn.execute(
                    "INSERT INTO semantic_relation_candidates (id,workspace_id,"
                    "run_id,target_id,relation_type,source_ref_json,"
                    "target_ref_json,source_candidate_id,target_candidate_id,"
                    "reason,model_confidence_hint,confidence,evidence_status,"
                    "review_status,review_note,reviewed_at,reviewer,"
                    "lifecycle_status,payload_json,created_at,updated_at,"
                    "stale_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, workspace_id, rel.get("run_id", ""),
                     rel.get("target_id", ""), rel["relation_type"],
                     _j(rel.get("source_ref") or {}),
                     _j(rel.get("target_ref") or {}),
                     rel.get("source_candidate_id"),
                     rel.get("target_candidate_id"),
                     rel.get("reason", ""),
                     rel.get("model_confidence_hint", ""),
                     rel.get("confidence", "low"),
                     rel.get("evidence_status", "rejected"),
                     ReviewStatus.UNREVIEWED, "", None, "",
                     rel.get("lifecycle_status", LifecycleStatus.ACTIVE),
                     _j(rel.get("payload") or {}), ts, ts, None))
                for ev in rel.get("evidence") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO semantic_relation_evidence "
                        "(relation_id,evidence_id,role,quote,quote_hash) "
                        "VALUES (?,?,?,?,?)",
                        (rid, ev["evidence_id"], ev.get("role", "supports"),
                         ev.get("quote", ""), ev.get("quote_hash", "")))
                ids.append(rid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def list_relations(workspace_id: str, *, relation_type: Optional[str] = None,
                   review_status: Optional[str] = None,
                   lifecycle_status: Optional[str] = None,
                   run_id: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM semantic_relation_candidates WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("relation_type", relation_type),
                     ("review_status", review_status),
                     ("lifecycle_status", lifecycle_status),
                     ("run_id", run_id)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_relation_row(r) for r in rows]


def get_relation(workspace_id: str,
                 relation_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM semantic_relation_candidates WHERE id=? AND "
            "workspace_id=?", (relation_id, workspace_id)).fetchone()
        if not row:
            return None
        evidence = conn.execute(
            "SELECT * FROM semantic_relation_evidence WHERE relation_id=? "
            "ORDER BY evidence_id, quote_hash", (relation_id,)).fetchall()
    out = _relation_row(row)
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    return out


# ---------------------------------------------------------------------------
# Conflict candidates
# ---------------------------------------------------------------------------
def _conflict_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "run_id": row["run_id"], "target_id": row["target_id"],
        "category": row["category"],
        "refs": _load(row["refs_json"], []),
        "left_candidate_id": row["left_candidate_id"],
        "right_candidate_id": row["right_candidate_id"],
        "explanation": row["explanation"],
        "model_confidence_hint": row["model_confidence_hint"],
        "confidence": row["confidence"],
        "evidence_status": row["evidence_status"],
        "review_status": row["review_status"],
        "review_note": row["review_note"],
        "reviewed_at": row["reviewed_at"], "reviewer": row["reviewer"],
        "lifecycle_status": row["lifecycle_status"],
        "payload": _load(row["payload_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "status": "candidate",
    }


def insert_conflicts(workspace_id: str,
                     conflicts: Sequence[Dict[str, Any]]) -> List[str]:
    ts = _now()
    ids: List[str] = []
    conn, lock = _cx()
    with lock:
        try:
            for conf in conflicts:
                fid = conf.get("id") or db.new_id("sx_")
                conn.execute(
                    "INSERT INTO semantic_conflict_candidates (id,workspace_id,"
                    "run_id,target_id,category,refs_json,left_candidate_id,"
                    "right_candidate_id,explanation,model_confidence_hint,"
                    "confidence,evidence_status,review_status,review_note,"
                    "reviewed_at,reviewer,lifecycle_status,payload_json,"
                    "created_at,updated_at,stale_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, workspace_id, conf.get("run_id", ""),
                     conf.get("target_id", ""), conf["category"],
                     _j(conf.get("refs") or []),
                     conf.get("left_candidate_id"),
                     conf.get("right_candidate_id"),
                     conf.get("explanation", ""),
                     conf.get("model_confidence_hint", ""),
                     conf.get("confidence", "low"),
                     conf.get("evidence_status", "rejected"),
                     ReviewStatus.UNREVIEWED, "", None, "",
                     conf.get("lifecycle_status", LifecycleStatus.ACTIVE),
                     _j(conf.get("payload") or {}), ts, ts, None))
                for ev in conf.get("evidence") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO semantic_conflict_evidence "
                        "(conflict_id,evidence_id,role,quote,quote_hash) "
                        "VALUES (?,?,?,?,?)",
                        (fid, ev["evidence_id"], ev.get("role", "supports"),
                         ev.get("quote", ""), ev.get("quote_hash", "")))
                ids.append(fid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def list_conflicts(workspace_id: str, *, category: Optional[str] = None,
                   review_status: Optional[str] = None,
                   lifecycle_status: Optional[str] = None,
                   run_id: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM semantic_conflict_candidates WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("category", category), ("review_status", review_status),
                     ("lifecycle_status", lifecycle_status), ("run_id", run_id)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_conflict_row(r) for r in rows]


def get_conflict(workspace_id: str,
                 conflict_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM semantic_conflict_candidates WHERE id=? AND "
            "workspace_id=?", (conflict_id, workspace_id)).fetchone()
        if not row:
            return None
        evidence = conn.execute(
            "SELECT * FROM semantic_conflict_evidence WHERE conflict_id=? "
            "ORDER BY evidence_id, quote_hash", (conflict_id,)).fetchall()
    out = _conflict_row(row)
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    return out


# ---------------------------------------------------------------------------
# Review (shared by the three candidate tables)
# ---------------------------------------------------------------------------
#: Table names by candidate kind — an allowlist, so no caller-provided string
#: is ever interpolated into SQL.
_REVIEW_TABLES = {
    "candidate": "semantic_candidates",
    "relation": "semantic_relation_candidates",
    "conflict": "semantic_conflict_candidates",
}


def apply_review(workspace_id: str, kind: str, entity_id: str, *,
                 review_status: str, review_note: str,
                 reviewer: str) -> bool:
    """Set review fields on one candidate/relation/conflict. Returns False if
    the id does not exist in this workspace. Never touches lifecycle_status —
    a stale confirmed candidate stays stale."""
    table = _REVIEW_TABLES[kind]
    ts = _now()
    reviewed_at = None if review_status == ReviewStatus.UNREVIEWED else ts
    conn, lock = _cx()
    with lock:
        cur = conn.execute(
            f"UPDATE {table} SET review_status=?, review_note=?, reviewer=?, "
            f"reviewed_at=?, updated_at=? WHERE id=? AND workspace_id=?",
            (review_status, review_note, reviewer, reviewed_at, ts,
             entity_id, workspace_id))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Staleness reconciliation
# ---------------------------------------------------------------------------
def reconcile_staleness(workspace_id: str) -> Dict[str, int]:
    """Mark candidates whose source Revision is no longer current as STALE,
    then propagate to relation and conflict candidates that depend on them.

    Incremental and indexed: only ACTIVE rows are considered, matched through
    ``idx_sem_cand_revision`` and the current-revision set of the workspace's
    assets. Review status is untouched; nothing is deleted. Safe to run any
    number of times — a second run changes nothing.
    """
    ts = _now()
    conn, lock = _cx()
    with lock:
        try:
            cur = conn.execute(
                "UPDATE semantic_candidates SET lifecycle_status='stale', "
                "stale_at=?, updated_at=? WHERE workspace_id=? AND "
                "lifecycle_status='active' AND revision_id != '' AND "
                "revision_id NOT IN (SELECT current_revision_id FROM assets "
                "WHERE workspace_id=? AND current_revision_id IS NOT NULL)",
                (ts, ts, workspace_id, workspace_id))
            stale_candidates = cur.rowcount
            cur = conn.execute(
                "UPDATE semantic_relation_candidates SET "
                "lifecycle_status='stale', stale_at=?, updated_at=? "
                "WHERE workspace_id=? AND lifecycle_status='active' AND ("
                "(source_candidate_id IS NOT NULL AND source_candidate_id IN "
                " (SELECT id FROM semantic_candidates WHERE workspace_id=? "
                "  AND lifecycle_status='stale')) OR "
                "(target_candidate_id IS NOT NULL AND target_candidate_id IN "
                " (SELECT id FROM semantic_candidates WHERE workspace_id=? "
                "  AND lifecycle_status='stale')))",
                (ts, ts, workspace_id, workspace_id, workspace_id))
            stale_relations = cur.rowcount
            cur = conn.execute(
                "UPDATE semantic_conflict_candidates SET "
                "lifecycle_status='stale', stale_at=?, updated_at=? "
                "WHERE workspace_id=? AND lifecycle_status='active' AND ("
                "(left_candidate_id IS NOT NULL AND left_candidate_id IN "
                " (SELECT id FROM semantic_candidates WHERE workspace_id=? "
                "  AND lifecycle_status='stale')) OR "
                "(right_candidate_id IS NOT NULL AND right_candidate_id IN "
                " (SELECT id FROM semantic_candidates WHERE workspace_id=? "
                "  AND lifecycle_status='stale')))",
                (ts, ts, workspace_id, workspace_id, workspace_id))
            stale_conflicts = cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"stale_candidates": stale_candidates,
            "stale_relations": stale_relations,
            "stale_conflicts": stale_conflicts}


# ---------------------------------------------------------------------------
# Usage ledger
# ---------------------------------------------------------------------------
def record_usage(entry: Dict[str, Any]) -> str:
    uid = db.new_id("use_")
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO semantic_usage (id,run_id,target_id,request_id,"
            "provider_profile,provider_kind,model_name,task_type,input_tokens,"
            "output_tokens,cached_tokens,estimated_cost,currency,cost_source,"
            "latency_ms,retry_count,status,request_hash,response_hash,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, entry.get("run_id", ""), entry.get("target_id", ""),
             entry.get("request_id", ""), entry.get("provider_profile", ""),
             entry.get("provider_kind", ""), entry.get("model_name", ""),
             entry.get("task_type", ""), entry.get("input_tokens"),
             entry.get("output_tokens"), entry.get("cached_tokens"),
             entry.get("estimated_cost"), entry.get("currency", ""),
             entry.get("cost_source", "unknown"), entry.get("latency_ms"),
             int(entry.get("retry_count") or 0), entry.get("status", ""),
             entry.get("request_hash", ""), entry.get("response_hash", ""),
             _now()))
        conn.commit()
    return uid


def list_usage(run_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM semantic_usage WHERE run_id=? "
            "ORDER BY created_at, id LIMIT ?",
            (run_id, max(0, int(limit)))).fetchall()
    return [dict(r) for r in rows]


def usage_totals(run_id: str) -> Dict[str, Any]:
    """Aggregate usage for one run. SUM over NULL-only columns is NULL, which
    is reported as None — unknown, not zero."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT COUNT(*) AS requests, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cached_tokens) AS cached_tokens, "
            "SUM(estimated_cost) AS estimated_cost, "
            "SUM(latency_ms) AS latency_ms FROM semantic_usage WHERE run_id=?",
            (run_id,)).fetchone()
    return {
        "requests": int(row["requests"] or 0),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cached_tokens": row["cached_tokens"],
        "estimated_cost": row["estimated_cost"],
        "latency_ms": row["latency_ms"],
    }


def usage_since(workspace_id: str, since: str) -> Dict[str, Any]:
    """Workspace-wide usage since a timestamp (daily budget enforcement).
    Joined through the run header so the ledger itself stays lean."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT COUNT(*) AS requests, SUM(u.input_tokens) AS input_tokens, "
            "SUM(u.output_tokens) AS output_tokens FROM semantic_usage u "
            "JOIN semantic_analysis_runs r ON u.run_id = r.id "
            "WHERE r.workspace_id=? AND u.created_at >= ?",
            (workspace_id, since)).fetchone()
    return {
        "requests": int(row["requests"] or 0),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
    }


# ---------------------------------------------------------------------------
# Local semantic cache
# ---------------------------------------------------------------------------
def cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM semantic_cache WHERE cache_key=?",
            (cache_key,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE semantic_cache SET last_used_at=? WHERE cache_key=?",
                     (_now(), cache_key))
        conn.commit()
    return {"cache_key": row["cache_key"],
            "provider_kind": row["provider_kind"],
            "model_name": row["model_name"], "task_type": row["task_type"],
            "prompt_hash": row["prompt_hash"],
            "schema_version": row["schema_version"],
            "input_hash": row["input_hash"],
            "output": _load(row["output_json"], {}),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"]}


def cache_find_by_input(task_type: str, input_hash: str) -> bool:
    """Plan-time cache probe: does ANY entry exist for this task + target
    input hash? Read-only — last_used_at is deliberately not bumped, so a
    dry-run plan leaves the cache exactly as it found it. The runner's real
    lookup uses the full composite key; this is an estimate."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT 1 FROM semantic_cache WHERE task_type=? AND input_hash=? "
            "LIMIT 1", (task_type, input_hash)).fetchone()
    return row is not None


def cache_put(cache_key: str, *, provider_kind: str, model_name: str,
              task_type: str, prompt_hash: str, schema_version: str,
              input_hash: str, output: Dict[str, Any]) -> None:
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO semantic_cache (cache_key,provider_kind,model_name,"
            "task_type,prompt_hash,schema_version,input_hash,output_json,"
            "created_at,last_used_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "output_json=excluded.output_json, last_used_at=excluded.last_used_at",
            (cache_key, provider_kind, model_name, task_type, prompt_hash,
             schema_version, input_hash, _j(output), ts, ts))
        conn.commit()


# ---------------------------------------------------------------------------
# Project lenses
# ---------------------------------------------------------------------------
def _lens_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "organization_key": row["organization_key"], "name": row["name"],
        "version": row["version"], "source": row["source"],
        "status": row["status"], "schema_version": row["schema_version"],
        "definition": _load(row["definition_json"], {}),
        "validation": _load(row["validation_json"], {}),
        "provider_profile": row["provider_profile"],
        "model_name": row["model_name"],
        "prompt_version": row["prompt_version"],
        "input_hash": row["input_hash"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "approved_at": row["approved_at"],
    }


def insert_lens(workspace_id: str, lens: Dict[str, Any]) -> str:
    lens_id = lens.get("id") or db.new_id("lens_")
    ts = _now()
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO project_lenses (id,workspace_id,organization_key,"
            "name,version,source,status,schema_version,definition_json,"
            "validation_json,provider_profile,model_name,prompt_version,"
            "input_hash,created_at,updated_at,approved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lens_id, workspace_id, lens.get("organization_key", ""),
             lens["name"], str(lens.get("version", "1")), lens["source"],
             lens.get("status", "provisional"),
             lens.get("schema_version", ""), _j(lens.get("definition") or {}),
             _j(lens.get("validation") or {}),
             lens.get("provider_profile", ""), lens.get("model_name", ""),
             lens.get("prompt_version", ""), lens.get("input_hash", ""),
             ts, ts, None))
        conn.commit()
    return lens_id


def update_lens(workspace_id: str, lens_id: str, **fields: Any) -> bool:
    if not fields:
        return True
    mapping = {"definition": "definition_json", "validation": "validation_json"}
    sets, args = [], []
    for key, value in fields.items():
        col = mapping.get(key, key)
        if isinstance(value, (dict, list)):
            value = _j(value)
        sets.append(f"{col}=?")
        args.append(value)
    sets.append("updated_at=?")
    args.append(_now())
    args.extend([lens_id, workspace_id])
    conn, lock = _cx()
    with lock:
        cur = conn.execute(
            f"UPDATE project_lenses SET {', '.join(sets)} "
            f"WHERE id=? AND workspace_id=?", args)
        conn.commit()
    return cur.rowcount > 0


def get_lens(workspace_id: str, lens_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM project_lenses WHERE id=? AND workspace_id=?",
            (lens_id, workspace_id)).fetchone()
    return _lens_row(row) if row else None


def list_lenses(workspace_id: str, status: Optional[str] = None,
                source: Optional[str] = None,
                limit: int = 100) -> List[Dict[str, Any]]:
    q = "SELECT * FROM project_lenses WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    if source:
        q += " AND source=?"
        args.append(source)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_lens_row(r) for r in rows]


def get_active_lens(workspace_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM project_lenses WHERE workspace_id=? AND "
            "status='active' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (workspace_id,)).fetchone()
    return _lens_row(row) if row else None


def find_lens_by_name(workspace_id: str, name: str,
                      source: Optional[str] = None) -> Optional[Dict[str, Any]]:
    q = "SELECT * FROM project_lenses WHERE workspace_id=? AND name=?"
    args: List[Any] = [workspace_id, name]
    if source:
        q += " AND source=?"
        args.append(source)
    q += " ORDER BY created_at DESC LIMIT 1"
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return _lens_row(row) if row else None
