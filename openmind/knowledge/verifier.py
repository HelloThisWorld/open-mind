"""Evidence validation for canonical graph writes.

Same discipline as the Phase 4 candidate verifier, applied at the canonical
boundary: every Evidence id cited by a Claim, Relation, alias or manual
Entity must exist IN THIS WORKSPACE, and every quote must be a
whitespace-normalized substring of the immutable Evidence content. A
fabricated citation is rejected as a typed error before anything is written.

Resolution reuses :func:`openmind.semantic.context.resolve_evidence_text` —
the deterministic, snapshot-only reader (never the live file). No provider,
no network.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .. import db
from .errors import GraphEvidenceInvalid
from .identity import quote_hash
from .vocabularies import ClaimEvidenceRole, require

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text or "").strip())


def resolve_evidence_text(workspace_id: str, evidence_id: str) -> Optional[str]:
    """The immutable content one Evidence row cites, or None (wrong
    workspace, missing, or corrupt snapshot)."""
    from ..semantic.context import resolve_evidence_text as resolve
    return resolve(workspace_id, evidence_id)


def verify_evidence_refs(workspace_id: str,
                         evidence: Sequence[Dict[str, Any]],
                         *, require_nonempty: bool = True
                         ) -> List[Dict[str, Any]]:
    """Validate caller-supplied evidence references for a canonical write.

    Each entry: ``{evidence_id, quote?, role?}``. Returns the normalized join
    rows (quote hashes computed here) or raises :class:`GraphEvidenceInvalid`
    naming every failure. A quote is OPTIONAL — an Evidence citation without
    a quote still anchors the object to the immutable snapshot — but a quote
    that IS supplied must verify against the snapshot.
    """
    problems: List[str] = []
    rows: List[Dict[str, Any]] = []
    seen = set()
    for ref in evidence or ():
        evidence_id = str(ref.get("evidence_id") or "").strip()
        if not evidence_id:
            problems.append("evidence reference without an evidence_id")
            continue
        role = str(ref.get("role") or ClaimEvidenceRole.PRIMARY)
        require(ClaimEvidenceRole, role, field="evidence role")
        record = db.get_evidence(workspace_id, evidence_id)
        if not record:
            problems.append(
                f"evidence {evidence_id} does not exist in this workspace")
            continue
        quote = str(ref.get("quote") or "")
        if quote:
            content = resolve_evidence_text(workspace_id, evidence_id)
            if content is None:
                problems.append(
                    f"evidence {evidence_id}: immutable content is not "
                    f"recoverable, quote cannot be verified")
                continue
            if _normalize(quote) not in _normalize(content):
                problems.append(
                    f"evidence {evidence_id}: quote is not a substring of "
                    f"the immutable evidence content")
                continue
        key = (evidence_id, quote_hash(quote))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"evidence_id": evidence_id, "role": role,
                     "quote": quote, "quote_hash": quote_hash(quote)})
    if problems:
        raise GraphEvidenceInvalid(
            "evidence validation failed: " + "; ".join(problems),
            details={"problems": problems})
    if require_nonempty and not rows:
        raise GraphEvidenceInvalid(
            "at least one valid evidence reference is required",
            details={"problems": ["no evidence supplied"]})
    return rows


def candidate_evidence_rows(workspace_id: str,
                            candidate: Dict[str, Any]
                            ) -> List[Dict[str, Any]]:
    """Re-verify a Phase 4 candidate's stored evidence joins for promotion.

    The candidate's quotes were verified at extraction time; promotion
    re-verifies them against the CURRENT immutable store (ownership + quote
    substring), because canonical writes must never trust a stale check.
    Raises :class:`GraphEvidenceInvalid` when any join fails.
    """
    refs = [{"evidence_id": ev.get("evidence_id"),
             "quote": ev.get("quote", ""),
             "role": ClaimEvidenceRole.PRIMARY}
            for ev in candidate.get("evidence") or []]
    return verify_evidence_refs(workspace_id, refs, require_nonempty=True)


__all__ = ["verify_evidence_refs", "candidate_evidence_rows",
           "resolve_evidence_text"]
