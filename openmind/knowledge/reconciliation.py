"""Canonical graph staleness reconciliation.

The graph must never silently drift from the source plane: when a Revision
stops being current (a new revision landed, or its Asset was removed), the
graph records that depended on it become ``stale`` — excluded from active
queries by default, still fully queryable, authority and history intact.

RULES (spec §22, each tested in verify_knowledge_staleness)
-----------------------------------------------------------
1. bindings referencing a non-current Revision (directly, or through a
   Segment of a non-current Revision) -> stale; bindings whose Revision is
   current again (a removed Asset reappeared) -> revived;
2. active Claims none of whose Evidence sits on a current Revision -> stale;
   stale Claims with current Evidence again -> revived;
3. active Relations whose endpoints are no longer active, or all of whose
   Evidence is non-current -> stale (epistemic state parked in metadata and
   restored on revival);
4. deterministic Entities with no active bindings -> stale;
   promoted Entities with no active bindings AND no active Claims -> stale;
   MANUAL Entities are never auto-staled — staleness for them is a
   governance decision (withdraw/supersede), not an inference;
5. nothing is deleted; authority_status is never touched; review history and
   promotion provenance stay intact.

Everything is set-based SQL over the v0006 indexes inside ONE graph
transaction. A pass that changes nothing writes nothing and mints no
Knowledge Revision. Safe to run any number of times.
"""
from __future__ import annotations

from typing import Any, Dict

from . import store
from .vocabularies import GraphOrigin, RevisionAction

#: The set of CURRENT revisions: active assets only, so a removed Asset's
#: revision no longer anchors graph knowledge (and re-anchors on reappearance).
_CURRENT_REVISIONS = ("SELECT current_revision_id FROM assets "
                      "WHERE workspace_id=? AND state='active' "
                      "AND current_revision_id IS NOT NULL")

#: One Evidence row of the claim/relation resolves to a current revision.
_CLAIM_HAS_CURRENT_EVIDENCE = (
    "EXISTS (SELECT 1 FROM engineering_claim_evidence ce "
    "JOIN evidence ev ON ev.id = ce.evidence_id "
    f"WHERE ce.claim_id = engineering_claims.id "
    f"AND ev.revision_id IN ({_CURRENT_REVISIONS}))")

_RELATION_HAS_EVIDENCE = (
    "EXISTS (SELECT 1 FROM engineering_relation_evidence re "
    "WHERE re.relation_id = engineering_relations.id)")

_RELATION_HAS_CURRENT_EVIDENCE = (
    "EXISTS (SELECT 1 FROM engineering_relation_evidence re "
    "JOIN evidence ev ON ev.id = re.evidence_id "
    f"WHERE re.relation_id = engineering_relations.id "
    f"AND ev.revision_id IN ({_CURRENT_REVISIONS}))")


def reconcile_graph_staleness(workspace_id: str, *,
                              actor: str = "") -> Dict[str, Any]:
    """One incremental reconciliation pass. Returns the change counts (all
    zero => no transaction was committed and no revision was minted)."""
    ws = workspace_id
    with store.graph_transaction(ws, action=RevisionAction.GRAPH_RECONCILE,
                                 actor=actor,
                                 summary="graph staleness reconciliation"
                                 ) as tx:
        ts = tx.ts
        # -- 1. bindings ----------------------------------------------------
        stale_rev_bindings = tx.execute_counted(
            "UPDATE engineering_entity_bindings SET status='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND status='active' AND "
            f"ref_kind='revision' AND ref_id NOT IN ({_CURRENT_REVISIONS})",
            (ts, tx.revision_number, ts, ws, ws), "bindings")
        stale_seg_bindings = tx.execute_counted(
            "UPDATE engineering_entity_bindings SET status='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND status='active' AND ref_kind='segment' "
            "AND ref_id NOT IN (SELECT s.id FROM segments s "
            f"WHERE s.revision_id IN ({_CURRENT_REVISIONS}))",
            (ts, tx.revision_number, ts, ws, ws), "bindings")
        revived_bindings = tx.execute_counted(
            "UPDATE engineering_entity_bindings SET status='active', "
            "stale_at=NULL, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND status='stale' AND ("
            f"(ref_kind='revision' AND ref_id IN ({_CURRENT_REVISIONS})) OR "
            "(ref_kind='segment' AND ref_id IN (SELECT s.id FROM segments s "
            f"WHERE s.revision_id IN ({_CURRENT_REVISIONS}))))",
            (tx.revision_number, ts, ws, ws, ws), "bindings")

        # -- 2. claims ------------------------------------------------------
        stale_claims = tx.execute_counted(
            "UPDATE engineering_claims SET lifecycle_status='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='active' AND "
            "EXISTS (SELECT 1 FROM engineering_claim_evidence ce "
            "WHERE ce.claim_id = engineering_claims.id) AND NOT "
            + _CLAIM_HAS_CURRENT_EVIDENCE,
            (ts, tx.revision_number, ts, ws, ws), "claims")
        revived_claims = tx.execute_counted(
            "UPDATE engineering_claims SET lifecycle_status='active', "
            "stale_at=NULL, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='stale' AND "
            + _CLAIM_HAS_CURRENT_EVIDENCE,
            (tx.revision_number, ts, ws, ws), "claims")

        # -- 3. relations ---------------------------------------------------
        # A relation goes stale when an endpoint is no longer active, or when
        # it HAS evidence and none of it is current. The prior epistemic
        # state is parked in metadata by the JSON rewrite below only for the
        # rows this statement touches.
        stale_relations = tx.execute_counted(
            "UPDATE engineering_relations SET lifecycle_status='stale', "
            "metadata_json=json_set(COALESCE(metadata_json,'{}'), "
            "'$.prior_state', relation_state), relation_state='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='active' AND ("
            "source_entity_id IN (SELECT id FROM engineering_entities WHERE "
            "workspace_id=? AND lifecycle_status != 'active') OR "
            "target_entity_id IN (SELECT id FROM engineering_entities WHERE "
            "workspace_id=? AND lifecycle_status != 'active') OR "
            f"({_RELATION_HAS_EVIDENCE} AND NOT "
            f"{_RELATION_HAS_CURRENT_EVIDENCE}))",
            (ts, tx.revision_number, ts, ws, ws, ws, ws), "relations")
        revived_relations = tx.execute_counted(
            "UPDATE engineering_relations SET lifecycle_status='active', "
            "relation_state=COALESCE(json_extract(metadata_json, "
            "'$.prior_state'), 'inferred'), stale_at=NULL, "
            "updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='stale' AND "
            "source_entity_id IN (SELECT id FROM engineering_entities WHERE "
            "workspace_id=? AND lifecycle_status='active') AND "
            "target_entity_id IN (SELECT id FROM engineering_entities WHERE "
            "workspace_id=? AND lifecycle_status='active') AND "
            f"(NOT {_RELATION_HAS_EVIDENCE} OR "
            f"{_RELATION_HAS_CURRENT_EVIDENCE})",
            (tx.revision_number, ts, ws, ws, ws, ws), "relations")

        # -- 4. entities ----------------------------------------------------
        stale_det_entities = tx.execute_counted(
            "UPDATE engineering_entities SET lifecycle_status='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='active' AND "
            f"origin='{GraphOrigin.DETERMINISTIC}' AND NOT EXISTS ("
            "SELECT 1 FROM engineering_entity_bindings b WHERE "
            "b.entity_id = engineering_entities.id AND b.status='active')",
            (ts, tx.revision_number, ts, ws), "entities")
        stale_promoted_entities = tx.execute_counted(
            "UPDATE engineering_entities SET lifecycle_status='stale', "
            "stale_at=?, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='active' AND "
            f"origin='{GraphOrigin.SEMANTIC_PROMOTION}' AND NOT EXISTS ("
            "SELECT 1 FROM engineering_entity_bindings b WHERE "
            "b.entity_id = engineering_entities.id AND b.status='active') "
            "AND NOT EXISTS (SELECT 1 FROM engineering_claims c WHERE "
            "c.entity_id = engineering_entities.id AND "
            "c.lifecycle_status='active')",
            (ts, tx.revision_number, ts, ws), "entities")
        revived_entities = tx.execute_counted(
            "UPDATE engineering_entities SET lifecycle_status='active', "
            "stale_at=NULL, updated_knowledge_revision=?, updated_at=? "
            "WHERE workspace_id=? AND lifecycle_status='stale' AND "
            f"origin IN ('{GraphOrigin.DETERMINISTIC}', "
            f"'{GraphOrigin.SEMANTIC_PROMOTION}') AND (EXISTS ("
            "SELECT 1 FROM engineering_entity_bindings b WHERE "
            "b.entity_id = engineering_entities.id AND b.status='active') "
            "OR EXISTS (SELECT 1 FROM engineering_claims c WHERE "
            "c.entity_id = engineering_entities.id AND "
            "c.lifecycle_status='active'))",
            (tx.revision_number, ts, ws), "entities")
        revision_number = tx.revision_number
        wrote = tx.wrote

    return {
        "workspace_id": ws,
        "stale_bindings": stale_rev_bindings + stale_seg_bindings,
        "revived_bindings": revived_bindings,
        "stale_claims": stale_claims, "revived_claims": revived_claims,
        "stale_relations": stale_relations,
        "revived_relations": revived_relations,
        "stale_entities": stale_det_entities + stale_promoted_entities,
        "revived_entities": revived_entities,
        "changed": wrote,
        "knowledge_revision": (revision_number if wrote else
                               store.current_revision_number(ws)),
    }


__all__ = ["reconcile_graph_staleness"]
