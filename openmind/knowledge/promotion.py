"""Explicit Candidate promotion: the only bridge from the Phase 4 semantic
plane into the canonical graph.

Planning (:func:`plan_candidate_promotion`, :func:`plan_relation_promotion`)
is deterministic, calls no provider and writes nothing. Promotion
(:func:`promote_candidate`, :func:`promote_relation`) re-checks every
eligibility rule inside the graph transaction, so a candidate that went
stale between plan and promote is still blocked.

Eligibility is deliberately without bypass flags: there is no
``--accept-stale``, no ``--ignore-evidence``, no ``--force-unverified``.
A stale candidate must be re-analyzed, or the knowledge recreated manually
with fresh Evidence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import db
from . import PROMOTION_POLICY_VERSION
from ..semantic import store as semantic_store
from ..semantic.models import (EvidenceVerificationStatus, LifecycleStatus,
                               ReviewStatus, SemanticCandidateKind)
from . import identity, store, verifier
from .errors import GraphEvidenceInvalid, PromotionBlocked
from .vocabularies import (AliasType, BindingRefKind, BindingRole, ClaimType,
                           CONCEPT_TYPE_TO_ENTITY_TYPE, DecisionTargetKind,
                           DecisionType, EntityType,
                           ENTITY_TYPE_TO_CLAIM_TYPE, GraphLifecycleStatus,
                           GraphOrigin, PromotionCandidateKind,
                           PromotionExpectedAction, PromotionStatus,
                           RELATION_CANDIDATE_TYPE_TO_RELATION_TYPE,
                           RelationState, RevisionAction)

MAX_STATEMENT_CHARS = 4_000
MAX_DISPLAY_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_NOTE_CHARS = 2_000


def _bound(text: str, cap: int) -> str:
    text = str(text or "")
    return text if len(text) <= cap else text[:cap]


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def _base_blocking_reasons(workspace_id: str,
                           candidate: Dict[str, Any]) -> List[str]:
    """The review/lifecycle/evidence gates shared by both candidate kinds."""
    reasons: List[str] = []
    if candidate.get("review_status") != ReviewStatus.CONFIRMED:
        reasons.append(
            f"review_status is {candidate.get('review_status')!r} "
            f"(must be confirmed)")
    if candidate.get("lifecycle_status") != LifecycleStatus.ACTIVE:
        reasons.append(
            f"lifecycle_status is {candidate.get('lifecycle_status')!r} "
            f"(must be active; a stale candidate must be re-analyzed)")
    if candidate.get("evidence_status") != EvidenceVerificationStatus.VERIFIED:
        reasons.append(
            f"evidence_status is {candidate.get('evidence_status')!r} "
            f"(must be verified)")
    return reasons


def _candidate_blocking_reasons(workspace_id: str,
                                candidate: Dict[str, Any]) -> List[str]:
    reasons = _base_blocking_reasons(workspace_id, candidate)
    kind = candidate.get("candidate_kind")
    if kind not in SemanticCandidateKind.VALUES:
        reasons.append(f"unsupported candidate kind: {kind!r}")
    if kind == SemanticCandidateKind.ENGINEERING_CONCEPT and \
            candidate.get("candidate_type") not in CONCEPT_TYPE_TO_ENTITY_TYPE:
        reasons.append(
            f"unsupported engineering-concept type: "
            f"{candidate.get('candidate_type')!r}")
    # Source revision must still be current.
    revision_id = str(candidate.get("revision_id") or "")
    if revision_id and revision_id not in store.current_revision_ids(
            workspace_id):
        reasons.append(
            f"source revision {revision_id} is no longer current")
    # Cited evidence must belong to the workspace (full quote verification
    # happens in the promoting transaction; existence is checked here so the
    # plan reports it as a blocking reason rather than an exception).
    for ev in candidate.get("evidence") or []:
        if not db.get_evidence(workspace_id, str(ev.get("evidence_id") or "")):
            reasons.append(
                f"evidence {ev.get('evidence_id')} does not exist in this "
                f"workspace")
    if not (candidate.get("evidence") or []):
        reasons.append("candidate has no evidence joins")
    prior = store.find_promotion(
        workspace_id, PromotionCandidateKind.SEMANTIC_CANDIDATE,
        candidate["id"])
    if prior:
        reasons.append("candidate is already promoted")
    return reasons


# ---------------------------------------------------------------------------
# Identity resolution for engineering-concept candidates
# ---------------------------------------------------------------------------
def _resolve_concept_identity(workspace_id: str, candidate: Dict[str, Any]
                              ) -> Dict[str, Any]:
    """Deterministic identity proposal + match/conflict report."""
    entity_type = CONCEPT_TYPE_TO_ENTITY_TYPE[candidate["candidate_type"]]
    stable_key = str(candidate.get("stable_key") or "").strip()
    title = str(candidate.get("title") or "").strip()
    statement = str(candidate.get("statement") or "").strip()
    has_identifier = identity.looks_like_identifier(stable_key)
    if has_identifier:
        canonical_key = identity.identifier_entity_key(entity_type, stable_key)
        display_name = stable_key
    else:
        basis = title or statement[:80]
        canonical_key = identity.derived_entity_key(entity_type, basis)
        display_name = _bound(basis, MAX_DISPLAY_NAME_CHARS)

    matches: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    existing = store.find_entity_by_key(workspace_id, entity_type,
                                        canonical_key)
    if existing:
        matches.append({"via": "canonical-key", "entity_id": existing["id"],
                        "canonical_key": existing["canonical_key"]})
    alias_entities: List[Dict[str, Any]] = []
    if stable_key:
        alias_entities = store.find_entity_by_alias(
            workspace_id, identity.normalize_alias(stable_key))
        for ent in alias_entities:
            record = {"via": "alias", "entity_id": ent["id"],
                      "canonical_key": ent["canonical_key"],
                      "entity_type": ent["entity_type"]}
            if existing and ent["id"] == existing["id"]:
                continue        # the key match already covers it
            if existing and ent["id"] != existing["id"]:
                conflicts.append({**record, "reason":
                                  "alias resolves to a different entity than "
                                  "the canonical key"})
            elif ent["entity_type"] != entity_type:
                conflicts.append({**record, "reason":
                                  "alias is held by an entity of a different "
                                  "type"})
            else:
                matches.append(record)

    resolved: Optional[Dict[str, Any]] = existing
    if not resolved:
        same_type = [e for e in alias_entities
                     if e["entity_type"] == entity_type]
        if len(same_type) == 1 and not conflicts:
            resolved = same_type[0]
        elif len(same_type) > 1:
            conflicts.append({
                "via": "alias", "reason":
                    f"alias {stable_key!r} is held by "
                    f"{len(same_type)} active entities",
                "entity_ids": [e["id"] for e in same_type]})

    return {
        "entity_type": entity_type,
        "canonical_key": canonical_key,
        "display_name": display_name,
        "has_identifier": has_identifier,
        "stable_key": stable_key,
        "existing_entity": resolved,
        "identity_matches": matches,
        "identity_conflicts": conflicts,
    }


def _source_anchor(workspace_id: str, candidate: Dict[str, Any]
                   ) -> Tuple[str, str, List[str]]:
    """(asset_id, revision_id, [segment ids of the cited evidence])."""
    revision_id = str(candidate.get("revision_id") or "")
    asset_id = ""
    if revision_id:
        revision = db.get_revision(workspace_id, revision_id)
        asset_id = (revision or {}).get("asset_id", "")
    segment_ids: List[str] = []
    for ev in candidate.get("evidence") or []:
        record = db.get_evidence(workspace_id,
                                 str(ev.get("evidence_id") or ""))
        seg = (record or {}).get("segment_id")
        if seg and seg not in segment_ids:
            segment_ids.append(seg)
    return asset_id, revision_id, segment_ids


def _proposed_bindings(asset_id: str, revision_id: str,
                       segment_ids: List[str]) -> List[Dict[str, Any]]:
    bindings: List[Dict[str, Any]] = []
    if asset_id:
        bindings.append({"ref_kind": BindingRefKind.ASSET, "ref_id": asset_id,
                         "binding_role": BindingRole.PRIMARY_SOURCE})
    if revision_id:
        bindings.append({"ref_kind": BindingRefKind.REVISION,
                         "ref_id": revision_id,
                         "binding_role": BindingRole.PRIMARY_SOURCE})
    for seg in segment_ids:
        bindings.append({"ref_kind": BindingRefKind.SEGMENT, "ref_id": seg,
                         "binding_role": BindingRole.DEFINITION_SOURCE})
    return bindings


def _concept_claim_type(entity_type: str) -> str:
    return ENTITY_TYPE_TO_CLAIM_TYPE.get(entity_type, ClaimType.DEFINITION)


def _document_entity_proposal(workspace_id: str,
                              candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Entity identity for classification / revision-status candidates: the
    source ASSET's deterministic entity (document for parsed documents,
    else the deterministic asset-type projection)."""
    asset_id, revision_id, segment_ids = _source_anchor(workspace_id,
                                                       candidate)
    entity_type = EntityType.DOCUMENT
    display_name = ""
    if asset_id:
        asset = db.get_asset(workspace_id, asset_id)
        if asset:
            display_name = asset.get("title") or asset.get("logical_key", "")
            if not db.is_document_asset(workspace_id, asset_id):
                mapped = {
                    "source-code": EntityType.CODE_COMPONENT,
                    "test-source": EntityType.CODE_COMPONENT,
                    "configuration": EntityType.CONFIGURATION,
                    "database-schema": EntityType.DATA_MODEL,
                    "build-definition": EntityType.BUILD_DEFINITION,
                }.get(asset.get("asset_type", ""), EntityType.DOCUMENT)
                entity_type = mapped
    canonical_key = identity.asset_entity_key(entity_type, asset_id)
    return {"entity_type": entity_type, "canonical_key": canonical_key,
            "display_name": _bound(display_name, MAX_DISPLAY_NAME_CHARS),
            "asset_id": asset_id, "revision_id": revision_id,
            "segment_ids": segment_ids}


# ---------------------------------------------------------------------------
# Candidate promotion plan
# ---------------------------------------------------------------------------
def plan_candidate_promotion(workspace_id: str,
                             candidate_id: str) -> Dict[str, Any]:
    """Deterministic dry-run. No graph write, no provider call."""
    semantic_store.reconcile_staleness(workspace_id)
    candidate = semantic_store.get_candidate(workspace_id, candidate_id)
    if not candidate:
        from ..semantic.errors import CandidateNotFound
        raise CandidateNotFound(
            f"semantic candidate not found: {candidate_id!r}",
            details={"candidate_id": candidate_id})

    reasons = _candidate_blocking_reasons(workspace_id, candidate)
    prior = store.find_promotion(
        workspace_id, PromotionCandidateKind.SEMANTIC_CANDIDATE, candidate_id)

    plan: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "candidate_id": candidate_id,
        "candidate_kind": candidate.get("candidate_kind"),
        "candidate": candidate,
        "eligible": not reasons,
        "blocking_reasons": reasons,
        "evidence": candidate.get("evidence") or [],
        "identity_matches": [],
        "identity_conflicts": [],
        "existing_entity": None,
        "existing_claim": None,
        "proposed_entity": None,
        "proposed_claim": None,
        "proposed_aliases": [],
        "proposed_bindings": [],
        "promotion_policy_version": PROMOTION_POLICY_VERSION,
    }
    if prior:
        plan["expected_action"] = PromotionExpectedAction.ALREADY_PROMOTED
        plan["existing_promotion"] = prior
        target = store.get_entity(workspace_id, prior["target_id"]) \
            if prior["target_kind"] == "entity" else \
            store.get_claim(workspace_id, prior["target_id"])
        plan["existing_target"] = target
        return plan
    if reasons:
        plan["expected_action"] = PromotionExpectedAction.BLOCKED
        return plan

    kind = candidate.get("candidate_kind")
    if kind == SemanticCandidateKind.ENGINEERING_CONCEPT:
        ident = _resolve_concept_identity(workspace_id, candidate)
        plan["identity_matches"] = ident["identity_matches"]
        plan["identity_conflicts"] = ident["identity_conflicts"]
        plan["existing_entity"] = ident["existing_entity"]
        statement = _bound(candidate.get("statement", ""),
                           MAX_STATEMENT_CHARS)
        claim_type = _concept_claim_type(ident["entity_type"])
        plan["proposed_entity"] = {
            "entity_type": ident["entity_type"],
            "canonical_key": ident["canonical_key"],
            "display_name": ident["display_name"],
            "description": _bound(statement, MAX_DESCRIPTION_CHARS),
            "origin": GraphOrigin.SEMANTIC_PROMOTION,
        }
        plan["proposed_claim"] = {
            "claim_type": claim_type,
            "statement": statement,
            "normalized_statement_hash": identity.statement_hash(statement),
        }
        if ident["has_identifier"]:
            plan["proposed_aliases"] = [{
                "alias": ident["stable_key"],
                "alias_type": AliasType.IDENTIFIER}]
        asset_id, revision_id, segment_ids = _source_anchor(workspace_id,
                                                            candidate)
        plan["proposed_bindings"] = _proposed_bindings(asset_id, revision_id,
                                                       segment_ids)
        if ident["identity_conflicts"]:
            plan["eligible"] = False
            plan["blocking_reasons"] = [
                "identity conflict: " + str(c.get("reason", ""))
                for c in ident["identity_conflicts"]]
            plan["expected_action"] = PromotionExpectedAction.IDENTITY_CONFLICT
            return plan
        if ident["existing_entity"]:
            existing_claim = store.find_claim_by_hash(
                workspace_id, ident["existing_entity"]["id"],
                plan["proposed_claim"]["normalized_statement_hash"])
            plan["existing_claim"] = existing_claim
            plan["expected_action"] = \
                PromotionExpectedAction.ATTACH_CLAIM_TO_EXISTING_ENTITY
        else:
            plan["expected_action"] = \
                PromotionExpectedAction.CREATE_ENTITY_AND_CLAIM
        return plan

    # classification / revision-status: a claim on the source asset's entity
    proposal = _document_entity_proposal(workspace_id, candidate)
    statement = _bound(candidate.get("statement", "") or
                       candidate.get("title", ""), MAX_STATEMENT_CHARS)
    claim_type = (ClaimType.CLASSIFICATION
                  if kind == SemanticCandidateKind.CLASSIFICATION
                  else ClaimType.REVISION_STATUS)
    existing = store.find_entity_by_key(workspace_id,
                                        proposal["entity_type"],
                                        proposal["canonical_key"])
    plan["existing_entity"] = existing
    plan["proposed_entity"] = {
        "entity_type": proposal["entity_type"],
        "canonical_key": proposal["canonical_key"],
        "display_name": proposal["display_name"],
        "description": "",
        "origin": GraphOrigin.SEMANTIC_PROMOTION,
    }
    plan["proposed_claim"] = {
        "claim_type": claim_type,
        "statement": statement,
        "normalized_statement_hash": identity.statement_hash(statement),
    }
    plan["proposed_bindings"] = _proposed_bindings(
        proposal["asset_id"], proposal["revision_id"],
        proposal["segment_ids"])
    if existing:
        plan["existing_claim"] = store.find_claim_by_hash(
            workspace_id, existing["id"],
            plan["proposed_claim"]["normalized_statement_hash"])
        plan["expected_action"] = \
            PromotionExpectedAction.ATTACH_CLAIM_TO_EXISTING_ENTITY
    else:
        plan["expected_action"] = \
            PromotionExpectedAction.CREATE_ENTITY_AND_CLAIM
    return plan


# ---------------------------------------------------------------------------
# Candidate promotion
# ---------------------------------------------------------------------------
def promote_candidate(workspace_id: str, candidate_id: str, *,
                      actor: str = "", note: str = "",
                      source_command: str = "") -> Dict[str, Any]:
    """Execute an eligible plan transactionally. Idempotent: an already
    promoted candidate returns its existing target and writes nothing."""
    note = _bound(note, MAX_NOTE_CHARS)
    plan = plan_candidate_promotion(workspace_id, candidate_id)
    if plan["expected_action"] == PromotionExpectedAction.ALREADY_PROMOTED:
        return {
            "workspace_id": workspace_id, "candidate_id": candidate_id,
            "status": PromotionStatus.ALREADY_PROMOTED,
            "promotion": plan.get("existing_promotion"),
            "target": plan.get("existing_target"),
            "knowledge_revision": store.current_revision_number(workspace_id),
        }
    if not plan["eligible"]:
        raise PromotionBlocked(candidate_id, plan["blocking_reasons"])

    candidate = plan["candidate"]
    # Full quote re-verification against the immutable store, before the
    # transaction body writes anything.
    evidence_rows = verifier.candidate_evidence_rows(workspace_id, candidate)

    proposed_entity = plan["proposed_entity"]
    proposed_claim = plan["proposed_claim"]

    with store.graph_transaction(
            workspace_id, action=RevisionAction.CANDIDATE_PROMOTION,
            actor=actor,
            summary=f"promote {candidate.get('candidate_kind')} candidate "
                    f"{candidate_id}") as tx:
        existing = plan["existing_entity"]
        if existing:
            entity = tx.get_entity(existing["id"])
        else:
            entity = tx.insert_entity(
                entity_type=proposed_entity["entity_type"],
                canonical_key=proposed_entity["canonical_key"],
                display_name=proposed_entity["display_name"],
                description=proposed_entity["description"],
                origin=GraphOrigin.SEMANTIC_PROMOTION,
                promoted_from_candidate_id=candidate_id)

        existing_claim = plan.get("existing_claim")
        if existing_claim:
            claim = existing_claim
            tx.add_claim_evidence(claim["id"], evidence_rows)
        else:
            claim = tx.insert_claim(
                entity_id=entity["id"],
                claim_type=proposed_claim["claim_type"],
                statement=proposed_claim["statement"],
                normalized_statement_hash=
                    proposed_claim["normalized_statement_hash"],
                origin=GraphOrigin.SEMANTIC_PROMOTION,
                promoted_from_candidate_id=candidate_id,
                evidence=evidence_rows)

        # Aliases from stable identifiers (never a colliding one: identity
        # conflicts blocked the plan already; an alias already on THIS
        # entity is skipped).
        existing_aliases = {a["normalized_alias"] for a in
                            store.list_aliases(workspace_id, entity["id"])}
        for alias in plan["proposed_aliases"]:
            normalized = identity.normalize_alias(alias["alias"])
            if normalized in existing_aliases:
                continue
            tx.insert_alias(entity_id=entity["id"], alias=alias["alias"],
                            normalized_alias=normalized,
                            alias_type=alias["alias_type"],
                            origin=GraphOrigin.SEMANTIC_PROMOTION,
                            evidence_id=evidence_rows[0]["evidence_id"]
                            if evidence_rows else "")

        # Bindings, deduplicated against the entity's existing ones.
        existing_bindings = {
            (b["ref_kind"], b["ref_id"], b["binding_role"])
            for b in store.list_bindings(workspace_id, entity["id"])}
        for binding in plan["proposed_bindings"]:
            key = (binding["ref_kind"], binding["ref_id"],
                   binding["binding_role"])
            if key in existing_bindings:
                continue
            tx.insert_binding(entity_id=entity["id"],
                              ref_kind=binding["ref_kind"],
                              ref_id=binding["ref_id"],
                              binding_role=binding["binding_role"],
                              origin=GraphOrigin.SEMANTIC_PROMOTION)

        target_kind = "entity" if candidate.get("candidate_kind") == \
            SemanticCandidateKind.ENGINEERING_CONCEPT else "claim"
        target_id = entity["id"] if target_kind == "entity" else claim["id"]
        promotion = tx.insert_promotion(
            candidate_kind=PromotionCandidateKind.SEMANTIC_CANDIDATE,
            candidate_id=candidate_id, target_kind=target_kind,
            target_id=target_id, status=PromotionStatus.PROMOTED,
            policy_version=PROMOTION_POLICY_VERSION, actor=actor, note=note)
        tx.insert_decision(
            decision_type=DecisionType.PROMOTE_CANDIDATE,
            target_kind=DecisionTargetKind.CANDIDATE,
            target_id=candidate_id, actor=actor, note=note,
            before={"candidate_id": candidate_id,
                    "review_status": candidate.get("review_status")},
            after={"entity_id": entity["id"], "claim_id": claim["id"],
                   "expected_action": plan["expected_action"]},
            source_command=source_command)
        revision_number = tx.revision_number

    return {
        "workspace_id": workspace_id, "candidate_id": candidate_id,
        "status": PromotionStatus.PROMOTED,
        "expected_action": plan["expected_action"],
        "entity": store.get_entity(workspace_id, entity["id"]),
        "claim": store.get_claim(workspace_id, claim["id"]),
        "promotion": promotion,
        "knowledge_revision": revision_number,
    }


# ---------------------------------------------------------------------------
# Relation promotion
# ---------------------------------------------------------------------------
def _resolve_endpoint(workspace_id: str, ref: Dict[str, Any],
                      candidate_id: Optional[str]) -> Dict[str, Any]:
    """Deterministically resolve one relation endpoint to a canonical
    Entity. Returns {resolved, entity_id, entity, via, reason, ambiguous}.
    """
    out: Dict[str, Any] = {"resolved": False, "entity_id": "", "entity": None,
                           "via": "", "reason": "", "ambiguous": False,
                           "ref": dict(ref or {})}
    # 1. A promoted candidate resolves to its promotion target entity.
    cand_id = candidate_id or (ref.get("id")
                               if (ref or {}).get("kind") == "candidate"
                               else None)
    if cand_id:
        promo = store.find_promotion(
            workspace_id, PromotionCandidateKind.SEMANTIC_CANDIDATE,
            str(cand_id))
        if promo and promo["target_kind"] == "entity":
            entity = store.get_entity(workspace_id, promo["target_id"])
            if entity and entity["lifecycle_status"] in (
                    GraphLifecycleStatus.ACTIVE,):
                out.update({"resolved": True, "entity_id": entity["id"],
                            "entity": entity, "via": "promotion"})
                return out
            if entity and entity["lifecycle_status"] == \
                    GraphLifecycleStatus.MERGED and \
                    entity.get("merged_into_entity_id"):
                target = store.get_entity(workspace_id,
                                          entity["merged_into_entity_id"])
                if target:
                    out.update({"resolved": True, "entity_id": target["id"],
                                "entity": target, "via": "promotion+merge"})
                    return out
        # Fall through to key/alias resolution using the candidate's key.
        cand = semantic_store.get_candidate(workspace_id, str(cand_id))
        key = str((cand or {}).get("stable_key") or (ref or {}).get("key")
                  or "").strip()
    else:
        key = str((ref or {}).get("key") or "").strip()

    kind = str((ref or {}).get("kind") or "")
    # 2. A symbol ref resolves through the projector's segment binding.
    if kind == "symbol" and (ref or {}).get("id"):
        bindings = store.find_bindings_by_ref(
            workspace_id, BindingRefKind.SEGMENT, str(ref["id"]))
        holders = []
        for b in bindings:
            if b["status"] != "active":
                continue
            ent = store.get_entity(workspace_id, b["entity_id"])
            if ent and ent["lifecycle_status"] == GraphLifecycleStatus.ACTIVE:
                holders.append(ent)
        unique = {e["id"]: e for e in holders}
        if len(unique) == 1:
            entity = next(iter(unique.values()))
            out.update({"resolved": True, "entity_id": entity["id"],
                        "entity": entity, "via": "segment-binding"})
            return out
        if len(unique) > 1:
            out.update({"ambiguous": True, "reason":
                        f"segment {ref['id']} is bound to "
                        f"{len(unique)} active entities"})
            return out
    # 3. Exact alias / canonical-key resolution by the ref key.
    if key:
        holders = store.find_entity_by_alias(workspace_id,
                                             identity.normalize_alias(key))
        if len(holders) == 1:
            out.update({"resolved": True, "entity_id": holders[0]["id"],
                        "entity": holders[0], "via": "alias"})
            return out
        if len(holders) > 1:
            out.update({"ambiguous": True, "reason":
                        f"key {key!r} resolves to {len(holders)} active "
                        f"entities"})
            return out
        out["reason"] = (f"key {key!r} resolves to no canonical entity "
                         f"(promote the referenced candidate first, or seed "
                         f"the graph)")
        return out
    out["reason"] = "endpoint reference carries no resolvable identity"
    return out


def _relation_blocking_reasons(workspace_id: str, candidate: Dict[str, Any],
                               source: Dict[str, Any],
                               target: Dict[str, Any]) -> List[str]:
    reasons = _base_blocking_reasons(workspace_id, candidate)
    relation_type = RELATION_CANDIDATE_TYPE_TO_RELATION_TYPE.get(
        str(candidate.get("relation_type") or ""))
    if not relation_type:
        reasons.append(
            f"unsupported relation type: {candidate.get('relation_type')!r}")
    if not (candidate.get("evidence") or []):
        reasons.append("relation candidate has no evidence joins")
    for ev in candidate.get("evidence") or []:
        if not db.get_evidence(workspace_id, str(ev.get("evidence_id") or "")):
            reasons.append(
                f"evidence {ev.get('evidence_id')} does not exist in this "
                f"workspace")
    if not source["resolved"]:
        reasons.append("source endpoint does not resolve unambiguously"
                       + (f": {source['reason']}" if source.get("reason")
                          else ""))
    if not target["resolved"]:
        reasons.append("target endpoint does not resolve unambiguously"
                       + (f": {target['reason']}" if target.get("reason")
                          else ""))
    if source["resolved"] and target["resolved"] and \
            source["entity_id"] == target["entity_id"]:
        reasons.append("source and target resolve to the same entity "
                       "(self-relations are rejected)")
    prior = store.find_promotion(
        workspace_id, PromotionCandidateKind.RELATION_CANDIDATE,
        candidate["id"])
    if prior:
        reasons.append("relation candidate is already promoted")
    return reasons


def plan_relation_promotion(workspace_id: str,
                            relation_candidate_id: str) -> Dict[str, Any]:
    """Deterministic dry-run for one Relation Candidate."""
    semantic_store.reconcile_staleness(workspace_id)
    candidate = semantic_store.get_relation(workspace_id,
                                            relation_candidate_id)
    if not candidate:
        from ..semantic.errors import CandidateNotFound
        raise CandidateNotFound(
            f"relation candidate not found: {relation_candidate_id!r}",
            details={"candidate_id": relation_candidate_id})

    source = _resolve_endpoint(workspace_id,
                               candidate.get("source_ref") or {},
                               candidate.get("source_candidate_id"))
    target = _resolve_endpoint(workspace_id,
                               candidate.get("target_ref") or {},
                               candidate.get("target_candidate_id"))
    prior = store.find_promotion(
        workspace_id, PromotionCandidateKind.RELATION_CANDIDATE,
        relation_candidate_id)
    reasons = _relation_blocking_reasons(workspace_id, candidate, source,
                                         target)
    plan: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "relation_candidate_id": relation_candidate_id,
        "candidate": candidate,
        "eligible": not reasons,
        "blocking_reasons": reasons,
        "source_resolution": source,
        "target_resolution": target,
        "proposed_relation": None,
        "existing_relation": None,
        "promotion_policy_version": PROMOTION_POLICY_VERSION,
    }
    if prior:
        plan["expected_action"] = PromotionExpectedAction.ALREADY_PROMOTED
        plan["existing_promotion"] = prior
        plan["existing_relation"] = store.get_relation(workspace_id,
                                                       prior["target_id"])
        plan["eligible"] = False
        return plan
    if reasons:
        plan["expected_action"] = PromotionExpectedAction.BLOCKED
        return plan
    relation_type = RELATION_CANDIDATE_TYPE_TO_RELATION_TYPE[
        candidate["relation_type"]]
    plan["proposed_relation"] = {
        "source_entity_id": source["entity_id"],
        "target_entity_id": target["entity_id"],
        "relation_type": relation_type,
        "relation_state": RelationState.CONFIRMED,
        "confidence": candidate.get("confidence", "low"),
        "origin": GraphOrigin.SEMANTIC_PROMOTION,
    }
    existing = store.find_active_relation(workspace_id, source["entity_id"],
                                          target["entity_id"], relation_type)
    plan["existing_relation"] = existing
    plan["expected_action"] = PromotionExpectedAction.CREATE_RELATION
    return plan


def promote_relation(workspace_id: str, relation_candidate_id: str, *,
                     actor: str = "", note: str = "",
                     source_command: str = "") -> Dict[str, Any]:
    """Execute an eligible relation plan transactionally. Idempotent."""
    note = _bound(note, MAX_NOTE_CHARS)
    plan = plan_relation_promotion(workspace_id, relation_candidate_id)
    if plan["expected_action"] == PromotionExpectedAction.ALREADY_PROMOTED:
        return {
            "workspace_id": workspace_id,
            "relation_candidate_id": relation_candidate_id,
            "status": PromotionStatus.ALREADY_PROMOTED,
            "promotion": plan.get("existing_promotion"),
            "relation": plan.get("existing_relation"),
            "knowledge_revision": store.current_revision_number(workspace_id),
        }
    if not plan["eligible"]:
        raise PromotionBlocked(relation_candidate_id,
                               plan["blocking_reasons"])

    candidate = plan["candidate"]
    evidence_rows = verifier.candidate_evidence_rows(workspace_id, candidate)
    proposed = plan["proposed_relation"]

    with store.graph_transaction(
            workspace_id, action=RevisionAction.RELATION_PROMOTION,
            actor=actor,
            summary=f"promote relation candidate "
                    f"{relation_candidate_id}") as tx:
        existing = plan["existing_relation"]
        if existing:
            relation = existing
            # Reuse the active relation; upgrade its state to confirmed and
            # attach the newly verified evidence.
            updates: Dict[str, Any] = {}
            if existing["relation_state"] != RelationState.CONFIRMED:
                updates["relation_state"] = RelationState.CONFIRMED
            if not existing.get("promoted_from_relation_candidate_id"):
                updates["promoted_from_relation_candidate_id"] = \
                    relation_candidate_id
            if updates:
                tx.update_relation(existing["id"], **updates)
            tx.add_relation_evidence(existing["id"], evidence_rows)
        else:
            relation = tx.insert_relation(
                source_entity_id=proposed["source_entity_id"],
                target_entity_id=proposed["target_entity_id"],
                relation_type=proposed["relation_type"],
                relation_state=RelationState.CONFIRMED,
                confidence=proposed["confidence"],
                origin=GraphOrigin.SEMANTIC_PROMOTION,
                promoted_from_relation_candidate_id=relation_candidate_id,
                evidence=evidence_rows)
        promotion = tx.insert_promotion(
            candidate_kind=PromotionCandidateKind.RELATION_CANDIDATE,
            candidate_id=relation_candidate_id, target_kind="relation",
            target_id=relation["id"], status=PromotionStatus.PROMOTED,
            policy_version=PROMOTION_POLICY_VERSION, actor=actor, note=note)
        tx.insert_decision(
            decision_type=DecisionType.PROMOTE_RELATION,
            target_kind=DecisionTargetKind.RELATION_CANDIDATE,
            target_id=relation_candidate_id, actor=actor, note=note,
            before={"relation_candidate_id": relation_candidate_id},
            after={"relation_id": relation["id"],
                   "relation_type": relation["relation_type"],
                   "relation_state": RelationState.CONFIRMED},
            source_command=source_command)
        revision_number = tx.revision_number

    return {
        "workspace_id": workspace_id,
        "relation_candidate_id": relation_candidate_id,
        "status": PromotionStatus.PROMOTED,
        "relation": store.get_relation(workspace_id, relation["id"]),
        "promotion": promotion,
        "knowledge_revision": revision_number,
    }


__all__ = [
    "plan_candidate_promotion", "promote_candidate",
    "plan_relation_promotion", "promote_relation",
    "MAX_STATEMENT_CHARS", "MAX_NOTE_CHARS",
]
