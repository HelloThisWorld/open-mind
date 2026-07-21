"""Local evidence verification and final-confidence derivation.

Schema-valid model output is still just a CLAIM. This module decides what
OpenMind will actually keep, by checking every candidate against the
immutable Evidence store:

1. each cited Evidence id exists **in this workspace** (workspace-scoped
   lookup — a foreign id resolves to nothing);
2. it was actually **included in the request** (``allowedEvidenceIds``) — a
   model cannot launder support from content it was never shown;
3. its immutable content is re-retrieved (via the injected resolver, which
   reads the content-addressed snapshot — never the live file, never the
   model's memory);
4. every quote, whitespace-normalized, must be a **substring** of that
   content; empty or fabricated quotes are rejected;
5. candidate types must be allowed for the task; a present ``stableKey``
   must be a plausible identifier (bounded, no control characters).

The outcome per candidate is ``verified`` / ``partially-verified`` /
``rejected`` plus a LOCALLY derived confidence. The provider's
``confidenceHint`` is recorded but never copied into the final confidence:

* ``high``   — fully verified evidence AND (an explicit identifier or
  explicitly normative language inside a verified quote);
* ``medium`` — fully verified evidence with a clear statement but no
  identifier/normative anchor;
* ``low``    — partially verified, or interpretation-heavy content.

Relation and conflict candidates go through the same evidence checks; a
relation's confidence is additionally CAPPED at ``medium`` (``low`` when its
pairing signal was semantic retrieval alone) — a model's say-so never makes a
relation high-confidence in Phase 4.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .models import (EvidenceVerificationStatus as EvStatus, FinalConfidence)

#: (workspace_id, evidence_id) -> immutable content text, or None when the
#: evidence does not exist IN THAT WORKSPACE or cannot be recovered.
EvidenceResolver = Callable[[str, str], Optional[str]]

_WS_RE = re.compile(r"\s+")
#: A plausible explicit identifier: letters+digits with dash/dot/slash
#: separators, at least one digit, bounded. REQ-NC-017 yes; "the system" no.
_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_.][A-Z0-9]+)+$")
#: A structurally acceptable stableKey (superset of identifiers: also allows
#: mixed case keys like api paths); control chars and whitespace never.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#:-]{0,119}$")
#: Explicitly normative language, checked inside VERIFIED quotes only.
_NORMATIVE_RE = re.compile(
    r"\b(shall|must|must not|shall not|is required to|are required to"
    r"|required)\b", re.IGNORECASE)


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def quote_hash(quote: str) -> str:
    return hashlib.sha256(normalize_ws(quote).encode("utf-8")).hexdigest()


@dataclass
class VerifiedItem:
    """One candidate's verification outcome."""
    accepted: bool
    evidence_status: str
    confidence: str
    verified_evidence: List[Dict[str, str]] = field(default_factory=list)
    rejection_reason: str = ""
    diagnostics: List[str] = field(default_factory=list)


def _check_quotes(workspace_id: str, evidence_items: Sequence[Dict[str, Any]],
                  allowed_ids: frozenset, resolver: EvidenceResolver
                  ) -> Dict[str, Any]:
    """Shared evidence loop. Returns verified entries + diagnostics + the
    counts the status derivation needs."""
    verified: List[Dict[str, str]] = []
    diagnostics: List[str] = []
    invalid = 0
    for ev in evidence_items or []:
        evidence_id = str(ev.get("evidenceId") or ev.get("evidence_id") or "")
        quote = str(ev.get("quote") or "")
        if not evidence_id:
            invalid += 1
            diagnostics.append("evidence entry without an id")
            continue
        if evidence_id not in allowed_ids:
            invalid += 1
            diagnostics.append(
                f"evidence {evidence_id} was not part of the request")
            continue
        content = resolver(workspace_id, evidence_id)
        if content is None:
            invalid += 1
            diagnostics.append(
                f"evidence {evidence_id} does not exist in this workspace "
                f"or cannot be recovered")
            continue
        norm_quote = normalize_ws(quote)
        if not norm_quote:
            invalid += 1
            diagnostics.append(f"evidence {evidence_id}: empty quote")
            continue
        if norm_quote not in normalize_ws(content):
            invalid += 1
            diagnostics.append(
                f"evidence {evidence_id}: quote is not a substring of the "
                f"immutable content (fabricated or altered)")
            continue
        verified.append({"evidence_id": evidence_id, "quote": quote,
                         "quote_hash": quote_hash(quote)})
    return {"verified": verified, "invalid": invalid,
            "diagnostics": diagnostics}


def _evidence_status(verified_count: int, invalid_count: int) -> str:
    if verified_count == 0:
        return EvStatus.REJECTED
    if invalid_count > 0:
        return EvStatus.PARTIALLY_VERIFIED
    return EvStatus.VERIFIED


def verify_candidate(workspace_id: str, candidate: Dict[str, Any], *,
                     allowed_types: frozenset,
                     allowed_evidence_ids: frozenset,
                     resolver: EvidenceResolver) -> VerifiedItem:
    """Verify one extraction candidate (engineering concept, classification
    or revision status) against the immutable store."""
    ctype = str(candidate.get("candidateType") or "")
    if ctype not in allowed_types:
        return VerifiedItem(False, EvStatus.REJECTED, FinalConfidence.LOW,
                            rejection_reason=f"candidate type {ctype!r} is "
                                             f"not allowed for this task")
    stable_key = str(candidate.get("stableKey") or "")
    if stable_key and not _KEY_RE.match(stable_key):
        return VerifiedItem(False, EvStatus.REJECTED, FinalConfidence.LOW,
                            rejection_reason=f"stableKey {stable_key!r} is "
                                             f"not a valid identifier")
    if not str(candidate.get("statement") or "").strip():
        return VerifiedItem(False, EvStatus.REJECTED, FinalConfidence.LOW,
                            rejection_reason="empty statement")

    outcome = _check_quotes(workspace_id, candidate.get("evidence") or [],
                            allowed_evidence_ids, resolver)
    status = _evidence_status(len(outcome["verified"]), outcome["invalid"])
    if status == EvStatus.REJECTED:
        return VerifiedItem(False, status, FinalConfidence.LOW,
                            rejection_reason="no valid evidence "
                                             "(missing, foreign, un-sent or "
                                             "fabricated citations)",
                            diagnostics=outcome["diagnostics"])
    confidence = derive_confidence(candidate, status, outcome["verified"])
    return VerifiedItem(True, status, confidence,
                        verified_evidence=outcome["verified"],
                        diagnostics=outcome["diagnostics"])


def derive_confidence(candidate: Dict[str, Any], evidence_status: str,
                      verified_evidence: List[Dict[str, str]]) -> str:
    """The deterministic confidence ladder. The model's hint plays no part."""
    if evidence_status != EvStatus.VERIFIED:
        return FinalConfidence.LOW
    stable_key = str(candidate.get("stableKey") or "")
    has_identifier = bool(_IDENTIFIER_RE.match(stable_key))
    has_normative = any(_NORMATIVE_RE.search(ev.get("quote") or "")
                        for ev in verified_evidence)
    if has_identifier or has_normative:
        return FinalConfidence.HIGH
    return FinalConfidence.MEDIUM


def verify_relation(workspace_id: str, relation: Dict[str, Any], *,
                    allowed_evidence_ids: frozenset,
                    resolver: EvidenceResolver,
                    pair_signal: str = "") -> VerifiedItem:
    """Verify one relation candidate. Confidence is capped: ``medium`` at
    best, ``low`` when the pair came from semantic retrieval alone."""
    outcome = _check_quotes(workspace_id, relation.get("evidence") or [],
                            allowed_evidence_ids, resolver)
    status = _evidence_status(len(outcome["verified"]), outcome["invalid"])
    if status == EvStatus.REJECTED:
        return VerifiedItem(False, status, FinalConfidence.LOW,
                            rejection_reason="no valid evidence",
                            diagnostics=outcome["diagnostics"])
    if status == EvStatus.VERIFIED and pair_signal and \
            pair_signal != "retrieval":
        confidence = FinalConfidence.MEDIUM
    else:
        confidence = FinalConfidence.LOW
    return VerifiedItem(True, status, confidence,
                        verified_evidence=outcome["verified"],
                        diagnostics=outcome["diagnostics"])


def verify_conflict(workspace_id: str, conflict: Dict[str, Any], *,
                    allowed_evidence_ids: frozenset,
                    resolver: EvidenceResolver) -> VerifiedItem:
    """Verify one conflict candidate. Quotes from BOTH sides earn medium;
    anything less stays low. Never high — a conflict is never 'confirmed'."""
    outcome = _check_quotes(workspace_id, conflict.get("evidence") or [],
                            allowed_evidence_ids, resolver)
    status = _evidence_status(len(outcome["verified"]), outcome["invalid"])
    if status == EvStatus.REJECTED:
        return VerifiedItem(False, status, FinalConfidence.LOW,
                            rejection_reason="no valid evidence",
                            diagnostics=outcome["diagnostics"])
    distinct_sources = {ev["evidence_id"] for ev in outcome["verified"]}
    confidence = (FinalConfidence.MEDIUM
                  if status == EvStatus.VERIFIED and len(distinct_sources) >= 2
                  else FinalConfidence.LOW)
    return VerifiedItem(True, status, confidence,
                        verified_evidence=outcome["verified"],
                        diagnostics=outcome["diagnostics"])


def store_resolver() -> EvidenceResolver:
    """The production resolver: workspace-scoped evidence lookup + immutable
    content recovery, sharing the AssetService recovery paths (block blob for
    document segments, revision-blob line slice for code)."""
    from . import context as context_mod
    return context_mod.resolve_evidence_text


__all__ = ["EvidenceResolver", "VerifiedItem", "verify_candidate",
           "verify_relation", "verify_conflict", "derive_confidence",
           "normalize_ws", "quote_hash", "store_resolver"]
