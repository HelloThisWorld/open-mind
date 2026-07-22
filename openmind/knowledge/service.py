"""KnowledgeService — the application service over the canonical graph.

Exposed as ``runtime.knowledge`` / ``ServiceContainer.knowledge`` and shared
by the CLI, REST and (read-only subset) MCP adapters. Every method validates
the workspace first (typed :class:`WorkspaceNotFound`) and then reads/writes
only through the workspace-scoped store — a graph object of workspace A
resolves to nothing through workspace B.

GOVERNANCE DISCIPLINE
---------------------
Every mutating method runs one graph transaction (one Knowledge Revision),
records one Human Decision with the caller-supplied actor (never inferred)
and a bounded note, and refreshes the vector projection AFTER commit —
best-effort, because the projection is derived state and its failure must
never roll back canonical SQLite truth.

The revision ledger and decision records themselves live in
:mod:`openmind.knowledge.store` (the ``GraphTransaction``); this module is
the orchestration and validation layer above them.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..domain.errors import InvalidRequest
from . import (graph, identity, projector, promotion, reconciliation, search,
               store, vector_projection, verifier)
from .errors import (AliasCollision, ClaimNotFound, EntityNotFound,
                     GraphConflict, KnowledgeRevisionNotFound,
                     RelationNotFound)
from .vocabularies import (AliasStatus, AliasType, AuthorityStatus,
                           BindingRefKind, BindingRole, ClaimType,
                           DecisionTargetKind, DecisionType, EntityType,
                           GraphLifecycleStatus, GraphOrigin, RelationState,
                           RelationType, RevisionAction, require)

MAX_NOTE_CHARS = 2_000
MAX_LIST_LIMIT = 500
MAX_STATEMENT_CHARS = promotion.MAX_STATEMENT_CHARS
MAX_DISPLAY_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000


class KnowledgeService:
    """Use cases over the Phase 5 canonical Engineering Knowledge Graph."""

    def __init__(self, workspaces: Any,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._ensure_worker = ensure_worker

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    @staticmethod
    def _bound(limit: Any, hard: int = MAX_LIST_LIMIT) -> int:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = hard
        return max(1, min(limit, hard))

    @staticmethod
    def _note(note: str) -> str:
        note = str(note or "")
        if len(note) > MAX_NOTE_CHARS:
            raise InvalidRequest(
                f"note exceeds {MAX_NOTE_CHARS} characters",
                details={"chars": len(note)})
        return note

    @staticmethod
    def _actor(actor: str) -> str:
        return str(actor or "")[:200]

    def _refresh_projection(self, workspace_id: str) -> None:
        """Best-effort vector refresh after a committed graph change."""
        try:
            vector_projection.refresh_workspace(workspace_id)
        except Exception as exc:
            print(f"[knowledge] vector projection refresh failed for "
                  f"{workspace_id}: {exc}", file=sys.stderr, flush=True)

    def _require_entity(self, workspace_id: str,
                        entity_id: str) -> Dict[str, Any]:
        entity = store.get_entity(workspace_id, entity_id)
        if not entity:
            raise EntityNotFound(entity_id, workspace_id=workspace_id)
        return entity

    def _require_claim(self, workspace_id: str,
                       claim_id: str) -> Dict[str, Any]:
        claim = store.get_claim(workspace_id, claim_id)
        if not claim:
            raise ClaimNotFound(claim_id, workspace_id=workspace_id)
        return claim

    def _require_relation(self, workspace_id: str,
                          relation_id: str) -> Dict[str, Any]:
        relation = store.get_relation(workspace_id, relation_id)
        if not relation:
            raise RelationNotFound(relation_id, workspace_id=workspace_id)
        return relation

    @staticmethod
    def _entity_snapshot(entity: Dict[str, Any]) -> Dict[str, Any]:
        """A bounded before/after snapshot for decision records — graph
        fields only, no free-form payloads, nothing secret to include."""
        return {k: entity.get(k) for k in
                ("id", "entity_type", "canonical_key", "display_name",
                 "lifecycle_status", "authority_status", "origin",
                 "merged_into_entity_id", "superseded_by_entity_id")}

    # ======================================================================
    # Reads
    # ======================================================================
    def get_stats(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        # Active statistics must not count knowledge whose sources moved on.
        reconciliation.reconcile_graph_staleness(workspace_id)
        result = store.stats(workspace_id)
        result["workspace_id"] = workspace_id
        state = store.get_projection_state(workspace_id)
        result["projection"] = state or {"projector_version": "",
                                         "last_synced_at": None}
        return result

    def get_current_revision(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return {"workspace_id": workspace_id,
                "knowledge_revision":
                    store.current_revision_number(workspace_id)}

    def list_entities(self, workspace_id: str, *,
                      entity_type: Optional[str] = None,
                      lifecycle_status: Optional[str] = "active",
                      authority_status: Optional[str] = None,
                      origin: Optional[str] = None,
                      limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if entity_type:
            entity_type = require(EntityType, entity_type,
                                  field="entity type")
        rows = store.list_entities(
            workspace_id, entity_type=entity_type,
            lifecycle_status=lifecycle_status or None,
            authority_status=authority_status, origin=origin,
            limit=self._bound(limit), offset=max(0, int(offset)))
        total = store.count_entities(
            workspace_id, entity_type=entity_type,
            lifecycle_status=lifecycle_status or None,
            authority_status=authority_status, origin=origin)
        return {"workspace_id": workspace_id, "entities": rows,
                "count": len(rows), "total": total,
                "knowledge_revision":
                    store.current_revision_number(workspace_id)}

    def get_entity(self, workspace_id: str, entity_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        out = dict(entity)
        out["aliases"] = store.list_aliases(workspace_id, entity_id,
                                            status=None)
        out["bindings"] = store.list_bindings(workspace_id, entity_id)
        out["claims"] = store.list_claims(workspace_id, entity_id=entity_id,
                                          limit=100)
        out["relations"] = store.list_relations(workspace_id,
                                                entity_id=entity_id,
                                                limit=200)
        out["knowledge_revision"] = store.current_revision_number(
            workspace_id)
        return out

    def list_claims(self, workspace_id: str, *,
                    entity_id: Optional[str] = None,
                    claim_type: Optional[str] = None,
                    lifecycle_status: Optional[str] = "active",
                    limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if claim_type:
            claim_type = require(ClaimType, claim_type, field="claim type")
        rows = store.list_claims(
            workspace_id, entity_id=entity_id, claim_type=claim_type,
            lifecycle_status=lifecycle_status or None,
            limit=self._bound(limit), offset=max(0, int(offset)))
        total = store.count_claims(
            workspace_id, entity_id=entity_id, claim_type=claim_type,
            lifecycle_status=lifecycle_status or None)
        return {"workspace_id": workspace_id, "claims": rows,
                "count": len(rows), "total": total,
                "knowledge_revision":
                    store.current_revision_number(workspace_id)}

    def get_claim(self, workspace_id: str, claim_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        claim = self._require_claim(workspace_id, claim_id)
        claim["knowledge_revision"] = store.current_revision_number(
            workspace_id)
        return claim

    def list_relations(self, workspace_id: str, *,
                       entity_id: Optional[str] = None,
                       relation_type: Optional[str] = None,
                       relation_state: Optional[str] = None,
                       lifecycle_status: Optional[str] = "active",
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if relation_type:
            relation_type = require(RelationType, relation_type,
                                    field="relation type")
        rows = store.list_relations(
            workspace_id, entity_id=entity_id, relation_type=relation_type,
            relation_state=relation_state,
            lifecycle_status=lifecycle_status or None,
            limit=self._bound(limit), offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "relations": rows,
                "count": len(rows),
                "knowledge_revision":
                    store.current_revision_number(workspace_id)}

    def get_relation(self, workspace_id: str,
                     relation_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        relation = self._require_relation(workspace_id, relation_id)
        relation["knowledge_revision"] = store.current_revision_number(
            workspace_id)
        return relation

    def get_node(self, workspace_id: str, node_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return graph.get_node(workspace_id, node_id)

    def expand_node(self, workspace_id: str, node_id: str,
                    **options: Any) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return graph.expand_node(workspace_id, node_id, **options)

    def find_path(self, workspace_id: str, source_id: str, target_id: str,
                  **options: Any) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return graph.find_path(workspace_id, source_id, target_id, **options)

    def get_subgraph(self, workspace_id: str, node_ids: Sequence[str],
                     **options: Any) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return graph.get_subgraph(workspace_id, node_ids, **options)

    def search_entities(self, workspace_id: str, query: str, *,
                        limit: int = 20,
                        include_stale: bool = False) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return search.search_graph(workspace_id, query,
                                   limit=self._bound(limit, 100),
                                   include_stale=include_stale)

    # ======================================================================
    # Promotion
    # ======================================================================
    def plan_candidate_promotion(self, workspace_id: str,
                                 candidate_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return promotion.plan_candidate_promotion(workspace_id, candidate_id)

    def promote_candidate(self, workspace_id: str, candidate_id: str, *,
                          actor: str = "", note: str = "",
                          source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = promotion.promote_candidate(
            workspace_id, candidate_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)
        if result.get("status") == "promoted":
            self._refresh_projection(workspace_id)
        return result

    def plan_relation_promotion(self, workspace_id: str,
                                relation_candidate_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return promotion.plan_relation_promotion(workspace_id,
                                                 relation_candidate_id)

    def promote_relation(self, workspace_id: str,
                         relation_candidate_id: str, *, actor: str = "",
                         note: str = "",
                         source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = promotion.promote_relation(
            workspace_id, relation_candidate_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)
        if result.get("status") == "promoted":
            self._refresh_projection(workspace_id)
        return result

    # ======================================================================
    # Deterministic projection + staleness
    # ======================================================================
    def plan_seed(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return projector.plan_seed(workspace_id)

    def seed(self, workspace_id: str, *, actor: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = projector.seed_graph(workspace_id,
                                      actor=self._actor(actor))
        if result.get("changed"):
            self._refresh_projection(workspace_id)
        return result

    def sync(self, workspace_id: str, *, actor: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = projector.sync_graph(workspace_id,
                                      actor=self._actor(actor))
        if result.get("changed"):
            self._refresh_projection(workspace_id)
        return result

    def reconcile_staleness(self, workspace_id: str, *,
                            actor: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        result = reconciliation.reconcile_graph_staleness(
            workspace_id, actor=self._actor(actor))
        if result.get("changed"):
            self._refresh_projection(workspace_id)
        return result

    # ======================================================================
    # History
    # ======================================================================
    def list_knowledge_revisions(self, workspace_id: str, *,
                                 limit: int = 50,
                                 offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_revisions(workspace_id, limit=self._bound(limit),
                                    offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "revisions": rows,
                "count": len(rows),
                "knowledge_revision":
                    store.current_revision_number(workspace_id)}

    def get_knowledge_revision(self, workspace_id: str,
                               revision_number: int) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_revision_by_number(workspace_id, revision_number)
        if not row:
            raise KnowledgeRevisionNotFound(int(revision_number),
                                            workspace_id=workspace_id)
        row["decisions"] = store.list_decisions(workspace_id, limit=100)
        row["decisions"] = [d for d in row["decisions"]
                            if d["knowledge_revision_id"] == row["id"]]
        return row

    def list_decisions(self, workspace_id: str, *,
                       target_kind: Optional[str] = None,
                       target_id: Optional[str] = None,
                       decision_type: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_decisions(
            workspace_id, target_kind=target_kind, target_id=target_id,
            decision_type=decision_type, limit=self._bound(limit),
            offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "decisions": rows,
                "count": len(rows)}

    def list_promotions(self, workspace_id: str, *,
                        limit: int = 100) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_promotions(workspace_id,
                                     limit=self._bound(limit))
        return {"workspace_id": workspace_id, "promotions": rows,
                "count": len(rows)}

    # ======================================================================
    # Manual creation
    # ======================================================================
    def create_entity(self, workspace_id: str, *, entity_type: str,
                      canonical_key: str, display_name: str,
                      evidence: Sequence[Dict[str, Any]],
                      actor: str, note: str, description: str = "",
                      source_command: str = "") -> Dict[str, Any]:
        """Manual Entity creation. Requires at least one valid Evidence —
        the structural-container exception applies ONLY to deterministic
        projection, never to a manual caller."""
        self._require_workspace(workspace_id)
        entity_type = require(EntityType, entity_type, field="entity type")
        canonical_key = str(canonical_key or "").strip()
        if not canonical_key:
            raise InvalidRequest("canonical_key is required")
        display_name = str(display_name or "").strip()
        if not display_name:
            raise InvalidRequest("display_name is required")
        note = self._note(note)
        rows = verifier.verify_evidence_refs(workspace_id, evidence,
                                             require_nonempty=True)
        existing = store.find_entity_by_key(workspace_id, entity_type,
                                            canonical_key)
        if existing:
            raise GraphConflict(
                f"an entity with canonical key {canonical_key!r} already "
                f"exists: {existing['id']}",
                details={"entity_id": existing["id"]})
        with store.graph_transaction(
                workspace_id, action=RevisionAction.MANUAL_ENTITY_CREATE,
                actor=self._actor(actor),
                summary=f"manual entity {canonical_key}") as tx:
            entity = tx.insert_entity(
                entity_type=entity_type, canonical_key=canonical_key,
                display_name=display_name[:MAX_DISPLAY_NAME_CHARS],
                description=str(description or "")[:MAX_DESCRIPTION_CHARS],
                origin=GraphOrigin.MANUAL)
            for row in rows:
                tx.insert_binding(entity_id=entity["id"],
                                  ref_kind=BindingRefKind.EVIDENCE,
                                  ref_id=row["evidence_id"],
                                  binding_role=BindingRole.SUPPORTING,
                                  origin=GraphOrigin.MANUAL,
                                  evidence_id=row["evidence_id"])
            tx.insert_decision(
                decision_type=DecisionType.CREATE_ENTITY,
                target_kind=DecisionTargetKind.ENTITY,
                target_id=entity["id"], actor=self._actor(actor), note=note,
                after=self._entity_snapshot(entity),
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id,
                "entity": store.get_entity(workspace_id, entity["id"]),
                "knowledge_revision": revision}

    def create_claim(self, workspace_id: str, *, entity_id: str,
                     claim_type: str, statement: str,
                     evidence: Sequence[Dict[str, Any]],
                     actor: str, note: str,
                     source_command: str = "") -> Dict[str, Any]:
        """Manual Claim creation. Evidence is mandatory; quotes must verify
        against the immutable store; an identical normalized statement on
        the same entity deduplicates to the existing claim."""
        self._require_workspace(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        claim_type = require(ClaimType, claim_type, field="claim type")
        statement = str(statement or "").strip()
        if not statement:
            raise InvalidRequest("statement is required")
        if len(statement) > MAX_STATEMENT_CHARS:
            raise InvalidRequest(
                f"statement exceeds {MAX_STATEMENT_CHARS} characters",
                details={"chars": len(statement)})
        note = self._note(note)
        rows = verifier.verify_evidence_refs(workspace_id, evidence,
                                             require_nonempty=True)
        statement_hash = identity.statement_hash(statement)
        existing = store.find_claim_by_hash(workspace_id, entity["id"],
                                            statement_hash)
        if existing:
            return {"workspace_id": workspace_id,
                    "claim": store.get_claim(workspace_id, existing["id"]),
                    "deduplicated": True,
                    "knowledge_revision":
                        store.current_revision_number(workspace_id)}
        with store.graph_transaction(
                workspace_id, action=RevisionAction.MANUAL_CLAIM_CREATE,
                actor=self._actor(actor),
                summary=f"manual claim on {entity['canonical_key']}") as tx:
            claim = tx.insert_claim(
                entity_id=entity["id"], claim_type=claim_type,
                statement=statement,
                normalized_statement_hash=statement_hash,
                origin=GraphOrigin.MANUAL, evidence=rows)
            tx.insert_decision(
                decision_type=DecisionType.CREATE_CLAIM,
                target_kind=DecisionTargetKind.CLAIM,
                target_id=claim["id"], actor=self._actor(actor), note=note,
                after={"claim_id": claim["id"], "entity_id": entity["id"],
                       "claim_type": claim_type},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id,
                "claim": store.get_claim(workspace_id, claim["id"]),
                "deduplicated": False, "knowledge_revision": revision}

    def create_relation(self, workspace_id: str, *, source_entity_id: str,
                        target_entity_id: str, relation_type: str,
                        relation_state: str,
                        evidence: Sequence[Dict[str, Any]],
                        actor: str, note: str,
                        confidence: str = "medium",
                        source_command: str = "") -> Dict[str, Any]:
        """Manual Relation creation: both endpoints must exist in this
        workspace, the state must be ``explicit`` or ``confirmed`` (a manual
        caller has no analyzer to justify ``inferred``), evidence is
        mandatory, and the active-identity tuple deduplicates."""
        self._require_workspace(workspace_id)
        source = self._require_entity(workspace_id, source_entity_id)
        target = self._require_entity(workspace_id, target_entity_id)
        relation_type = require(RelationType, relation_type,
                                field="relation type")
        relation_state = str(relation_state or "").strip().lower()
        if relation_state not in RelationState.MANUAL:
            raise InvalidRequest(
                f"manual relation state must be one of "
                f"{sorted(RelationState.MANUAL)} (got {relation_state!r}; "
                f"'inferred' requires a documented analyzer origin)",
                details={"allowed": sorted(RelationState.MANUAL)})
        if source["id"] == target["id"] and \
                relation_type not in RelationType.SELF_RELATING:
            raise GraphConflict(
                f"self-relation rejected for type {relation_type!r}")
        note = self._note(note)
        rows = verifier.verify_evidence_refs(workspace_id, evidence,
                                             require_nonempty=True)
        existing = store.find_active_relation(
            workspace_id, source["id"], target["id"], relation_type)
        if existing:
            return {"workspace_id": workspace_id,
                    "relation": store.get_relation(workspace_id,
                                                   existing["id"]),
                    "deduplicated": True,
                    "knowledge_revision":
                        store.current_revision_number(workspace_id)}
        with store.graph_transaction(
                workspace_id, action=RevisionAction.MANUAL_RELATION_CREATE,
                actor=self._actor(actor),
                summary=f"manual relation {relation_type}") as tx:
            relation = tx.insert_relation(
                source_entity_id=source["id"],
                target_entity_id=target["id"],
                relation_type=relation_type,
                relation_state=relation_state,
                confidence=str(confidence or "medium"),
                origin=GraphOrigin.MANUAL, evidence=rows)
            tx.insert_decision(
                decision_type=DecisionType.CREATE_RELATION,
                target_kind=DecisionTargetKind.RELATION,
                target_id=relation["id"], actor=self._actor(actor),
                note=note,
                after={"relation_id": relation["id"],
                       "source_entity_id": source["id"],
                       "target_entity_id": target["id"],
                       "relation_type": relation_type,
                       "relation_state": relation_state},
                source_command=source_command)
            revision = tx.revision_number
        return {"workspace_id": workspace_id,
                "relation": store.get_relation(workspace_id, relation["id"]),
                "deduplicated": False, "knowledge_revision": revision}

    # ======================================================================
    # Aliases
    # ======================================================================
    def add_alias(self, workspace_id: str, *, entity_id: str, alias: str,
                  alias_type: str, actor: str, note: str,
                  evidence_id: str = "",
                  source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        alias_type = require(AliasType, alias_type, field="alias type")
        alias = str(alias or "").strip()
        if not alias:
            raise InvalidRequest("alias is required")
        note = self._note(note)
        normalized = identity.normalize_alias(alias)
        holders = store.find_alias_holders(workspace_id, normalized,
                                           exclude_entity_id=entity["id"])
        if holders:
            raise AliasCollision(alias, [
                {"entity_id": h["entity_id"],
                 "canonical_key": h["holder_canonical_key"]}
                for h in holders])
        own = [a for a in store.list_aliases(workspace_id, entity["id"])
               if a["normalized_alias"] == normalized]
        if own:
            return {"workspace_id": workspace_id, "alias": own[0],
                    "deduplicated": True,
                    "knowledge_revision":
                        store.current_revision_number(workspace_id)}
        if evidence_id:
            verifier.verify_evidence_refs(
                workspace_id, [{"evidence_id": evidence_id}],
                require_nonempty=True)
        with store.graph_transaction(
                workspace_id, action=RevisionAction.ALIAS_CHANGE,
                actor=self._actor(actor),
                summary=f"add alias to {entity['canonical_key']}") as tx:
            row = tx.insert_alias(entity_id=entity["id"], alias=alias,
                                  normalized_alias=normalized,
                                  alias_type=alias_type,
                                  origin=GraphOrigin.MANUAL,
                                  evidence_id=evidence_id)
            tx.insert_decision(
                decision_type=DecisionType.ADD_ALIAS,
                target_kind=DecisionTargetKind.ALIAS, target_id=row["id"],
                actor=self._actor(actor), note=note,
                after={"entity_id": entity["id"], "alias": alias,
                       "alias_type": alias_type},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id, "alias": row,
                "deduplicated": False, "knowledge_revision": revision}

    def remove_alias(self, workspace_id: str, *, entity_id: str, alias: str,
                     actor: str, note: str,
                     source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        note = self._note(note)
        normalized = identity.normalize_alias(alias)
        matches = [a for a in store.list_aliases(workspace_id, entity["id"])
                   if a["normalized_alias"] == normalized]
        if not matches:
            raise InvalidRequest(
                f"entity {entity_id} has no active alias {alias!r}")
        with store.graph_transaction(
                workspace_id, action=RevisionAction.ALIAS_CHANGE,
                actor=self._actor(actor),
                summary=f"remove alias from {entity['canonical_key']}") as tx:
            for row in matches:
                tx.update_alias(row["id"], status=AliasStatus.REMOVED)
            tx.insert_decision(
                decision_type=DecisionType.REMOVE_ALIAS,
                target_kind=DecisionTargetKind.ALIAS,
                target_id=matches[0]["id"], actor=self._actor(actor),
                note=note,
                before={"entity_id": entity["id"], "alias": alias},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id, "removed": len(matches),
                "knowledge_revision": revision}

    # ======================================================================
    # Authority / supersede / withdraw / reject / restore
    # ======================================================================
    _OBJECT_KINDS = {
        "entity": (DecisionTargetKind.ENTITY,),
        "claim": (DecisionTargetKind.CLAIM,),
        "relation": (DecisionTargetKind.RELATION,),
    }

    def _require_object(self, workspace_id: str, kind: str,
                        object_id: str) -> Dict[str, Any]:
        if kind == "entity":
            return self._require_entity(workspace_id, object_id)
        if kind == "claim":
            return self._require_claim(workspace_id, object_id)
        if kind == "relation":
            return self._require_relation(workspace_id, object_id)
        raise InvalidRequest(f"unknown object kind: {kind!r}",
                             details={"allowed": ["entity", "claim",
                                                  "relation"]})

    def set_authority(self, workspace_id: str, *, kind: str, object_id: str,
                      authority: str, actor: str, note: str,
                      source_command: str = "") -> Dict[str, Any]:
        """Explicit authority marking. Never inferred — this method IS the
        only path that changes authority_status."""
        self._require_workspace(workspace_id)
        authority = require(AuthorityStatus, authority,
                            field="authority status")
        note = self._note(note)
        obj = self._require_object(workspace_id, kind, object_id)
        decision_type = (DecisionType.MARK_AUTHORITATIVE
                         if authority == AuthorityStatus.AUTHORITATIVE
                         else DecisionType.MARK_NON_AUTHORITATIVE)
        with store.graph_transaction(
                workspace_id, action=RevisionAction.AUTHORITY_CHANGE,
                actor=self._actor(actor),
                summary=f"set {kind} authority to {authority}") as tx:
            update = {"entity": tx.update_entity, "claim": tx.update_claim,
                      "relation": tx.update_relation}[kind]
            update(object_id, authority_status=authority)
            tx.insert_decision(
                decision_type=decision_type,
                target_kind=self._OBJECT_KINDS[kind][0],
                target_id=object_id, actor=self._actor(actor), note=note,
                before={"authority_status": obj["authority_status"]},
                after={"authority_status": authority},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id, "kind": kind,
                "object_id": object_id, "authority_status": authority,
                "knowledge_revision": revision}

    def supersede_object(self, workspace_id: str, *, kind: str,
                         object_id: str, replacement_id: str, actor: str,
                         note: str,
                         source_command: str = "") -> Dict[str, Any]:
        """Explicit supersede: the old object is preserved (never deleted),
        marked superseded, and points at its replacement."""
        self._require_workspace(workspace_id)
        note = self._note(note)
        old = self._require_object(workspace_id, kind, object_id)
        new = self._require_object(workspace_id, kind, replacement_id)
        if old["id"] == new["id"]:
            raise GraphConflict("an object cannot supersede itself")
        if new["lifecycle_status"] != GraphLifecycleStatus.ACTIVE:
            raise GraphConflict(
                f"replacement {replacement_id} is not active "
                f"({new['lifecycle_status']})")
        pointer = {"entity": "superseded_by_entity_id",
                   "claim": "superseded_by_claim_id",
                   "relation": "superseded_by_relation_id"}[kind]
        action = (RevisionAction.CLAIM_SUPERSEDE if kind == "claim"
                  else RevisionAction.SUPERSEDE)
        with store.graph_transaction(
                workspace_id, action=action, actor=self._actor(actor),
                summary=f"supersede {kind} {object_id}") as tx:
            update = {"entity": tx.update_entity, "claim": tx.update_claim,
                      "relation": tx.update_relation}[kind]
            fields = {"lifecycle_status": GraphLifecycleStatus.SUPERSEDED,
                      pointer: new["id"]}
            if kind == "relation":
                fields["relation_state"] = RelationState.SUPERSEDED
            update(object_id, **fields)
            tx.insert_decision(
                decision_type=DecisionType.SUPERSEDE,
                target_kind=self._OBJECT_KINDS[kind][0],
                target_id=object_id, actor=self._actor(actor), note=note,
                before={"lifecycle_status": old["lifecycle_status"]},
                after={"lifecycle_status": GraphLifecycleStatus.SUPERSEDED,
                       "replacement_id": new["id"]},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id, "kind": kind,
                "object_id": object_id, "replacement_id": new["id"],
                "knowledge_revision": revision}

    def withdraw_object(self, workspace_id: str, *, kind: str,
                        object_id: str, actor: str, note: str,
                        source_command: str = "") -> Dict[str, Any]:
        """Explicit withdrawal: history preserved, excluded from active
        queries."""
        self._require_workspace(workspace_id)
        note = self._note(note)
        obj = self._require_object(workspace_id, kind, object_id)
        if obj["lifecycle_status"] == GraphLifecycleStatus.WITHDRAWN:
            return {"workspace_id": workspace_id, "kind": kind,
                    "object_id": object_id, "already_withdrawn": True,
                    "knowledge_revision":
                        store.current_revision_number(workspace_id)}
        with store.graph_transaction(
                workspace_id, action=RevisionAction.WITHDRAW,
                actor=self._actor(actor),
                summary=f"withdraw {kind} {object_id}") as tx:
            update = {"entity": tx.update_entity, "claim": tx.update_claim,
                      "relation": tx.update_relation}[kind]
            update(object_id,
                   lifecycle_status=GraphLifecycleStatus.WITHDRAWN)
            tx.insert_decision(
                decision_type=DecisionType.WITHDRAW,
                target_kind=self._OBJECT_KINDS[kind][0],
                target_id=object_id, actor=self._actor(actor), note=note,
                before={"lifecycle_status": obj["lifecycle_status"]},
                after={"lifecycle_status": GraphLifecycleStatus.WITHDRAWN},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id, "kind": kind,
                "object_id": object_id, "knowledge_revision": revision}

    def reject_relation(self, workspace_id: str, *, relation_id: str,
                        actor: str, note: str,
                        source_command: str = "") -> Dict[str, Any]:
        """Mark a Relation rejected: retained as governance history, excluded
        from the active graph. The prior epistemic state is parked so an
        explicit restore is lossless."""
        self._require_workspace(workspace_id)
        note = self._note(note)
        relation = self._require_relation(workspace_id, relation_id)
        if relation["relation_state"] == RelationState.REJECTED:
            return {"workspace_id": workspace_id,
                    "relation_id": relation_id, "already_rejected": True,
                    "knowledge_revision":
                        store.current_revision_number(workspace_id)}
        meta = dict(relation.get("metadata") or {})
        meta["prior_state"] = relation["relation_state"]
        with store.graph_transaction(
                workspace_id, action=RevisionAction.RELATION_STATE_CHANGE,
                actor=self._actor(actor),
                summary=f"reject relation {relation_id}") as tx:
            tx.update_relation(relation_id,
                               relation_state=RelationState.REJECTED,
                               metadata=meta)
            tx.insert_decision(
                decision_type=DecisionType.REJECT_RELATION,
                target_kind=DecisionTargetKind.RELATION,
                target_id=relation_id, actor=self._actor(actor), note=note,
                before={"relation_state": relation["relation_state"]},
                after={"relation_state": RelationState.REJECTED},
                source_command=source_command)
            revision = tx.revision_number
        return {"workspace_id": workspace_id, "relation_id": relation_id,
                "relation_state": RelationState.REJECTED,
                "knowledge_revision": revision}

    def restore_relation(self, workspace_id: str, *, relation_id: str,
                         actor: str, note: str,
                         source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        note = self._note(note)
        relation = self._require_relation(workspace_id, relation_id)
        if relation["relation_state"] != RelationState.REJECTED:
            raise GraphConflict(
                f"relation {relation_id} is not rejected "
                f"({relation['relation_state']})")
        prior = (relation.get("metadata") or {}).get(
            "prior_state", RelationState.CONFIRMED)
        with store.graph_transaction(
                workspace_id, action=RevisionAction.RELATION_STATE_CHANGE,
                actor=self._actor(actor),
                summary=f"restore relation {relation_id}") as tx:
            tx.update_relation(relation_id, relation_state=prior)
            tx.insert_decision(
                decision_type=DecisionType.RESTORE_RELATION,
                target_kind=DecisionTargetKind.RELATION,
                target_id=relation_id, actor=self._actor(actor), note=note,
                before={"relation_state": RelationState.REJECTED},
                after={"relation_state": prior},
                source_command=source_command)
            revision = tx.revision_number
        return {"workspace_id": workspace_id, "relation_id": relation_id,
                "relation_state": prior, "knowledge_revision": revision}

    # ======================================================================
    # Merge and split
    # ======================================================================
    def merge_entities(self, workspace_id: str, *, source_entity_id: str,
                       target_entity_id: str, actor: str, note: str,
                       source_command: str = "") -> Dict[str, Any]:
        """Merge source into target, transactionally (spec §24)."""
        self._require_workspace(workspace_id)
        note = self._note(note)
        source = self._require_entity(workspace_id, source_entity_id)
        target = self._require_entity(workspace_id, target_entity_id)
        if source["id"] == target["id"]:
            raise GraphConflict("an entity cannot be merged into itself")
        if target["lifecycle_status"] != GraphLifecycleStatus.ACTIVE:
            raise GraphConflict(
                f"merge target must be active "
                f"({target['lifecycle_status']})")
        if source["lifecycle_status"] in (GraphLifecycleStatus.MERGED,
                                          GraphLifecycleStatus.WITHDRAWN):
            raise GraphConflict(
                f"source entity is {source['lifecycle_status']} and cannot "
                f"be merged")

        alias_collisions: List[Dict[str, Any]] = []
        moved = {"aliases": 0, "bindings": 0, "claims": 0,
                 "claims_deduplicated": 0, "relations_rewired": 0,
                 "relations_deduplicated": 0, "self_relations_removed": 0}

        with store.graph_transaction(
                workspace_id, action=RevisionAction.ENTITY_MERGE,
                actor=self._actor(actor),
                summary=f"merge {source['canonical_key']} into "
                        f"{target['canonical_key']}") as tx:
            # aliases: move unless the normalized form collides anywhere
            # outside the pair.
            target_aliases = {a["normalized_alias"] for a in
                              store.list_aliases(workspace_id, target["id"],
                                                 status=None)}
            for alias in store.list_aliases(workspace_id, source["id"]):
                holders = store.find_alias_holders(
                    workspace_id, alias["normalized_alias"],
                    exclude_entity_id=source["id"])
                external = [h for h in holders
                            if h["entity_id"] != target["id"]]
                if external:
                    alias_collisions.append(
                        {"alias": alias["alias"],
                         "held_by": [h["holder_canonical_key"]
                                     for h in external]})
                    continue
                if alias["normalized_alias"] in target_aliases:
                    tx.update_alias(alias["id"],
                                    status=AliasStatus.SUPERSEDED)
                    continue
                tx.update_alias(alias["id"], entity_id=target["id"])
                target_aliases.add(alias["normalized_alias"])
                moved["aliases"] += 1

            # bindings: move and deduplicate.
            target_bindings = {
                (b["ref_kind"], b["ref_id"], b["binding_role"])
                for b in store.list_bindings(workspace_id, target["id"])}
            for binding in store.list_bindings(workspace_id, source["id"]):
                sig = (binding["ref_kind"], binding["ref_id"],
                       binding["binding_role"])
                if sig in target_bindings:
                    tx.update_binding(binding["id"], status="removed")
                    continue
                tx.update_binding(binding["id"], entity_id=target["id"])
                target_bindings.add(sig)
                moved["bindings"] += 1

            # claims: move; identical normalized statements deduplicate by
            # superseding the source claim with the target's.
            for claim in store.list_claims(workspace_id,
                                           entity_id=source["id"],
                                           lifecycle_status=None,
                                           limit=10_000):
                duplicate = store.find_claim_by_hash(
                    workspace_id, target["id"],
                    claim["normalized_statement_hash"])
                if duplicate and claim["lifecycle_status"] == \
                        GraphLifecycleStatus.ACTIVE:
                    tx.update_claim(
                        claim["id"], entity_id=target["id"],
                        lifecycle_status=GraphLifecycleStatus.SUPERSEDED,
                        superseded_by_claim_id=duplicate["id"])
                    moved["claims_deduplicated"] += 1
                else:
                    tx.move_claim(claim["id"], target["id"])
                    moved["claims"] += 1

            # relations: rewire both endpoints; drop self-relations; dedup
            # against the target's existing active relations.
            for relation in store.list_relations(workspace_id,
                                                 entity_id=source["id"],
                                                 lifecycle_status=None,
                                                 limit=10_000):
                new_source = (target["id"]
                              if relation["source_entity_id"] == source["id"]
                              else relation["source_entity_id"])
                new_target = (target["id"]
                              if relation["target_entity_id"] == source["id"]
                              else relation["target_entity_id"])
                if new_source == new_target:
                    tx.update_relation(
                        relation["id"],
                        lifecycle_status=GraphLifecycleStatus.WITHDRAWN)
                    moved["self_relations_removed"] += 1
                    continue
                duplicate = store.find_active_relation(
                    workspace_id, new_source, new_target,
                    relation["relation_type"])
                if duplicate and duplicate["id"] != relation["id"] and \
                        relation["lifecycle_status"] == \
                        GraphLifecycleStatus.ACTIVE:
                    tx.update_relation(
                        relation["id"], source_entity_id=new_source,
                        target_entity_id=new_target,
                        lifecycle_status=GraphLifecycleStatus.SUPERSEDED,
                        relation_state=RelationState.SUPERSEDED,
                        superseded_by_relation_id=duplicate["id"])
                    moved["relations_deduplicated"] += 1
                else:
                    tx.update_relation(relation["id"],
                                       source_entity_id=new_source,
                                       target_entity_id=new_target)
                    moved["relations_rewired"] += 1

            # the source itself: merged, addressable, resolving to target.
            tx.update_entity(source["id"],
                             lifecycle_status=GraphLifecycleStatus.MERGED,
                             merged_into_entity_id=target["id"])
            tx.insert_decision(
                decision_type=DecisionType.MERGE_ENTITY,
                target_kind=DecisionTargetKind.ENTITY,
                target_id=source["id"], actor=self._actor(actor), note=note,
                before=self._entity_snapshot(source),
                after={"merged_into_entity_id": target["id"], **moved},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id,
                "source_entity_id": source["id"],
                "target_entity_id": target["id"],
                **moved, "alias_collisions": alias_collisions,
                "knowledge_revision": revision}

    def split_entity(self, workspace_id: str, *, source_entity_id: str,
                     new_entity_type: str, new_canonical_key: str,
                     new_display_name: str, claim_ids: Sequence[str],
                     binding_ids: Sequence[str],
                     relation_rewrites: Sequence[Dict[str, str]],
                     actor: str, note: str,
                     source_command: str = "") -> Dict[str, Any]:
        """Explicit, narrow split (spec §25): the caller lists exactly what
        moves; nothing is chosen by a model; all-or-nothing."""
        self._require_workspace(workspace_id)
        note = self._note(note)
        source = self._require_entity(workspace_id, source_entity_id)
        new_entity_type = require(EntityType, new_entity_type,
                                  field="entity type")
        new_canonical_key = str(new_canonical_key or "").strip()
        if not new_canonical_key:
            raise InvalidRequest("new canonical_key is required")
        if store.find_entity_by_key(workspace_id, new_entity_type,
                                    new_canonical_key):
            raise GraphConflict(
                f"an entity with canonical key {new_canonical_key!r} "
                f"already exists")
        claim_ids = [str(c).strip() for c in claim_ids if str(c).strip()]
        binding_ids = [str(b).strip() for b in binding_ids
                       if str(b).strip()]
        if not claim_ids and not binding_ids:
            raise InvalidRequest(
                "a split must move at least one claim or binding")

        # Every moved object must belong to the source — checked BEFORE the
        # transaction so an invalid id aborts with nothing written.
        source_claims = {c["id"] for c in store.list_claims(
            workspace_id, entity_id=source["id"], lifecycle_status=None,
            limit=10_000)}
        for claim_id in claim_ids:
            if claim_id not in source_claims:
                raise GraphConflict(
                    f"claim {claim_id} does not belong to entity "
                    f"{source['id']}", details={"claim_id": claim_id})
        source_bindings = {b["id"] for b in store.list_bindings(
            workspace_id, source["id"])}
        for binding_id in binding_ids:
            if binding_id not in source_bindings:
                raise GraphConflict(
                    f"binding {binding_id} does not belong to entity "
                    f"{source['id']}", details={"binding_id": binding_id})
        rewrites: List[Dict[str, str]] = []
        for rewrite in relation_rewrites or []:
            relation_id = str(rewrite.get("relation_id") or "").strip()
            end = str(rewrite.get("end") or "").strip().lower()
            if end not in ("source", "target"):
                raise InvalidRequest(
                    f"relation rewrite end must be 'source' or 'target' "
                    f"(got {end!r})")
            relation = self._require_relation(workspace_id, relation_id)
            column = f"{end}_entity_id"
            if relation[column] != source["id"]:
                raise GraphConflict(
                    f"relation {relation_id} does not have entity "
                    f"{source['id']} as its {end}")
            rewrites.append({"relation_id": relation_id, "column": column})

        with store.graph_transaction(
                workspace_id, action=RevisionAction.ENTITY_SPLIT,
                actor=self._actor(actor),
                summary=f"split {source['canonical_key']} -> "
                        f"{new_canonical_key}") as tx:
            new_entity = tx.insert_entity(
                entity_type=new_entity_type,
                canonical_key=new_canonical_key,
                display_name=str(new_display_name
                                 or new_canonical_key)[:200],
                origin=GraphOrigin.MANUAL)
            for claim_id in claim_ids:
                tx.move_claim(claim_id, new_entity["id"])
            for binding_id in binding_ids:
                tx.update_binding(binding_id, entity_id=new_entity["id"])
            for rewrite in rewrites:
                tx.update_relation(rewrite["relation_id"],
                                   **{rewrite["column"]: new_entity["id"]})
            tx.insert_decision(
                decision_type=DecisionType.SPLIT_ENTITY,
                target_kind=DecisionTargetKind.ENTITY,
                target_id=source["id"], actor=self._actor(actor), note=note,
                before=self._entity_snapshot(source),
                after={"new_entity_id": new_entity["id"],
                       "moved_claims": len(claim_ids),
                       "moved_bindings": len(binding_ids),
                       "rewired_relations": len(rewrites)},
                source_command=source_command)
            revision = tx.revision_number
        self._refresh_projection(workspace_id)
        return {"workspace_id": workspace_id,
                "source_entity_id": source["id"],
                "new_entity": store.get_entity(workspace_id,
                                               new_entity["id"]),
                "moved_claims": len(claim_ids),
                "moved_bindings": len(binding_ids),
                "rewired_relations": len(rewrites),
                "knowledge_revision": revision}


__all__ = ["KnowledgeService", "MAX_NOTE_CHARS"]
