"""Repository over the v0006 canonical-graph tables.

Same placement rationale as :mod:`openmind.semantic.store`: db.py is the
Phase 1–3 store, semantic/store.py the Phase 4 one, and this module the
Phase 5 one — all three run on the SAME shared WAL connection and the SAME
RLock (``db.shared_connection()``), so the graph inherits every concurrency
property the rest of persistence has. No second connection, no second lock
ordering.

THE GRAPH TRANSACTION
---------------------
Every canonical write happens inside :func:`graph_transaction`:

    with graph_transaction(ws, action=RevisionAction.CANDIDATE_PROMOTION,
                           actor=actor) as tx:
        entity = tx.insert_entity(...)
        tx.insert_claim(...)
        tx.insert_decision(...)

The context manager holds the process lock for its whole body, allocates the
workspace's next ``revision_number`` up front (so created rows can be stamped
with it), collects per-kind change counts as writes happen, writes the
``knowledge_revisions`` row LAST, and commits — or rolls the whole body back
on any exception, in which case no revision exists. One logical graph
transaction therefore produces exactly one Knowledge Revision, a failed one
produces none, and concurrent writers serialize on the lock so numbers stay
unique and monotonic.

A transaction that recorded ZERO changes commits nothing and writes no
revision (``tx.wrote`` is False) — that is what makes an unchanged
``graph sync`` an honest no-op.

Workspace scoping is structural: every row carries a validated
``workspace_id`` and every read filters on it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from contextlib import contextmanager

from .. import db
from .vocabularies import (AliasStatus, BindingStatus, GraphLifecycleStatus,
                           RelationState)


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
# Row mappers
# ---------------------------------------------------------------------------
def _entity_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "entity_type": row["entity_type"],
        "canonical_key": row["canonical_key"],
        "display_name": row["display_name"],
        "description": row["description"],
        "lifecycle_status": row["lifecycle_status"],
        "authority_status": row["authority_status"],
        "origin": row["origin"],
        "promoted_from_candidate_id": row["promoted_from_candidate_id"],
        "created_knowledge_revision": row["created_knowledge_revision"],
        "updated_knowledge_revision": row["updated_knowledge_revision"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "superseded_by_entity_id": row["superseded_by_entity_id"],
        "merged_into_entity_id": row["merged_into_entity_id"],
    }


def _alias_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "entity_id": row["entity_id"], "alias": row["alias"],
        "normalized_alias": row["normalized_alias"],
        "alias_type": row["alias_type"], "origin": row["origin"],
        "status": row["status"], "evidence_id": row["evidence_id"],
        "created_knowledge_revision": row["created_knowledge_revision"],
        "created_at": row["created_at"],
    }


def _binding_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "entity_id": row["entity_id"], "ref_kind": row["ref_kind"],
        "ref_id": row["ref_id"], "ref_key": row["ref_key"],
        "binding_role": row["binding_role"], "status": row["status"],
        "origin": row["origin"], "evidence_id": row["evidence_id"],
        "created_knowledge_revision": row["created_knowledge_revision"],
        "updated_knowledge_revision": row["updated_knowledge_revision"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
    }


def _claim_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "entity_id": row["entity_id"], "claim_type": row["claim_type"],
        "statement": row["statement"],
        "normalized_statement_hash": row["normalized_statement_hash"],
        "lifecycle_status": row["lifecycle_status"],
        "authority_status": row["authority_status"],
        "origin": row["origin"],
        "promoted_from_candidate_id": row["promoted_from_candidate_id"],
        "created_knowledge_revision": row["created_knowledge_revision"],
        "updated_knowledge_revision": row["updated_knowledge_revision"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "superseded_by_claim_id": row["superseded_by_claim_id"],
    }


def _relation_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "source_entity_id": row["source_entity_id"],
        "target_entity_id": row["target_entity_id"],
        "relation_type": row["relation_type"],
        "relation_state": row["relation_state"],
        "confidence": row["confidence"],
        "lifecycle_status": row["lifecycle_status"],
        "authority_status": row["authority_status"],
        "origin": row["origin"],
        "promoted_from_relation_candidate_id":
            row["promoted_from_relation_candidate_id"],
        "created_knowledge_revision": row["created_knowledge_revision"],
        "updated_knowledge_revision": row["updated_knowledge_revision"],
        "metadata": _load(row["metadata_json"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stale_at": row["stale_at"],
        "superseded_by_relation_id": row["superseded_by_relation_id"],
    }


def _decision_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "knowledge_revision_id": row["knowledge_revision_id"],
        "decision_type": row["decision_type"],
        "target_kind": row["target_kind"], "target_id": row["target_id"],
        "actor": row["actor"], "note": row["note"],
        "before": _load(row["before_json"], {}),
        "after": _load(row["after_json"], {}),
        "source_command": row["source_command"],
        "created_at": row["created_at"],
    }


def _revision_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "revision_number": row["revision_number"],
        "parent_revision_number": row["parent_revision_number"],
        "change_set_id": row["change_set_id"], "action": row["action"],
        "summary": row["summary"], "actor": row["actor"],
        "object_counts": _load(row["object_counts_json"], {}),
        "created_at": row["created_at"],
    }


def _promotion_row(row) -> Dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"],
        "candidate_kind": row["candidate_kind"],
        "candidate_id": row["candidate_id"],
        "target_kind": row["target_kind"], "target_id": row["target_id"],
        "status": row["status"], "policy_version": row["policy_version"],
        "actor": row["actor"], "note": row["note"],
        "knowledge_revision_id": row["knowledge_revision_id"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# The graph transaction
# ---------------------------------------------------------------------------
class GraphTransaction:
    """One canonical graph write: everything or nothing, one revision.

    Only usable inside :func:`graph_transaction`, which holds the process
    lock for the whole body. Insert/update helpers write through the shared
    connection WITHOUT committing; the context manager commits once.
    """

    def __init__(self, conn, workspace_id: str, *, action: str, actor: str,
                 summary: str, change_set_id: str) -> None:
        self._conn = conn
        self.workspace_id = workspace_id
        self.action = action
        self.actor = actor
        self.summary = summary
        self.change_set_id = change_set_id or db.new_id("cs_")
        self.ts = _now()
        row = conn.execute(
            "SELECT COALESCE(MAX(revision_number),0) FROM knowledge_revisions "
            "WHERE workspace_id=?", (workspace_id,)).fetchone()
        self.parent_revision = int(row[0])
        self.revision_number = self.parent_revision + 1
        self.revision_id = db.new_id("kr_")
        self.counts: Dict[str, int] = {}
        self.wrote = False

    def _bump(self, kind: str, n: int = 1) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + n
        self.wrote = True

    # -- entities -----------------------------------------------------------
    def insert_entity(self, *, entity_type: str, canonical_key: str,
                      display_name: str, description: str = "",
                      origin: str, lifecycle_status: str = "active",
                      authority_status: str = "unknown",
                      promoted_from_candidate_id: str = "",
                      metadata: Optional[Dict[str, Any]] = None,
                      entity_id: str = "") -> Dict[str, Any]:
        eid = entity_id or db.new_id("ent_")
        self._conn.execute(
            "INSERT INTO engineering_entities (id,workspace_id,entity_type,"
            "canonical_key,display_name,description,lifecycle_status,"
            "authority_status,origin,promoted_from_candidate_id,"
            "created_knowledge_revision,updated_knowledge_revision,"
            "metadata_json,created_at,updated_at,stale_at,"
            "superseded_by_entity_id,merged_into_entity_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, self.workspace_id, entity_type, canonical_key,
             display_name, description, lifecycle_status, authority_status,
             origin, promoted_from_candidate_id, self.revision_number,
             self.revision_number, _j(metadata or {}), self.ts, self.ts,
             None, None, None))
        self._bump("entities")
        return self.get_entity(eid)

    def update_entity(self, entity_id: str, **fields: Any) -> None:
        self._update("engineering_entities", entity_id, fields,
                     count_kind="entities")

    def get_entity(self, entity_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM engineering_entities WHERE id=? AND workspace_id=?",
            (entity_id, self.workspace_id)).fetchone()
        return _entity_row(row)

    # -- aliases ------------------------------------------------------------
    def insert_alias(self, *, entity_id: str, alias: str,
                     normalized_alias: str, alias_type: str, origin: str,
                     status: str = AliasStatus.ACTIVE,
                     evidence_id: str = "") -> Dict[str, Any]:
        aid = db.new_id("al_")
        self._conn.execute(
            "INSERT INTO engineering_entity_aliases (id,workspace_id,"
            "entity_id,alias,normalized_alias,alias_type,origin,status,"
            "evidence_id,created_knowledge_revision,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.workspace_id, entity_id, alias, normalized_alias,
             alias_type, origin, status, evidence_id, self.revision_number,
             self.ts))
        self._bump("aliases")
        row = self._conn.execute(
            "SELECT * FROM engineering_entity_aliases WHERE id=?",
            (aid,)).fetchone()
        return _alias_row(row)

    def update_alias(self, alias_id: str, **fields: Any) -> None:
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key}=?")
            args.append(value)
        args.extend([alias_id, self.workspace_id])
        self._conn.execute(
            f"UPDATE engineering_entity_aliases SET {', '.join(sets)} "
            f"WHERE id=? AND workspace_id=?", args)
        self._bump("aliases")

    # -- bindings -----------------------------------------------------------
    def insert_binding(self, *, entity_id: str, ref_kind: str, ref_id: str,
                       ref_key: str = "", binding_role: str = "supporting",
                       origin: str, status: str = BindingStatus.ACTIVE,
                       evidence_id: str = "") -> Dict[str, Any]:
        bid = db.new_id("bd_")
        self._conn.execute(
            "INSERT INTO engineering_entity_bindings (id,workspace_id,"
            "entity_id,ref_kind,ref_id,ref_key,binding_role,status,origin,"
            "evidence_id,created_knowledge_revision,"
            "updated_knowledge_revision,created_at,updated_at,stale_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, self.workspace_id, entity_id, ref_kind, ref_id, ref_key,
             binding_role, status, origin, evidence_id, self.revision_number,
             self.revision_number, self.ts, self.ts, None))
        self._bump("bindings")
        row = self._conn.execute(
            "SELECT * FROM engineering_entity_bindings WHERE id=?",
            (bid,)).fetchone()
        return _binding_row(row)

    def update_binding(self, binding_id: str, **fields: Any) -> None:
        self._update("engineering_entity_bindings", binding_id, fields,
                     count_kind="bindings")

    # -- claims -------------------------------------------------------------
    def insert_claim(self, *, entity_id: str, claim_type: str,
                     statement: str, normalized_statement_hash: str,
                     origin: str, lifecycle_status: str = "active",
                     authority_status: str = "unknown",
                     promoted_from_candidate_id: str = "",
                     metadata: Optional[Dict[str, Any]] = None,
                     evidence: Sequence[Dict[str, Any]] = ()
                     ) -> Dict[str, Any]:
        cid = db.new_id("clm_")
        self._conn.execute(
            "INSERT INTO engineering_claims (id,workspace_id,entity_id,"
            "claim_type,statement,normalized_statement_hash,lifecycle_status,"
            "authority_status,origin,promoted_from_candidate_id,"
            "created_knowledge_revision,updated_knowledge_revision,"
            "metadata_json,created_at,updated_at,stale_at,"
            "superseded_by_claim_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, self.workspace_id, entity_id, claim_type, statement,
             normalized_statement_hash, lifecycle_status, authority_status,
             origin, promoted_from_candidate_id, self.revision_number,
             self.revision_number, _j(metadata or {}), self.ts, self.ts,
             None, None))
        for ev in evidence:
            self._conn.execute(
                "INSERT OR IGNORE INTO engineering_claim_evidence "
                "(claim_id,evidence_id,role,quote,quote_hash) "
                "VALUES (?,?,?,?,?)",
                (cid, ev["evidence_id"], ev.get("role", "primary"),
                 ev.get("quote", ""), ev.get("quote_hash", "")))
        self._bump("claims")
        row = self._conn.execute(
            "SELECT * FROM engineering_claims WHERE id=?", (cid,)).fetchone()
        return _claim_row(row)

    def add_claim_evidence(self, claim_id: str,
                           evidence: Sequence[Dict[str, Any]]) -> None:
        for ev in evidence:
            self._conn.execute(
                "INSERT OR IGNORE INTO engineering_claim_evidence "
                "(claim_id,evidence_id,role,quote,quote_hash) "
                "VALUES (?,?,?,?,?)",
                (claim_id, ev["evidence_id"], ev.get("role", "primary"),
                 ev.get("quote", ""), ev.get("quote_hash", "")))

    def update_claim(self, claim_id: str, **fields: Any) -> None:
        self._update("engineering_claims", claim_id, fields,
                     count_kind="claims")

    def move_claim(self, claim_id: str, new_entity_id: str) -> None:
        self._conn.execute(
            "UPDATE engineering_claims SET entity_id=?, "
            "updated_knowledge_revision=?, updated_at=? "
            "WHERE id=? AND workspace_id=?",
            (new_entity_id, self.revision_number, self.ts, claim_id,
             self.workspace_id))
        self._bump("claims")

    # -- relations ----------------------------------------------------------
    def insert_relation(self, *, source_entity_id: str,
                        target_entity_id: str, relation_type: str,
                        relation_state: str, confidence: str = "low",
                        origin: str, lifecycle_status: str = "active",
                        authority_status: str = "unknown",
                        promoted_from_relation_candidate_id: str = "",
                        metadata: Optional[Dict[str, Any]] = None,
                        evidence: Sequence[Dict[str, Any]] = ()
                        ) -> Dict[str, Any]:
        rid = db.new_id("rel_")
        self._conn.execute(
            "INSERT INTO engineering_relations (id,workspace_id,"
            "source_entity_id,target_entity_id,relation_type,relation_state,"
            "confidence,lifecycle_status,authority_status,origin,"
            "promoted_from_relation_candidate_id,created_knowledge_revision,"
            "updated_knowledge_revision,metadata_json,created_at,updated_at,"
            "stale_at,superseded_by_relation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, self.workspace_id, source_entity_id, target_entity_id,
             relation_type, relation_state, confidence, lifecycle_status,
             authority_status, origin, promoted_from_relation_candidate_id,
             self.revision_number, self.revision_number, _j(metadata or {}),
             self.ts, self.ts, None, None))
        for ev in evidence:
            self._conn.execute(
                "INSERT OR IGNORE INTO engineering_relation_evidence "
                "(relation_id,evidence_id,role,quote,quote_hash) "
                "VALUES (?,?,?,?,?)",
                (rid, ev["evidence_id"], ev.get("role", "primary"),
                 ev.get("quote", ""), ev.get("quote_hash", "")))
        self._bump("relations")
        row = self._conn.execute(
            "SELECT * FROM engineering_relations WHERE id=?",
            (rid,)).fetchone()
        return _relation_row(row)

    def update_relation(self, relation_id: str, **fields: Any) -> None:
        self._update("engineering_relations", relation_id, fields,
                     count_kind="relations")

    def add_relation_evidence(self, relation_id: str,
                              evidence: Sequence[Dict[str, Any]]) -> None:
        for ev in evidence:
            self._conn.execute(
                "INSERT OR IGNORE INTO engineering_relation_evidence "
                "(relation_id,evidence_id,role,quote,quote_hash) "
                "VALUES (?,?,?,?,?)",
                (relation_id, ev["evidence_id"], ev.get("role", "primary"),
                 ev.get("quote", ""), ev.get("quote_hash", "")))

    # -- decisions / promotions --------------------------------------------
    def insert_decision(self, *, decision_type: str, target_kind: str,
                        target_id: str, actor: str = "", note: str = "",
                        before: Optional[Dict[str, Any]] = None,
                        after: Optional[Dict[str, Any]] = None,
                        source_command: str = "") -> Dict[str, Any]:
        did = db.new_id("dec_")
        self._conn.execute(
            "INSERT INTO knowledge_decisions (id,workspace_id,"
            "knowledge_revision_id,decision_type,target_kind,target_id,"
            "actor,note,before_json,after_json,source_command,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, self.workspace_id, self.revision_id, decision_type,
             target_kind, target_id, actor, note, _j(before or {}),
             _j(after or {}), source_command, self.ts))
        self._bump("decisions")
        row = self._conn.execute(
            "SELECT * FROM knowledge_decisions WHERE id=?", (did,)).fetchone()
        return _decision_row(row)

    def insert_promotion(self, *, candidate_kind: str, candidate_id: str,
                         target_kind: str, target_id: str, status: str,
                         policy_version: str, actor: str = "",
                         note: str = "") -> Dict[str, Any]:
        pid = db.new_id("pro_")
        self._conn.execute(
            "INSERT INTO knowledge_promotions (id,workspace_id,"
            "candidate_kind,candidate_id,target_kind,target_id,status,"
            "policy_version,actor,note,knowledge_revision_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, self.workspace_id, candidate_kind, candidate_id,
             target_kind, target_id, status, policy_version, actor, note,
             self.revision_id, self.ts))
        self._bump("promotions")
        row = self._conn.execute(
            "SELECT * FROM knowledge_promotions WHERE id=?",
            (pid,)).fetchone()
        return _promotion_row(row)

    def execute_counted(self, sql: str, args: Sequence[Any],
                        kind: str) -> int:
        """Run one bulk UPDATE inside the transaction, counting affected rows
        under *kind*. Zero affected rows records no change. For the indexed
        set-based staleness reconciliation, where per-row helpers would be
        O(rows) round trips."""
        cur = self._conn.execute(sql, args)
        affected = max(0, cur.rowcount)
        if affected:
            self._bump(kind, affected)
        return affected

    def set_projection_state(self, *, projector_version: str,
                             source_knowledge_hash: str) -> None:
        self._conn.execute(
            "INSERT INTO knowledge_projection_state (workspace_id,"
            "projector_version,source_knowledge_hash,"
            "last_knowledge_revision,last_synced_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET "
            "projector_version=excluded.projector_version, "
            "source_knowledge_hash=excluded.source_knowledge_hash, "
            "last_knowledge_revision=excluded.last_knowledge_revision, "
            "last_synced_at=excluded.last_synced_at",
            (self.workspace_id, projector_version, source_knowledge_hash,
             self.revision_number, self.ts))
        # A watermark refresh alone is not a graph change: no bump.

    # -- internals ----------------------------------------------------------
    def _update(self, table: str, row_id: str, fields: Dict[str, Any],
                *, count_kind: str) -> None:
        if not fields:
            return
        sets, args = [], []
        for key, value in fields.items():
            if key == "metadata":
                key, value = "metadata_json", _j(value)
            sets.append(f"{key}=?")
            args.append(value)
        sets.append("updated_knowledge_revision=?")
        args.append(self.revision_number)
        sets.append("updated_at=?")
        args.append(self.ts)
        args.extend([row_id, self.workspace_id])
        self._conn.execute(
            f"UPDATE {table} SET {', '.join(sets)} "
            f"WHERE id=? AND workspace_id=?", args)
        self._bump(count_kind)

    def _finalize(self) -> None:
        """Write the revision row (called by the context manager on a body
        that recorded at least one change, right before commit)."""
        self._conn.execute(
            "INSERT INTO knowledge_revisions (id,workspace_id,"
            "revision_number,parent_revision_number,change_set_id,action,"
            "summary,actor,object_counts_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.revision_id, self.workspace_id, self.revision_number,
             self.parent_revision, self.change_set_id, self.action,
             self.summary[:2000], self.actor[:200], _j(self.counts),
             self.ts))


@contextmanager
def graph_transaction(workspace_id: str, *, action: str, actor: str = "",
                      summary: str = "",
                      change_set_id: str = "") -> Iterator[GraphTransaction]:
    """One canonical graph transaction (see the module docstring).

    Commit rules: body raised → full rollback, no revision; body recorded no
    change → rollback (nothing to keep), no revision; otherwise the revision
    row is written last and everything commits together.
    """
    conn, lock = _cx()
    with lock:
        tx = GraphTransaction(conn, workspace_id, action=action, actor=actor,
                              summary=summary, change_set_id=change_set_id)
        try:
            yield tx
        except Exception:
            conn.rollback()
            raise
        if not tx.wrote:
            conn.rollback()
            return
        tx._finalize()
        conn.commit()


# ---------------------------------------------------------------------------
# Reads (each takes the lock for itself)
# ---------------------------------------------------------------------------
def get_entity(workspace_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_entities WHERE id=? AND workspace_id=?",
            (entity_id, workspace_id)).fetchone()
    return _entity_row(row) if row else None


def find_entity_by_key(workspace_id: str, entity_type: str,
                       canonical_key: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_entities WHERE workspace_id=? AND "
            "entity_type=? AND canonical_key=?",
            (workspace_id, entity_type, canonical_key)).fetchone()
    return _entity_row(row) if row else None


def list_entities(workspace_id: str, *, entity_type: Optional[str] = None,
                  lifecycle_status: Optional[str] = None,
                  authority_status: Optional[str] = None,
                  origin: Optional[str] = None,
                  key_prefix: Optional[str] = None,
                  limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM engineering_entities WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("entity_type", entity_type),
                     ("lifecycle_status", lifecycle_status),
                     ("authority_status", authority_status),
                     ("origin", origin)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    if key_prefix:
        q += " AND canonical_key LIKE ?"
        args.append(key_prefix.replace("%", "").replace("_", "\\_") + "%")
    q += " ORDER BY canonical_key, id LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_entity_row(r) for r in rows]


def count_entities(workspace_id: str, **filters: Optional[str]) -> int:
    q = "SELECT COUNT(*) FROM engineering_entities WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col in ("entity_type", "lifecycle_status", "authority_status",
                "origin"):
        val = filters.get(col)
        if val:
            q += f" AND {col}=?"
            args.append(val)
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def list_aliases(workspace_id: str, entity_id: str,
                 status: Optional[str] = AliasStatus.ACTIVE
                 ) -> List[Dict[str, Any]]:
    q = ("SELECT * FROM engineering_entity_aliases WHERE workspace_id=? "
         "AND entity_id=?")
    args: List[Any] = [workspace_id, entity_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY normalized_alias, id"
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_alias_row(r) for r in rows]


def find_alias_holders(workspace_id: str, normalized_alias: str,
                       exclude_entity_id: str = "") -> List[Dict[str, Any]]:
    """ACTIVE aliases with this normalized form held by ACTIVE entities —
    the collision-detection read."""
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT al.*, e.canonical_key AS holder_key, "
            "e.display_name AS holder_name FROM engineering_entity_aliases al "
            "JOIN engineering_entities e ON al.entity_id = e.id "
            "WHERE al.workspace_id=? AND al.normalized_alias=? AND "
            "al.status='active' AND e.lifecycle_status='active'",
            (workspace_id, normalized_alias)).fetchall()
    out = []
    for r in rows:
        if exclude_entity_id and r["entity_id"] == exclude_entity_id:
            continue
        rec = _alias_row(r)
        rec["holder_canonical_key"] = r["holder_key"]
        rec["holder_display_name"] = r["holder_name"]
        out.append(rec)
    return out


def find_entity_by_alias(workspace_id: str,
                         normalized_alias: str) -> List[Dict[str, Any]]:
    """Active entities holding this exact normalized alias."""
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT e.* FROM engineering_entities e "
            "JOIN engineering_entity_aliases al ON al.entity_id = e.id "
            "WHERE e.workspace_id=? AND al.normalized_alias=? AND "
            "al.status='active' AND e.lifecycle_status='active' "
            "ORDER BY e.canonical_key",
            (workspace_id, normalized_alias)).fetchall()
    return [_entity_row(r) for r in rows]


def list_bindings(workspace_id: str, entity_id: str,
                  status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = ("SELECT * FROM engineering_entity_bindings WHERE workspace_id=? "
         "AND entity_id=?")
    args: List[Any] = [workspace_id, entity_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY ref_kind, ref_id, id"
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_binding_row(r) for r in rows]


def find_bindings_by_ref(workspace_id: str, ref_kind: str,
                         ref_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM engineering_entity_bindings WHERE workspace_id=? "
            "AND ref_kind=? AND ref_id=? ORDER BY id",
            (workspace_id, ref_kind, ref_id)).fetchall()
    return [_binding_row(r) for r in rows]


def list_workspace_bindings(workspace_id: str, status: Optional[str] = None,
                            limit: int = 200_000) -> List[Dict[str, Any]]:
    """Every binding of a workspace in one query — the projector's and the
    reconciler's bulk read (per-entity queries would be O(entities))."""
    q = "SELECT * FROM engineering_entity_bindings WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY entity_id, ref_kind, ref_id LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_binding_row(r) for r in rows]


def get_claim(workspace_id: str, claim_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_claims WHERE id=? AND workspace_id=?",
            (claim_id, workspace_id)).fetchone()
        if not row:
            return None
        evidence = conn.execute(
            "SELECT * FROM engineering_claim_evidence WHERE claim_id=? "
            "ORDER BY evidence_id, quote_hash", (claim_id,)).fetchall()
    out = _claim_row(row)
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    return out


def list_claims(workspace_id: str, *, entity_id: Optional[str] = None,
                claim_type: Optional[str] = None,
                lifecycle_status: Optional[str] = None,
                origin: Optional[str] = None,
                limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM engineering_claims WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("entity_id", entity_id), ("claim_type", claim_type),
                     ("lifecycle_status", lifecycle_status),
                     ("origin", origin)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at, id LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_claim_row(r) for r in rows]


def count_claims(workspace_id: str, **filters: Optional[str]) -> int:
    q = "SELECT COUNT(*) FROM engineering_claims WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col in ("entity_id", "claim_type", "lifecycle_status", "origin"):
        val = filters.get(col)
        if val:
            q += f" AND {col}=?"
            args.append(val)
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def find_claim_by_hash(workspace_id: str, entity_id: str,
                       normalized_statement_hash: str,
                       lifecycle_status: str = GraphLifecycleStatus.ACTIVE
                       ) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_claims WHERE workspace_id=? AND "
            "entity_id=? AND normalized_statement_hash=? AND "
            "lifecycle_status=? ORDER BY created_at LIMIT 1",
            (workspace_id, entity_id, normalized_statement_hash,
             lifecycle_status)).fetchone()
    return _claim_row(row) if row else None


def claim_evidence(claim_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM engineering_claim_evidence WHERE claim_id=? "
            "ORDER BY evidence_id, quote_hash", (claim_id,)).fetchall()
    return [{"evidence_id": e["evidence_id"], "role": e["role"],
             "quote": e["quote"], "quote_hash": e["quote_hash"]}
            for e in rows]


def get_relation(workspace_id: str,
                 relation_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_relations WHERE id=? AND "
            "workspace_id=?", (relation_id, workspace_id)).fetchone()
        if not row:
            return None
        evidence = conn.execute(
            "SELECT * FROM engineering_relation_evidence WHERE relation_id=? "
            "ORDER BY evidence_id, quote_hash", (relation_id,)).fetchall()
    out = _relation_row(row)
    out["evidence"] = [{"evidence_id": e["evidence_id"], "role": e["role"],
                        "quote": e["quote"], "quote_hash": e["quote_hash"]}
                       for e in evidence]
    return out


def list_relations(workspace_id: str, *,
                   source_entity_id: Optional[str] = None,
                   target_entity_id: Optional[str] = None,
                   entity_id: Optional[str] = None,
                   relation_type: Optional[str] = None,
                   relation_state: Optional[str] = None,
                   lifecycle_status: Optional[str] = None,
                   origin: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM engineering_relations WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    if entity_id:
        q += " AND (source_entity_id=? OR target_entity_id=?)"
        args.extend([entity_id, entity_id])
    for col, val in (("source_entity_id", source_entity_id),
                     ("target_entity_id", target_entity_id),
                     ("relation_type", relation_type),
                     ("relation_state", relation_state),
                     ("lifecycle_status", lifecycle_status),
                     ("origin", origin)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY relation_type, source_entity_id, target_entity_id, id " \
         "LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_relation_row(r) for r in rows]


def count_relations(workspace_id: str, **filters: Optional[str]) -> int:
    q = "SELECT COUNT(*) FROM engineering_relations WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col in ("relation_type", "relation_state", "lifecycle_status",
                "origin"):
        val = filters.get(col)
        if val:
            q += f" AND {col}=?"
            args.append(val)
    conn, lock = _cx()
    with lock:
        row = conn.execute(q, args).fetchone()
    return int(row[0]) if row else 0


def find_active_relation(workspace_id: str, source_entity_id: str,
                         target_entity_id: str,
                         relation_type: str) -> Optional[Dict[str, Any]]:
    """The active-identity lookup: at most one ACTIVE relation per
    (workspace, source, target, type)."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM engineering_relations WHERE workspace_id=? AND "
            "source_entity_id=? AND target_entity_id=? AND relation_type=? "
            "AND lifecycle_status='active' ORDER BY created_at LIMIT 1",
            (workspace_id, source_entity_id, target_entity_id,
             relation_type)).fetchone()
    return _relation_row(row) if row else None


def relation_evidence(relation_id: str) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM engineering_relation_evidence WHERE relation_id=? "
            "ORDER BY evidence_id, quote_hash", (relation_id,)).fetchall()
    return [{"evidence_id": e["evidence_id"], "role": e["role"],
             "quote": e["quote"], "quote_hash": e["quote_hash"]}
            for e in rows]


def _like_escape(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def search_entities_lexical(workspace_id: str, query: str,
                            *, lifecycle_status: Optional[str] = "active",
                            limit: int = 50) -> List[Dict[str, Any]]:
    """Case-insensitive substring match over canonical key and display name.
    Deterministic ordering; token-precision ranking happens in search.py."""
    pattern = f"%{_like_escape(query)}%"
    q = ("SELECT * FROM engineering_entities WHERE workspace_id=? AND "
         "(canonical_key LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
         "display_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
         "description LIKE ? ESCAPE '\\' COLLATE NOCASE)")
    args: List[Any] = [workspace_id, pattern, pattern, pattern]
    if lifecycle_status:
        q += " AND lifecycle_status=?"
        args.append(lifecycle_status)
    q += " ORDER BY canonical_key, id LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_entity_row(r) for r in rows]


def search_claims_lexical(workspace_id: str, query: str,
                          *, lifecycle_status: Optional[str] = "active",
                          limit: int = 50) -> List[Dict[str, Any]]:
    pattern = f"%{_like_escape(query)}%"
    q = ("SELECT * FROM engineering_claims WHERE workspace_id=? AND "
         "statement LIKE ? ESCAPE '\\' COLLATE NOCASE")
    args: List[Any] = [workspace_id, pattern]
    if lifecycle_status:
        q += " AND lifecycle_status=?"
        args.append(lifecycle_status)
    q += " ORDER BY created_at, id LIMIT ?"
    args.append(max(0, int(limit)))
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_claim_row(r) for r in rows]


# -- ledger reads -----------------------------------------------------------
def current_revision_number(workspace_id: str) -> int:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision_number),0) FROM knowledge_revisions "
            "WHERE workspace_id=?", (workspace_id,)).fetchone()
    return int(row[0])


def list_revisions(workspace_id: str, limit: int = 50,
                   offset: int = 0) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM knowledge_revisions WHERE workspace_id=? "
            "ORDER BY revision_number DESC LIMIT ? OFFSET ?",
            (workspace_id, max(0, int(limit)),
             max(0, int(offset)))).fetchall()
    return [_revision_row(r) for r in rows]


def get_revision_by_number(workspace_id: str,
                           revision_number: int) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM knowledge_revisions WHERE workspace_id=? AND "
            "revision_number=?",
            (workspace_id, int(revision_number))).fetchone()
    return _revision_row(row) if row else None


def list_decisions(workspace_id: str, *, target_kind: Optional[str] = None,
                   target_id: Optional[str] = None,
                   decision_type: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM knowledge_decisions WHERE workspace_id=?"
    args: List[Any] = [workspace_id]
    for col, val in (("target_kind", target_kind), ("target_id", target_id),
                     ("decision_type", decision_type)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    q += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([max(0, int(limit)), max(0, int(offset))])
    conn, lock = _cx()
    with lock:
        rows = conn.execute(q, args).fetchall()
    return [_decision_row(r) for r in rows]


def find_promotion(workspace_id: str, candidate_kind: str,
                   candidate_id: str) -> Optional[Dict[str, Any]]:
    """The PROMOTED record for one candidate, if promotion ever happened."""
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM knowledge_promotions WHERE workspace_id=? AND "
            "candidate_kind=? AND candidate_id=? AND status='promoted' "
            "ORDER BY created_at LIMIT 1",
            (workspace_id, candidate_kind, candidate_id)).fetchone()
    return _promotion_row(row) if row else None


def list_promotions(workspace_id: str, limit: int = 100,
                    offset: int = 0) -> List[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT * FROM knowledge_promotions WHERE workspace_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (workspace_id, max(0, int(limit)),
             max(0, int(offset)))).fetchall()
    return [_promotion_row(r) for r in rows]


def get_projection_state(workspace_id: str) -> Optional[Dict[str, Any]]:
    conn, lock = _cx()
    with lock:
        row = conn.execute(
            "SELECT * FROM knowledge_projection_state WHERE workspace_id=?",
            (workspace_id,)).fetchone()
    if not row:
        return None
    return {"workspace_id": row["workspace_id"],
            "projector_version": row["projector_version"],
            "source_knowledge_hash": row["source_knowledge_hash"],
            "last_knowledge_revision": row["last_knowledge_revision"],
            "last_synced_at": row["last_synced_at"]}


def touch_projection_state(workspace_id: str, *, projector_version: str,
                           source_knowledge_hash: str) -> None:
    """Refresh the projection watermark WITHOUT a graph transaction — the
    unchanged-sync path (and the empty first seed). A watermark refresh is
    not a graph change, so it must not mint a Knowledge Revision."""
    conn, lock = _cx()
    with lock:
        last = conn.execute(
            "SELECT COALESCE(MAX(revision_number),0) FROM knowledge_revisions "
            "WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO knowledge_projection_state (workspace_id,"
            "projector_version,source_knowledge_hash,"
            "last_knowledge_revision,last_synced_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET "
            "projector_version=excluded.projector_version, "
            "source_knowledge_hash=excluded.source_knowledge_hash, "
            "last_knowledge_revision=excluded.last_knowledge_revision, "
            "last_synced_at=excluded.last_synced_at",
            (workspace_id, projector_version, source_knowledge_hash,
             int(last), _now()))
        conn.commit()


def stats(workspace_id: str) -> Dict[str, Any]:
    """Aggregate graph counts, all scoped, plus the current revision."""
    conn, lock = _cx()
    with lock:
        c = conn
        by_type = c.execute(
            "SELECT entity_type, COUNT(*) AS n FROM engineering_entities "
            "WHERE workspace_id=? AND lifecycle_status='active' "
            "GROUP BY entity_type", (workspace_id,)).fetchall()
        ent_total = c.execute(
            "SELECT COUNT(*) FROM engineering_entities WHERE workspace_id=?",
            (workspace_id,)).fetchone()[0]
        ent_active = c.execute(
            "SELECT COUNT(*) FROM engineering_entities WHERE workspace_id=? "
            "AND lifecycle_status='active'", (workspace_id,)).fetchone()[0]
        claims_active = c.execute(
            "SELECT COUNT(*) FROM engineering_claims WHERE workspace_id=? "
            "AND lifecycle_status='active'", (workspace_id,)).fetchone()[0]
        claims_total = c.execute(
            "SELECT COUNT(*) FROM engineering_claims WHERE workspace_id=?",
            (workspace_id,)).fetchone()[0]
        rel_active = c.execute(
            "SELECT COUNT(*) FROM engineering_relations WHERE workspace_id=? "
            "AND lifecycle_status='active'", (workspace_id,)).fetchone()[0]
        rel_total = c.execute(
            "SELECT COUNT(*) FROM engineering_relations WHERE workspace_id=?",
            (workspace_id,)).fetchone()[0]
        aliases = c.execute(
            "SELECT COUNT(*) FROM engineering_entity_aliases WHERE "
            "workspace_id=? AND status='active'",
            (workspace_id,)).fetchone()[0]
        bindings = c.execute(
            "SELECT COUNT(*) FROM engineering_entity_bindings WHERE "
            "workspace_id=? AND status='active'",
            (workspace_id,)).fetchone()[0]
        decisions = c.execute(
            "SELECT COUNT(*) FROM knowledge_decisions WHERE workspace_id=?",
            (workspace_id,)).fetchone()[0]
        promotions = c.execute(
            "SELECT COUNT(*) FROM knowledge_promotions WHERE workspace_id=? "
            "AND status='promoted'", (workspace_id,)).fetchone()[0]
        revision = c.execute(
            "SELECT COALESCE(MAX(revision_number),0) FROM knowledge_revisions "
            "WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
    return {
        "entities_active": int(ent_active), "entities_total": int(ent_total),
        "entities_by_type": {r["entity_type"]: int(r["n"]) for r in by_type},
        "claims_active": int(claims_active),
        "claims_total": int(claims_total),
        "relations_active": int(rel_active),
        "relations_total": int(rel_total),
        "aliases_active": int(aliases), "bindings_active": int(bindings),
        "decisions": int(decisions), "promotions": int(promotions),
        "knowledge_revision": int(revision),
    }


def clear_workspace_graph(workspace_id: str) -> None:
    """Wipe every canonical graph row of a workspace. Used by TERMINATE
    (full wipe of learned data back to init) and by project delete. Entity
    deletion cascades to aliases, bindings, claims, relations and evidence
    joins via the v0006 foreign keys; the ledger tables are removed
    explicitly."""
    conn, lock = _cx()
    with lock:
        try:
            conn.execute(
                "DELETE FROM engineering_entities WHERE workspace_id=?",
                (workspace_id,))
            for table in ("knowledge_decisions", "knowledge_revisions",
                          "knowledge_promotions",
                          "knowledge_projection_state"):
                conn.execute(f"DELETE FROM {table} WHERE workspace_id=?",
                             (workspace_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Staleness support reads (used by reconciliation.py)
# ---------------------------------------------------------------------------
def current_revision_ids(workspace_id: str) -> frozenset:
    """The workspace's set of current revision ids (active assets)."""
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT current_revision_id FROM assets WHERE workspace_id=? "
            "AND current_revision_id IS NOT NULL",
            (workspace_id,)).fetchall()
    return frozenset(r[0] for r in rows)


def active_bindings_on_noncurrent_revisions(workspace_id: str
                                            ) -> List[Dict[str, Any]]:
    """ACTIVE bindings whose ref is a revision that is no longer any active
    asset's current revision — the staleness frontier, one indexed query."""
    conn, lock = _cx()
    with lock:
        rows = conn.execute(
            "SELECT b.* FROM engineering_entity_bindings b "
            "WHERE b.workspace_id=? AND b.status='active' AND "
            "b.ref_kind='revision' AND b.ref_id NOT IN ("
            "SELECT current_revision_id FROM assets WHERE workspace_id=? "
            "AND current_revision_id IS NOT NULL)",
            (workspace_id, workspace_id)).fetchall()
    return [_binding_row(r) for r in rows]


__all__ = [
    "GraphTransaction", "graph_transaction",
    "get_entity", "find_entity_by_key", "list_entities", "count_entities",
    "list_aliases", "find_alias_holders", "find_entity_by_alias",
    "list_bindings", "find_bindings_by_ref", "list_workspace_bindings",
    "get_claim", "list_claims", "count_claims", "find_claim_by_hash",
    "claim_evidence",
    "get_relation", "list_relations", "count_relations",
    "find_active_relation", "relation_evidence",
    "current_revision_number", "list_revisions", "get_revision_by_number",
    "list_decisions", "find_promotion", "list_promotions",
    "get_projection_state", "touch_projection_state", "stats",
    "clear_workspace_graph",
    "current_revision_ids", "active_bindings_on_noncurrent_revisions",
]
