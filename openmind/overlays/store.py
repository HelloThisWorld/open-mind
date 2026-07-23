"""Repository over the v0008 overlay tables.

Runs on the SAME shared WAL connection and RLock as every other store. Overlay
rows reference canonical objects only by id — there is no foreign key into a
canonical table — so nothing here can cascade a write or a delete into the
canonical Base Workspace. Deleting an overlay cascades to its own files,
segments, evidence, deltas, impacts and reports via the v0008 foreign keys.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .. import db
from ..git.vocabularies import OverlayState


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


def _row(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------
def create_overlay(workspace_id: str, *, overlay_kind: str, name: str = "",
                   base_knowledge_revision: int = 0,
                   base_traceability_run_id: str = "",
                   base_policy_checksum: str = "",
                   options: Optional[Dict[str, Any]] = None,
                   state: str = OverlayState.PLANNED) -> Dict[str, Any]:
    conn, lock = _cx()
    ts = _now()
    oid = db.new_id("ov_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlays(id, workspace_id, name, overlay_kind, "
            "state, base_knowledge_revision, base_traceability_run_id, "
            "base_policy_checksum, overlay_revision, source_hash, options_json, "
            "summary_json, error, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, workspace_id, name, overlay_kind, state,
             int(base_knowledge_revision), base_traceability_run_id,
             base_policy_checksum, 0, "", _j(options or {}), _j({}), "",
             ts, ts))
        conn.commit()
    return get_overlay(workspace_id, oid)


def get_overlay(workspace_id: str, overlay_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM git_overlays WHERE id=? AND workspace_id=?",
            (overlay_id, workspace_id)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["options"] = _load(d.pop("options_json", "{}"), {})
    d["summary"] = _load(d.pop("summary_json", "{}"), {})
    return d


def list_overlays(workspace_id: str, *, state: Optional[str] = None,
                  limit: int = 200) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if state:
            rows = conn.execute(
                "SELECT * FROM git_overlays WHERE workspace_id=? AND state=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (workspace_id, state, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM git_overlays WHERE workspace_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (workspace_id, int(limit))).fetchall()
    out = []
    for row in rows:
        d = _row(row)
        d["options"] = _load(d.pop("options_json", "{}"), {})
        d["summary"] = _load(d.pop("summary_json", "{}"), {})
        out.append(d)
    return out


def update_overlay(workspace_id: str, overlay_id: str, **fields: Any) -> None:
    """Update mutable overlay fields. JSON fields (options/summary) are passed
    as dicts under ``options``/``summary`` and serialized here."""
    if "options" in fields:
        fields["options_json"] = _j(fields.pop("options"))
    if "summary" in fields:
        fields["summary_json"] = _j(fields.pop("summary"))
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    args = list(fields.values()) + [overlay_id, workspace_id]
    conn, lock = _cx()
    with lock:
        conn.execute(f"UPDATE git_overlays SET {cols} WHERE id=? AND "
                     f"workspace_id=?", args)
        conn.commit()


def set_state(workspace_id: str, overlay_id: str, state: str, *,
              error: str = "") -> None:
    ts = _now()
    extra = {"state": state, "error": error}
    if state == OverlayState.READY:
        extra["ready_at"] = ts
    elif state == OverlayState.STALE:
        extra["stale_at"] = ts
    elif state in (OverlayState.CLOSED, OverlayState.ABANDONED,
                   OverlayState.MERGED):
        extra["closed_at"] = ts
    update_overlay(workspace_id, overlay_id, **extra)


def next_revision(workspace_id: str, overlay_id: str) -> int:
    """Allocate and persist the next overlay revision number."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT overlay_revision FROM git_overlays WHERE id=? AND "
            "workspace_id=?", (overlay_id, workspace_id)).fetchone()
        n = (row["overlay_revision"] if row else 0) + 1
        conn.execute("UPDATE git_overlays SET overlay_revision=?, updated_at=? "
                     "WHERE id=? AND workspace_id=?",
                     (n, _now(), overlay_id, workspace_id))
        conn.commit()
    return n


def delete_overlay(workspace_id: str, overlay_id: str) -> bool:
    """Delete an overlay and (via FK cascade) all its data. Search-index
    bookkeeping is removed explicitly (it has no FK). Returns True if a row was
    removed. Never touches canonical data."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT id FROM git_overlays WHERE id=? AND workspace_id=?",
            (overlay_id, workspace_id)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM git_overlay_search_index WHERE overlay_id=?",
                     (overlay_id,))
        conn.execute("DELETE FROM git_overlays WHERE id=? AND workspace_id=?",
                     (overlay_id, workspace_id))
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Overlay repositories
# ---------------------------------------------------------------------------
def add_overlay_repository(overlay_id: str, repository_id: str, *,
                           base_ref: str = "", head_ref: str = "",
                           base_commit: str = "", head_commit: str = "",
                           merge_base_commit: str = "", base_tree: str = "",
                           head_tree: str = "", branch_name: str = "",
                           target_branch: str = "", worktree_hash: str = "",
                           dirty_state: Optional[Dict[str, Any]] = None
                           ) -> str:
    conn, lock = _cx()
    ts = _now()
    rid = db.new_id("ovr_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_repositories(id, overlay_id, "
            "repository_id, base_ref, head_ref, base_commit, head_commit, "
            "merge_base_commit, base_tree, head_tree, branch_name, "
            "target_branch, worktree_hash, dirty_state_json, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, overlay_id, repository_id, base_ref, head_ref, base_commit,
             head_commit, merge_base_commit, base_tree, head_tree, branch_name,
             target_branch, worktree_hash, _j(dirty_state or {}), ts, ts))
        conn.commit()
    return rid


def list_overlay_repositories(overlay_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM git_overlay_repositories WHERE overlay_id=? "
            "ORDER BY repository_id", (overlay_id,)).fetchall()
    out = []
    for row in rows:
        d = _row(row)
        d["dirty_state"] = _load(d.pop("dirty_state_json", "{}"), {})
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Overlay files
# ---------------------------------------------------------------------------
def add_file(overlay_id: str, overlay_repository_id: str, fc, *,
             changed_ranges: Optional[Dict[str, Any]] = None) -> str:
    """Persist a FileChange (openmind.git.models.FileChange) as an overlay
    file row. Returns the file id."""
    conn, lock = _cx()
    ts = _now()
    fid = db.new_id("ovf_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_files(id, overlay_id, "
            "overlay_repository_id, change_type, old_path, new_path, old_mode, "
            "new_mode, old_blob_sha, new_blob_sha, old_content_blob_hash, "
            "new_content_blob_hash, is_binary, is_symlink, is_submodule, "
            "is_lfs_pointer, similarity, additions, deletions, "
            "changed_ranges_json, layer, status, metadata_json, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, overlay_id, overlay_repository_id, fc.change_type,
             fc.old_path, fc.new_path, fc.old_mode, fc.new_mode,
             fc.old_blob_sha, fc.new_blob_sha, fc.old_content_blob_hash,
             fc.new_content_blob_hash, 1 if fc.is_binary else 0,
             1 if fc.is_symlink else 0, 1 if fc.is_submodule else 0,
             1 if fc.is_lfs_pointer else 0, int(fc.similarity),
             int(fc.additions), int(fc.deletions),
             _j(changed_ranges or {}), fc.layer, fc.status, _j(fc.metadata),
             ts, ts))
        conn.commit()
    return fid


def list_files(overlay_id: str, *, change_type: Optional[str] = None,
               limit: int = 5000) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if change_type:
            rows = conn.execute(
                "SELECT * FROM git_overlay_files WHERE overlay_id=? AND "
                "change_type=? ORDER BY new_path, old_path LIMIT ?",
                (overlay_id, change_type, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM git_overlay_files WHERE overlay_id=? "
                "ORDER BY new_path, old_path LIMIT ?",
                (overlay_id, int(limit))).fetchall()
    out = []
    for row in rows:
        d = _row(row)
        d["changed_ranges"] = _load(d.pop("changed_ranges_json", "{}"), {})
        d["metadata"] = _load(d.pop("metadata_json", "{}"), {})
        out.append(d)
    return out


def get_file(overlay_id: str, file_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM git_overlay_files WHERE overlay_id=? AND id=?",
            (overlay_id, file_id)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["changed_ranges"] = _load(d.pop("changed_ranges_json", "{}"), {})
    d["metadata"] = _load(d.pop("metadata_json", "{}"), {})
    return d


# ---------------------------------------------------------------------------
# Segments & evidence
# ---------------------------------------------------------------------------
def add_segment(overlay_id: str, overlay_file_id: str, *, side: str,
                segment_key: str, segment_type: str, change_class: str,
                ordinal: int, start_line: int, end_line: int, symbol: str,
                content_hash: str, content_blob_hash: str = "",
                content_mode: str = "verbatim",
                metadata: Optional[Dict[str, Any]] = None) -> str:
    conn, lock = _cx()
    sid = db.new_id("ovs_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_segments(id, overlay_id, overlay_file_id, "
            "side, segment_key, segment_type, change_class, ordinal, "
            "start_line, end_line, symbol, content_hash, content_blob_hash, "
            "content_mode, metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, overlay_id, overlay_file_id, side, segment_key, segment_type,
             change_class, int(ordinal), int(start_line), int(end_line),
             symbol, content_hash, content_blob_hash, content_mode,
             _j(metadata or {})))
        conn.commit()
    return sid


def list_segments(overlay_id: str, *, overlay_file_id: Optional[str] = None,
                  side: Optional[str] = None, change_class: Optional[str] = None,
                  limit: int = 20000) -> List[Dict[str, Any]]:
    q = "SELECT * FROM git_overlay_segments WHERE overlay_id=?"
    args: List[Any] = [overlay_id]
    if overlay_file_id:
        q += " AND overlay_file_id=?"
        args.append(overlay_file_id)
    if side:
        q += " AND side=?"
        args.append(side)
    if change_class:
        q += " AND change_class=?"
        args.append(change_class)
    q += " ORDER BY overlay_file_id, side, ordinal LIMIT ?"
    args.append(int(limit))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    out = []
    for row in rows:
        d = _row(row)
        d["metadata"] = _load(d.pop("metadata_json", "{}"), {})
        out.append(d)
    return out


def add_evidence(overlay_id: str, *, overlay_file_id: str = "",
                 segment_id: str = "", side: str = "after",
                 locator: Optional[Dict[str, Any]] = None, excerpt: str = "",
                 content_hash: str = "") -> str:
    conn, lock = _cx()
    ts = _now()
    eid = db.new_id("oev_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_evidence(id, overlay_id, overlay_file_id, "
            "segment_id, side, locator_json, excerpt, content_hash, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, overlay_id, overlay_file_id, segment_id, side,
             _j(locator or {}), excerpt[:2000], content_hash, ts))
        conn.commit()
    return eid


def get_evidence(overlay_id: str, evidence_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM git_overlay_evidence WHERE overlay_id=? AND id=?",
            (overlay_id, evidence_id)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["locator"] = _load(d.pop("locator_json", "{}"), {})
    return d


def list_evidence(overlay_id: str, *, side: Optional[str] = None,
                  limit: int = 20000) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if side:
            rows = conn.execute(
                "SELECT * FROM git_overlay_evidence WHERE overlay_id=? AND "
                "side=? ORDER BY id LIMIT ?",
                (overlay_id, side, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM git_overlay_evidence WHERE overlay_id=? "
                "ORDER BY id LIMIT ?", (overlay_id, int(limit))).fetchall()
    out = []
    for row in rows:
        d = _row(row)
        d["locator"] = _load(d.pop("locator_json", "{}"), {})
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Deltas & impacts (generic writers)
# ---------------------------------------------------------------------------
def add_entity_delta(overlay_id: str, *, delta_type: str,
                     base_entity_id: str = "", canonical_key: str = "",
                     entity_type: str = "", before: Any = None,
                     after: Any = None, reason: str = "",
                     confidence: str = "low",
                     evidence_ids: Optional[List[str]] = None) -> str:
    conn, lock = _cx()
    eid = db.new_id("oed_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_entity_deltas(id, overlay_id, delta_type, "
            "base_entity_id, canonical_key, entity_type, before_json, "
            "after_json, reason, confidence, evidence_ids_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, overlay_id, delta_type, base_entity_id, canonical_key,
             entity_type, _j(before or {}), _j(after or {}), reason,
             confidence, json.dumps(list(evidence_ids or [])), _now()))
        conn.commit()
    return eid


def add_relation_delta(overlay_id: str, *, delta_type: str,
                       base_relation_id: str = "", source_ref: Any = None,
                       target_ref: Any = None, relation_type: str = "",
                       before: Any = None, after: Any = None, reason: str = "",
                       confidence: str = "low",
                       evidence_ids: Optional[List[str]] = None) -> str:
    conn, lock = _cx()
    rid = db.new_id("ord_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_relation_deltas(id, overlay_id, "
            "delta_type, base_relation_id, source_ref_json, target_ref_json, "
            "relation_type, before_json, after_json, reason, confidence, "
            "evidence_ids_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, overlay_id, delta_type, base_relation_id, _j(source_ref or {}),
             _j(target_ref or {}), relation_type, _j(before or {}),
             _j(after or {}), reason, confidence,
             json.dumps(list(evidence_ids or [])), _now()))
        conn.commit()
    return rid


def add_trace_impact(overlay_id: str, *, root_requirement_id: str = "",
                     impact_type: str, severity: str = "info",
                     before: Any = None, after: Any = None,
                     introduced_gaps: Optional[List[Any]] = None,
                     resolved_gaps: Optional[List[Any]] = None,
                     affected_trace_ids: Optional[List[str]] = None,
                     reason: str = "",
                     evidence_ids: Optional[List[str]] = None) -> str:
    conn, lock = _cx()
    tid = db.new_id("oti_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_trace_impacts(id, overlay_id, "
            "root_requirement_id, impact_type, severity, before_json, "
            "after_json, introduced_gaps_json, resolved_gaps_json, "
            "affected_trace_ids_json, reason, evidence_ids_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, overlay_id, root_requirement_id, impact_type, severity,
             _j(before or {}), _j(after or {}),
             json.dumps(list(introduced_gaps or [])),
             json.dumps(list(resolved_gaps or [])),
             json.dumps(list(affected_trace_ids or [])), reason,
             json.dumps(list(evidence_ids or [])), _now()))
        conn.commit()
    return tid


def add_conflict_impact(overlay_id: str, *, subject_key: str = "",
                        impact_type: str, category: str = "",
                        severity: str = "info", base_conflict_id: str = "",
                        before: Any = None, after: Any = None, reason: str = "",
                        evidence_ids: Optional[List[str]] = None) -> str:
    conn, lock = _cx()
    cid = db.new_id("oci_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_conflict_impacts(id, overlay_id, "
            "subject_key, impact_type, category, severity, base_conflict_id, "
            "before_json, after_json, reason, evidence_ids_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, overlay_id, subject_key, impact_type, category, severity,
             base_conflict_id, _j(before or {}), _j(after or {}), reason,
             json.dumps(list(evidence_ids or [])), _now()))
        conn.commit()
    return cid


def _list_json(rows, *json_cols) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        d = _row(row)
        for col in list(d.keys()):
            if col.endswith("_json"):
                d[col[:-5]] = _load(d.pop(col), [] if col.endswith("ids_json")
                                    or "gaps" in col or "trace_ids" in col
                                    else {})
        out.append(d)
    return out


def list_entity_deltas(overlay_id: str, *, delta_type: Optional[str] = None
                       ) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if delta_type:
            rows = conn.execute(
                "SELECT * FROM git_overlay_entity_deltas WHERE overlay_id=? AND "
                "delta_type=? ORDER BY canonical_key, id",
                (overlay_id, delta_type)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM git_overlay_entity_deltas WHERE overlay_id=? "
                "ORDER BY canonical_key, id", (overlay_id,)).fetchall()
    return _list_json(rows)


def list_relation_deltas(overlay_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM git_overlay_relation_deltas WHERE overlay_id=? "
            "ORDER BY relation_type, id", (overlay_id,)).fetchall()
    return _list_json(rows)


def list_trace_impacts(overlay_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM git_overlay_trace_impacts WHERE overlay_id=? "
            "ORDER BY root_requirement_id, impact_type, id",
            (overlay_id,)).fetchall()
    return _list_json(rows)


def list_conflict_impacts(overlay_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM git_overlay_conflict_impacts WHERE overlay_id=? "
            "ORDER BY subject_key, impact_type, id", (overlay_id,)).fetchall()
    return _list_json(rows)


def clear_derived(overlay_id: str) -> None:
    """Remove derived deltas/impacts before a rebuild (files/segments/evidence
    are reused across an incremental refresh; deltas/impacts are recomputed)."""
    conn, lock = _cx()
    with lock:
        for table in ("git_overlay_entity_deltas", "git_overlay_relation_deltas",
                      "git_overlay_trace_impacts",
                      "git_overlay_conflict_impacts"):
            conn.execute(f"DELETE FROM {table} WHERE overlay_id=?", (overlay_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def save_report(overlay_id: str, *, overlay_revision: int,
                report_schema_version: str, report: Dict[str, Any],
                report_hash: str, markdown_blob_hash: str = "") -> str:
    conn, lock = _cx()
    rid = db.new_id("ovrep_")
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_reports(id, overlay_id, overlay_revision, "
            "report_schema_version, report_json, report_hash, "
            "markdown_blob_hash, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (rid, overlay_id, int(overlay_revision), report_schema_version,
             _j(report), report_hash, markdown_blob_hash, _now()))
        conn.commit()
    return rid


def latest_report(overlay_id: str, *, overlay_revision: Optional[int] = None
                  ) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        if overlay_revision is not None:
            row = conn.execute(
                "SELECT * FROM git_overlay_reports WHERE overlay_id=? AND "
                "overlay_revision=? ORDER BY created_at DESC LIMIT 1",
                (overlay_id, int(overlay_revision))).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM git_overlay_reports WHERE overlay_id=? "
                "ORDER BY overlay_revision DESC, created_at DESC LIMIT 1",
                (overlay_id,)).fetchone()
    if not row:
        return None
    d = _row(row)
    d["report"] = _load(d.pop("report_json", "{}"), {})
    return d


# ---------------------------------------------------------------------------
# Search-index bookkeeping
# ---------------------------------------------------------------------------
def set_search_index(overlay_id: str, plane: str, chunk_ids: List[str]) -> None:
    conn, lock = _cx()
    with lock:
        conn.execute(
            "INSERT INTO git_overlay_search_index(overlay_id, plane, "
            "chunk_ids_json, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(overlay_id, plane) DO UPDATE SET chunk_ids_json=?, "
            "updated_at=?",
            (overlay_id, plane, json.dumps(list(chunk_ids)), _now(),
             json.dumps(list(chunk_ids)), _now()))
        conn.commit()


def get_search_index(overlay_id: str) -> Dict[str, List[str]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT plane, chunk_ids_json FROM git_overlay_search_index WHERE "
            "overlay_id=?", (overlay_id,)).fetchall()
    return {r["plane"]: _load(r["chunk_ids_json"], []) for r in rows}
