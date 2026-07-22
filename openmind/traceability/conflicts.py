"""Canonical Conflict management: deterministic scanning, deduplication,
suppression, lifecycle governance and the explicit promotion of confirmed
Phase 4 Conflict Candidates.

GOVERNANCE DISCIPLINE
---------------------
Conflict creation and every lifecycle decision are graph-governance writes:
each runs inside one Phase 5 graph transaction, records one
``knowledge_decisions`` row (actor caller-supplied, never inferred) plus one
``engineering_conflict_decisions`` row for lifecycle transitions, and mints
exactly one Knowledge Revision. Observing an IDENTICAL conflict again
touches metadata only and mints nothing (spec §24). Resolution never
rewrites Claims or Relations — the human makes graph changes separately and
a later scan confirms the incompatible state is gone.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..domain.errors import InvalidRequest
from ..knowledge import store as kg
from ..knowledge import verifier
from ..knowledge.identity import quote_hash
from ..knowledge.vocabularies import (DecisionTargetKind, DecisionType,
                                      PromotionCandidateKind,
                                      PromotionStatus, RevisionAction)
from ..semantic import store as semantic_store
from ..semantic.models import (EvidenceVerificationStatus, LifecycleStatus,
                               ReviewStatus)
from . import CONFLICT_FRAMEWORK_VERSION
from .detectors import (ALL_DETECTORS, SUPPORTED_CATEGORIES,
                        ConflictDetectionContext, conflict_dedup_key,
                        suppression_fingerprint)
from .errors import ConflictNotFound, ConflictPromotionBlocked, \
    ConflictStateInvalid
from .facts import collect_comparable_facts
from . import store
from .vocabularies import (ConflictDecisionType, ConflictObjectKind,
                           ConflictOrigin, ConflictResolutionType,
                           ConflictStatus)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ---------------------------------------------------------------------------
# Scan planning
# ---------------------------------------------------------------------------
def plan_scan(workspace_id: str) -> Dict[str, Any]:
    """What a scan WOULD examine: per-detector plans. Writes nothing."""
    revision = kg.current_revision_number(workspace_id)
    plans = []
    for detector in ALL_DETECTORS:
        plans.append(detector.plan(workspace_id, revision).as_dict())
    return {
        "workspace_id": workspace_id,
        "knowledge_revision": revision,
        "framework_version": CONFLICT_FRAMEWORK_VERSION,
        "detectors": plans,
        "supported_categories": sorted(SUPPORTED_CATEGORIES),
    }


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def _draft_fingerprint(draft) -> str:
    return suppression_fingerprint(
        [draft.left_value, draft.right_value],
        [quote_hash(ev.get("quote", "")) for ev in draft.evidence])


def _draft_dedup_key(workspace_id: str, draft) -> str:
    return conflict_dedup_key(
        workspace_id, draft.category, draft.subject_key,
        [o["object_id"] for o in draft.objects], draft.property,
        draft.detector_name)


def scan_conflicts(workspace_id: str, *, actor: str = "",
                   progress: Optional[Callable[[str], None]] = None,
                   cancelled: Optional[Callable[[], bool]] = None
                   ) -> Dict[str, Any]:
    """One deterministic scan: collect facts, run every detector (failure
    isolated), verify evidence, deduplicate against existing conflicts,
    persist changes in ONE graph transaction, reconcile lifecycle."""
    def step(name: str) -> None:
        if progress:
            progress(name)

    revision = kg.current_revision_number(workspace_id)
    step("planning")
    detector_errors: List[Dict[str, str]] = []

    step("collecting-comparable-facts")
    facts = collect_comparable_facts(workspace_id)
    context = ConflictDetectionContext(workspace_id, revision, facts)

    step("running-detectors")
    drafts = []
    for detector in ALL_DETECTORS:
        if cancelled and cancelled():
            return {"workspace_id": workspace_id, "status": "cancelled",
                    "knowledge_revision": revision}
        try:
            drafts.extend(detector.detect(context))
        except Exception as exc:
            detector_errors.append({"detector": detector.name,
                                    "error": str(exc)})

    step("verifying-evidence")
    verified_drafts = []
    dropped_unverifiable = 0
    for draft in drafts:
        refs = [{"evidence_id": ev["evidence_id"],
                 "quote": ev.get("quote", "")} for ev in draft.evidence
                if ev.get("evidence_id")]
        if refs:
            try:
                verifier.verify_evidence_refs(workspace_id, refs,
                                              require_nonempty=True)
            except Exception:
                dropped_unverifiable += 1
                continue
        verified_drafts.append(draft)

    step("deduplicating")
    # One CURRENT conflict represents one dispute: drafts are grouped by
    # their subject identity (category + subject + property + detector).
    # Several simultaneous value pairs on one subject (2s vs 5s, 2s vs
    # 2000ms, ...) are facets of the SAME dispute — persisting each pair
    # would both spam governance and ping-pong supersessions across scans.
    to_create: List[Any] = []
    to_supersede: List[Dict[str, Any]] = []
    observed_unchanged: List[str] = []
    suppressed: List[str] = []
    to_reopen: List[Dict[str, Any]] = []
    absorbed = 0
    seen_keys: set = set()
    superseding_ids: set = set()
    groups: Dict[Any, List[Any]] = {}
    for draft in verified_drafts:
        dedup_key = _draft_dedup_key(workspace_id, draft)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        identity = (draft.category, draft.subject_key, draft.property,
                    draft.detector_name)
        groups.setdefault(identity, []).append((draft, dedup_key))
    for identity in sorted(groups, key=str):
        drafts = sorted(groups[identity], key=lambda d: d[1])
        exact = None
        for draft, dedup_key in drafts:
            match = store.find_conflict_by_dedup_key(workspace_id,
                                                     dedup_key)
            if match is not None and \
                    match["status"] != ConflictStatus.SUPERSEDED:
                exact = (draft, dedup_key, match)
                break
        if exact is not None:
            draft, dedup_key, existing = exact
            absorbed += len(drafts) - 1
            fingerprint = _draft_fingerprint(draft)
            existing_fingerprint = (existing.get("metadata") or {}).get(
                "suppression_fingerprint", "")
            if existing_fingerprint == fingerprint:
                if existing["status"] == ConflictStatus.DISMISSED:
                    suppressed.append(existing["id"])
                    store.touch_conflict_observation(
                        workspace_id, existing["id"], revision)
                elif existing["status"] == ConflictStatus.RESOLVED:
                    # Resolved but the identical incompatible state is
                    # still detected: deterministic reopen rule.
                    to_reopen.append({"conflict": existing,
                                      "reason": "re-detected unchanged "
                                                "after resolution"})
                elif existing["status"] == ConflictStatus.STALE:
                    to_reopen.append({"conflict": existing,
                                      "reason": "re-detected after having "
                                                "gone stale"})
                else:
                    observed_unchanged.append(existing["id"])
                    store.touch_conflict_observation(
                        workspace_id, existing["id"], revision)
            else:
                # Same objects, changed evidence: supersede (history kept).
                to_supersede.append({"old": existing, "draft": draft,
                                     "dedup_key": dedup_key,
                                     "fingerprint": fingerprint})
                superseding_ids.add(existing["id"])
            continue
        # No draft of the group matches exactly. A changed compared VALUE
        # means new claim ids and therefore a new dedup key — recognize
        # the same dispute at the subject level so history supersedes
        # instead of accumulating parallel conflicts. A DISMISSED subject
        # match whose facts changed no longer suppresses (spec §25) and a
        # fresh conflict opens beside it.
        draft, dedup_key = drafts[0]
        absorbed += len(drafts) - 1
        fingerprint = _draft_fingerprint(draft)
        subject_match = store.find_current_conflict_by_subject(
            workspace_id, draft.category, draft.subject_key,
            draft.detector_name, draft.property)
        if subject_match and subject_match["status"] in (
                ConflictStatus.OPEN, ConflictStatus.UNDER_REVIEW,
                ConflictStatus.ACCEPTED_RISK, ConflictStatus.STALE):
            to_supersede.append({"old": subject_match, "draft": draft,
                                 "dedup_key": dedup_key,
                                 "fingerprint": fingerprint})
            superseding_ids.add(subject_match["id"])
        else:
            to_create.append((draft, dedup_key, fingerprint))

    # Reconciliation inputs: current deterministic conflicts NOT re-detected
    # this scan (and not about to be superseded by it).
    detected_keys = seen_keys
    stale_candidates = []
    expired_risks = []
    for conflict in store.list_conflicts(workspace_id, limit=1000):
        if conflict["origin"] != ConflictOrigin.DETERMINISTIC:
            continue
        if conflict["id"] in superseding_ids:
            continue
        if conflict["status"] in (ConflictStatus.OPEN,
                                  ConflictStatus.UNDER_REVIEW):
            if conflict["dedup_key"] not in detected_keys:
                stale_candidates.append(conflict)
        if conflict["status"] == ConflictStatus.ACCEPTED_RISK:
            expiry = (conflict.get("metadata") or {}).get(
                "accepted_risk_expires_at", "")
            if expiry and expiry < _now():
                expired_risks.append(conflict)

    step("persisting-conflicts")
    created_ids: List[str] = []
    superseded_ids: List[str] = []
    reopened_ids: List[str] = []
    staled_ids: List[str] = []
    wrote = bool(to_create or to_supersede or to_reopen or
                 stale_candidates or expired_risks)
    if wrote:
        with kg.graph_transaction(
                workspace_id, action=RevisionAction.CONFLICT_SCAN,
                actor=actor, summary="deterministic conflict scan") as tx:
            def _new_conflict(draft, dedup_key, fingerprint,
                              extra_metadata=None):
                conflict_id = store.insert_conflict_tx(tx, {
                    "category": draft.category,
                    "subject_key": draft.subject_key,
                    "title": draft.title,
                    "description": draft.description,
                    "severity": draft.severity,
                    "status": ConflictStatus.OPEN,
                    "origin": ConflictOrigin.DETERMINISTIC,
                    "detector_name": draft.detector_name,
                    "detector_version": draft.detector_version,
                    "dedup_key": dedup_key,
                    "metadata": {**draft.metadata,
                                 "property": draft.property,
                                 "suppression_fingerprint": fingerprint,
                                 "left_value": draft.left_value,
                                 "right_value": draft.right_value,
                                 **(extra_metadata or {})},
                    "objects": draft.objects,
                    "evidence": [{**ev,
                                  "quote_hash":
                                      quote_hash(ev.get("quote", ""))}
                                 for ev in draft.evidence],
                })
                tx.insert_decision(
                    decision_type=DecisionType.CONFLICT_DETECT,
                    target_kind=DecisionTargetKind.CONFLICT,
                    target_id=conflict_id, actor=actor,
                    note=f"detected by {draft.detector_name} "
                         f"{draft.detector_version}",
                    after={"category": draft.category,
                           "subject_key": draft.subject_key,
                           "severity": draft.severity},
                    source_command="conflict scan")
                return conflict_id

            for draft, dedup_key, fingerprint in to_create:
                created_ids.append(_new_conflict(draft, dedup_key,
                                                 fingerprint))
            for entry in to_supersede:
                old = entry["old"]
                new_id = _new_conflict(
                    entry["draft"], entry["dedup_key"],
                    entry["fingerprint"],
                    extra_metadata={"supersedes_conflict_id": old["id"]})
                store.update_conflict_tx(
                    tx, old["id"], status=ConflictStatus.SUPERSEDED,
                    superseded_by_conflict_id=new_id)
                kg_decision = tx.insert_decision(
                    decision_type=DecisionType.CONFLICT_SUPERSEDE,
                    target_kind=DecisionTargetKind.CONFLICT,
                    target_id=old["id"], actor=actor,
                    note="compared values or evidence changed",
                    before={"status": old["status"]},
                    after={"status": ConflictStatus.SUPERSEDED,
                           "superseded_by": new_id},
                    source_command="conflict scan")
                store.insert_conflict_decision_tx(
                    tx, conflict_id=old["id"],
                    decision=ConflictDecisionType.SUPERSEDE,
                    actor=actor,
                    note="compared values or evidence changed",
                    before_status=old["status"],
                    after_status=ConflictStatus.SUPERSEDED,
                    resolution={"superseded_by": new_id},
                    knowledge_decision_id=kg_decision["id"])
                superseded_ids.append(old["id"])
                created_ids.append(new_id)
            for entry in to_reopen:
                conflict = entry["conflict"]
                store.update_conflict_tx(tx, conflict["id"],
                                         status=ConflictStatus.OPEN,
                                         resolved_at=None, stale_at=None)
                kg_decision = tx.insert_decision(
                    decision_type=DecisionType.CONFLICT_REOPEN,
                    target_kind=DecisionTargetKind.CONFLICT,
                    target_id=conflict["id"], actor=actor,
                    note=entry["reason"],
                    before={"status": conflict["status"]},
                    after={"status": ConflictStatus.OPEN},
                    source_command="conflict scan")
                store.insert_conflict_decision_tx(
                    tx, conflict_id=conflict["id"],
                    decision=ConflictDecisionType.REOPEN, actor=actor,
                    note=entry["reason"],
                    before_status=conflict["status"],
                    after_status=ConflictStatus.OPEN,
                    knowledge_decision_id=kg_decision["id"])
                reopened_ids.append(conflict["id"])
            for conflict in stale_candidates:
                store.update_conflict_tx(tx, conflict["id"],
                                         status=ConflictStatus.STALE,
                                         stale_at=_now())
                tx.insert_decision(
                    decision_type=DecisionType.CONFLICT_REVIEW,
                    target_kind=DecisionTargetKind.CONFLICT,
                    target_id=conflict["id"], actor=actor,
                    note="no longer detected; detection basis gone",
                    before={"status": conflict["status"]},
                    after={"status": ConflictStatus.STALE},
                    source_command="conflict scan")
                staled_ids.append(conflict["id"])
            for conflict in expired_risks:
                store.update_conflict_tx(tx, conflict["id"],
                                         status=ConflictStatus.OPEN)
                kg_decision = tx.insert_decision(
                    decision_type=DecisionType.CONFLICT_REOPEN,
                    target_kind=DecisionTargetKind.CONFLICT,
                    target_id=conflict["id"], actor=actor,
                    note="accepted risk expired",
                    before={"status": ConflictStatus.ACCEPTED_RISK},
                    after={"status": ConflictStatus.OPEN},
                    source_command="conflict scan")
                store.insert_conflict_decision_tx(
                    tx, conflict_id=conflict["id"],
                    decision=ConflictDecisionType.REOPEN, actor=actor,
                    note="accepted risk expired",
                    before_status=ConflictStatus.ACCEPTED_RISK,
                    after_status=ConflictStatus.OPEN,
                    knowledge_decision_id=kg_decision["id"])
                reopened_ids.append(conflict["id"])

    step("reconciling-conflicts")
    step("done")
    status = "partial" if detector_errors else "done"
    return {
        "workspace_id": workspace_id,
        "status": status,
        "knowledge_revision": kg.current_revision_number(workspace_id),
        "analyzed_revision": revision,
        "framework_version": CONFLICT_FRAMEWORK_VERSION,
        "facts_collected": len(facts),
        "drafts": len(drafts),
        "dropped_unverifiable": dropped_unverifiable,
        "created": created_ids,
        "superseded": superseded_ids,
        "reopened": reopened_ids,
        "staled": staled_ids,
        "observed_unchanged": observed_unchanged,
        "suppressed": suppressed,
        "absorbed_drafts": absorbed,
        "detector_errors": detector_errors,
    }


# ---------------------------------------------------------------------------
# Lifecycle governance
# ---------------------------------------------------------------------------
_TRANSITIONS = {
    ConflictDecisionType.START_REVIEW: (
        {ConflictStatus.OPEN}, ConflictStatus.UNDER_REVIEW),
    ConflictDecisionType.ACCEPT_RISK: (
        {ConflictStatus.OPEN, ConflictStatus.UNDER_REVIEW},
        ConflictStatus.ACCEPTED_RISK),
    ConflictDecisionType.RESOLVE: (
        {ConflictStatus.OPEN, ConflictStatus.UNDER_REVIEW,
         ConflictStatus.ACCEPTED_RISK}, ConflictStatus.RESOLVED),
    ConflictDecisionType.DISMISS: (
        {ConflictStatus.OPEN, ConflictStatus.UNDER_REVIEW},
        ConflictStatus.DISMISSED),
    ConflictDecisionType.REOPEN: (
        {ConflictStatus.ACCEPTED_RISK, ConflictStatus.RESOLVED,
         ConflictStatus.DISMISSED, ConflictStatus.STALE},
        ConflictStatus.OPEN),
}

_DECISION_TO_KG = {
    ConflictDecisionType.START_REVIEW: DecisionType.CONFLICT_REVIEW,
    ConflictDecisionType.ACCEPT_RISK: DecisionType.CONFLICT_ACCEPT_RISK,
    ConflictDecisionType.RESOLVE: DecisionType.CONFLICT_RESOLVE,
    ConflictDecisionType.DISMISS: DecisionType.CONFLICT_DISMISS,
    ConflictDecisionType.REOPEN: DecisionType.CONFLICT_REOPEN,
}


def _governance(workspace_id: str, conflict_id: str, decision: str, *,
                actor: str, note: str,
                resolution: Optional[Dict[str, Any]] = None,
                metadata_updates: Optional[Dict[str, Any]] = None,
                extra_fields: Optional[Dict[str, Any]] = None,
                source_command: str = "") -> Dict[str, Any]:
    """One conflict lifecycle transition: validated, transactional, doubly
    audited (conflict ledger + knowledge ledger), one Knowledge Revision."""
    actor = str(actor or "").strip()
    note = str(note or "").strip()
    if not actor:
        raise InvalidRequest("actor is required for conflict governance")
    if not note:
        raise InvalidRequest("note is required for conflict governance")
    conflict = store.get_conflict(workspace_id, conflict_id)
    if not conflict:
        raise ConflictNotFound(conflict_id, workspace_id=workspace_id)
    allowed_from, to_status = _TRANSITIONS[decision]
    if conflict["status"] not in allowed_from:
        raise ConflictStateInvalid(
            f"cannot {decision} a conflict in status "
            f"{conflict['status']!r} (allowed: "
            f"{', '.join(sorted(allowed_from))})",
            details={"conflict_id": conflict_id,
                     "status": conflict["status"],
                     "decision": decision})
    metadata = dict(conflict.get("metadata") or {})
    metadata.update(metadata_updates or {})
    fields: Dict[str, Any] = {"status": to_status, "metadata": metadata}
    fields.update(extra_fields or {})
    with kg.graph_transaction(
            workspace_id, action=RevisionAction.CONFLICT_GOVERNANCE,
            actor=actor, summary=f"conflict {decision}") as tx:
        store.update_conflict_tx(tx, conflict_id, **fields)
        decision_row = tx.insert_decision(
            decision_type=_DECISION_TO_KG[decision],
            target_kind=DecisionTargetKind.CONFLICT,
            target_id=conflict_id, actor=actor, note=note,
            before={"status": conflict["status"]},
            after={"status": to_status,
                   **({"resolution": resolution} if resolution else {})},
            source_command=source_command)
        store.insert_conflict_decision_tx(
            tx, conflict_id=conflict_id, decision=decision, actor=actor,
            note=note, before_status=conflict["status"],
            after_status=to_status, resolution=resolution or {},
            knowledge_decision_id=decision_row["id"])
        revision = tx.revision_number
    return {"workspace_id": workspace_id,
            "conflict": store.get_conflict(workspace_id, conflict_id),
            "knowledge_revision": revision}


def start_review(workspace_id: str, conflict_id: str, *, actor: str,
                 note: str, source_command: str = "") -> Dict[str, Any]:
    return _governance(workspace_id, conflict_id,
                       ConflictDecisionType.START_REVIEW, actor=actor,
                       note=note, source_command=source_command)


def accept_risk(workspace_id: str, conflict_id: str, *, actor: str,
                note: str, expires_at: str = "",
                follow_up: str = "",
                source_command: str = "") -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"accepted_risk_by": actor,
                                "accepted_risk_at": _now()}
    if expires_at:
        metadata["accepted_risk_expires_at"] = str(expires_at)
    if follow_up:
        metadata["accepted_risk_follow_up"] = str(follow_up)[:500]
    return _governance(workspace_id, conflict_id,
                       ConflictDecisionType.ACCEPT_RISK, actor=actor,
                       note=note, metadata_updates=metadata,
                       resolution={"expires_at": expires_at,
                                   "follow_up": follow_up},
                       source_command=source_command)


def resolve(workspace_id: str, conflict_id: str, *, actor: str, note: str,
            resolution_type: str,
            evidence: Sequence[Dict[str, Any]] = (),
            source_command: str = "") -> Dict[str, Any]:
    resolution_type = str(resolution_type or "").strip().lower()
    if resolution_type not in ConflictResolutionType.VALUES:
        raise InvalidRequest(
            f"unknown resolution type {resolution_type!r} (allowed: "
            f"{', '.join(sorted(ConflictResolutionType.VALUES))})")
    rows = verifier.verify_evidence_refs(workspace_id, list(evidence),
                                         require_nonempty=True)
    result = _governance(
        workspace_id, conflict_id, ConflictDecisionType.RESOLVE,
        actor=actor, note=note,
        resolution={"resolution_type": resolution_type,
                    "evidence": [r["evidence_id"] for r in rows]},
        metadata_updates={"resolution_type": resolution_type},
        extra_fields={"resolved_at": _now()},
        source_command=source_command)
    return result


def dismiss(workspace_id: str, conflict_id: str, *, actor: str, note: str,
            source_command: str = "") -> Dict[str, Any]:
    """Dismissal records the suppression fingerprint of the CURRENT
    compared values + evidence: an unchanged re-detection stays
    suppressed; changed facts no longer match and create a new conflict."""
    conflict = store.get_conflict(workspace_id, conflict_id)
    if not conflict:
        raise ConflictNotFound(conflict_id, workspace_id=workspace_id)
    metadata = conflict.get("metadata") or {}
    fingerprint = metadata.get("suppression_fingerprint", "")
    if not fingerprint:
        fingerprint = suppression_fingerprint(
            [str(metadata.get("left_value", "")),
             str(metadata.get("right_value", ""))],
            [ev["quote_hash"] for ev in conflict.get("evidence") or []])
    return _governance(
        workspace_id, conflict_id, ConflictDecisionType.DISMISS,
        actor=actor, note=note,
        metadata_updates={"suppression_fingerprint": fingerprint,
                          "dismissed_reason": note[:500]},
        source_command=source_command)


def reopen(workspace_id: str, conflict_id: str, *, actor: str, note: str,
           source_command: str = "") -> Dict[str, Any]:
    return _governance(workspace_id, conflict_id,
                       ConflictDecisionType.REOPEN, actor=actor,
                       note=note, extra_fields={"resolved_at": None},
                       source_command=source_command)


# ---------------------------------------------------------------------------
# Conflict Candidate promotion
# ---------------------------------------------------------------------------
def _candidate_blocking_reasons(workspace_id: str,
                                candidate: Dict[str, Any]
                                ) -> List[str]:
    reasons: List[str] = []
    if candidate.get("review_status") != ReviewStatus.CONFIRMED:
        reasons.append(f"review_status is "
                       f"{candidate.get('review_status')!r} "
                       f"(must be confirmed)")
    if candidate.get("lifecycle_status") != LifecycleStatus.ACTIVE:
        reasons.append(f"lifecycle_status is "
                       f"{candidate.get('lifecycle_status')!r} "
                       f"(must be active; a stale candidate must be "
                       f"re-analyzed)")
    if candidate.get("evidence_status") != \
            EvidenceVerificationStatus.VERIFIED:
        reasons.append(f"evidence_status is "
                       f"{candidate.get('evidence_status')!r} "
                       f"(must be verified)")
    if candidate.get("category") not in SUPPORTED_CATEGORIES:
        reasons.append(f"category {candidate.get('category')!r} is not a "
                       f"supported canonical conflict category")
    if not (candidate.get("evidence") or []):
        reasons.append("candidate has no evidence joins")
    prior = kg.find_promotion(workspace_id,
                              PromotionCandidateKind.CONFLICT_CANDIDATE,
                              candidate["id"])
    if prior:
        reasons.append("candidate is already promoted")
    return reasons


def _resolve_candidate_objects(workspace_id: str,
                               candidate: Dict[str, Any]
                               ) -> Dict[str, Any]:
    """Resolve the candidate's left/right semantic candidates to their
    PROMOTED canonical objects. A referenced candidate that is not
    promoted blocks — a canonical conflict must reference canonical
    objects."""
    objects: List[Dict[str, str]] = []
    problems: List[str] = []
    for side, role in (("left_candidate_id", "left"),
                       ("right_candidate_id", "right")):
        candidate_id = candidate.get(side)
        if not candidate_id:
            continue
        promotion = kg.find_promotion(
            workspace_id, PromotionCandidateKind.SEMANTIC_CANDIDATE,
            candidate_id)
        if not promotion:
            problems.append(
                f"{side} {candidate_id} is not promoted to the canonical "
                f"graph; promote it first so the conflict can reference "
                f"canonical objects")
            continue
        target_kind = promotion["target_kind"]
        target_id = promotion["target_id"]
        resolved = None
        if target_kind == "claim":
            resolved = kg.get_claim(workspace_id, target_id)
            kind = ConflictObjectKind.CLAIM
        else:
            resolved = kg.get_entity(workspace_id, target_id)
            kind = ConflictObjectKind.ENTITY
        if not resolved:
            problems.append(f"promoted target {target_id} of {side} does "
                            f"not resolve in this workspace")
            continue
        objects.append({"object_kind": kind, "object_id": target_id,
                        "role": role})
        if kind == ConflictObjectKind.CLAIM:
            objects.append({"object_kind": ConflictObjectKind.ENTITY,
                            "object_id": resolved["entity_id"],
                            "role": "subject"})
    return {"objects": objects, "problems": problems}


def plan_promotion(workspace_id: str, candidate_id: str) -> Dict[str, Any]:
    """Deterministic promotion dry-run. No write, no provider call."""
    semantic_store.reconcile_staleness(workspace_id)
    candidate = semantic_store.get_conflict(workspace_id, candidate_id)
    if not candidate:
        from ..semantic.errors import CandidateNotFound
        raise CandidateNotFound(
            f"conflict candidate not found: {candidate_id!r}",
            details={"candidate_id": candidate_id})
    reasons = _candidate_blocking_reasons(workspace_id, candidate)
    resolution = _resolve_candidate_objects(workspace_id, candidate)
    reasons.extend(resolution["problems"])
    prior = kg.find_promotion(workspace_id,
                              PromotionCandidateKind.CONFLICT_CANDIDATE,
                              candidate_id)
    plan: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "candidate_id": candidate_id,
        "candidate": candidate,
        "eligible": not reasons,
        "blocking_reasons": reasons,
        "proposed_objects": resolution["objects"],
        "evidence": candidate.get("evidence") or [],
    }
    if prior:
        plan["expected_action"] = "already-promoted"
        existing = store.find_conflict_by_candidate(workspace_id,
                                                    candidate_id)
        plan["existing_conflict"] = existing
    elif reasons:
        plan["expected_action"] = "blocked"
    else:
        plan["expected_action"] = "create-conflict"
    return plan


def promote_candidate(workspace_id: str, candidate_id: str, *, actor: str,
                      note: str,
                      source_command: str = "") -> Dict[str, Any]:
    """Promote one confirmed, active, verified Conflict Candidate into a
    governed canonical Conflict. Transactional, idempotent, one Knowledge
    Revision."""
    actor = str(actor or "").strip()
    note = str(note or "").strip()
    if not actor:
        raise InvalidRequest("actor is required for conflict promotion")
    if not note:
        raise InvalidRequest("note is required for conflict promotion")
    # Re-check staleness immediately before promoting (spec §20 step 1).
    semantic_store.reconcile_staleness(workspace_id)
    candidate = semantic_store.get_conflict(workspace_id, candidate_id)
    if not candidate:
        from ..semantic.errors import CandidateNotFound
        raise CandidateNotFound(
            f"conflict candidate not found: {candidate_id!r}",
            details={"candidate_id": candidate_id})
    prior = kg.find_promotion(workspace_id,
                              PromotionCandidateKind.CONFLICT_CANDIDATE,
                              candidate_id)
    if prior:
        existing = store.find_conflict_by_candidate(workspace_id,
                                                    candidate_id)
        return {"workspace_id": workspace_id,
                "status": PromotionStatus.ALREADY_PROMOTED,
                "conflict": existing, "promotion": prior,
                "knowledge_revision":
                    kg.current_revision_number(workspace_id)}
    reasons = _candidate_blocking_reasons(workspace_id, candidate)
    resolution = _resolve_candidate_objects(workspace_id, candidate)
    reasons.extend(resolution["problems"])
    if reasons:
        raise ConflictPromotionBlocked(candidate_id, reasons)
    # Verify quotes against the immutable store (spec §20 step 3).
    refs = [{"evidence_id": ev.get("evidence_id"),
             "quote": ev.get("quote", "")}
            for ev in candidate.get("evidence") or []]
    evidence_rows = verifier.verify_evidence_refs(workspace_id, refs,
                                                  require_nonempty=True)
    object_ids = [o["object_id"] for o in resolution["objects"]]
    dedup_key = conflict_dedup_key(
        workspace_id, candidate["category"],
        str(candidate.get("payload", {}).get("subject_key", "")
            or candidate_id),
        object_ids or [candidate_id], "", "semantic-promotion")
    with kg.graph_transaction(
            workspace_id, action=RevisionAction.CONFLICT_PROMOTION,
            actor=actor, summary=f"promote conflict candidate "
                                 f"{candidate_id}") as tx:
        conflict_id = store.insert_conflict_tx(tx, {
            "category": candidate["category"],
            "subject_key": str(candidate.get("payload", {}).get(
                "subject_key", "") or ""),
            "title": (candidate.get("explanation") or
                      f"promoted conflict candidate "
                      f"{candidate_id}")[:200],
            "description": candidate.get("explanation", ""),
            "severity": "medium",
            "status": ConflictStatus.OPEN,
            "origin": ConflictOrigin.SEMANTIC_PROMOTION,
            "promoted_from_conflict_candidate_id": candidate_id,
            "dedup_key": dedup_key,
            "metadata": {"initial_status": ConflictStatus.OPEN,
                         "candidate_confidence":
                             candidate.get("confidence", "")},
            "objects": resolution["objects"],
            "evidence": [{"evidence_id": r["evidence_id"],
                          "role": "supports", "quote": r["quote"],
                          "quote_hash": r["quote_hash"]}
                         for r in evidence_rows],
        })
        tx.insert_promotion(
            candidate_kind=PromotionCandidateKind.CONFLICT_CANDIDATE,
            candidate_id=candidate_id, target_kind="conflict",
            target_id=conflict_id, status=PromotionStatus.PROMOTED,
            policy_version=CONFLICT_FRAMEWORK_VERSION, actor=actor,
            note=note)
        tx.insert_decision(
            decision_type=DecisionType.CONFLICT_PROMOTE,
            target_kind=DecisionTargetKind.CONFLICT_CANDIDATE,
            target_id=candidate_id, actor=actor, note=note,
            after={"conflict_id": conflict_id,
                   "category": candidate["category"]},
            source_command=source_command)
        revision = tx.revision_number
    return {"workspace_id": workspace_id,
            "status": PromotionStatus.PROMOTED,
            "conflict": store.get_conflict(workspace_id, conflict_id),
            "knowledge_revision": revision}


__all__ = [
    "plan_scan", "scan_conflicts",
    "start_review", "accept_risk", "resolve", "dismiss", "reopen",
    "plan_promotion", "promote_candidate",
]
