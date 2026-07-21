"""Deterministic whole-corpus lens validation. No model, ever.

A lens — induced or organization-supplied — is judged against the FULL local
workspace with plain matching:

* **asset coverage**  — fraction of active assets any role matches;
* **role coverage**   — roles with at least one match / total roles;
* **role overlap**    — fraction of matched assets claimed by >1 role;
* **identifier hits** — how often each identifier pattern matches across
  stored evidence excerpts, segment symbols and logical keys, with a
  false-collision indicator for patterns that match implausibly often;
* **document-pattern hits** — heading patterns against document headings;
* **unsupported tasks / invalid patterns** — schema-level defects;
* **sample-evidence validity** — an induced lens's cited samples must exist
  in this workspace.

Verdict: ``invalid`` (schema errors, unsupported tasks, no matching role, or
below the lens's own ``minimumAssetCoverage``), else ``valid-with-warnings``
(overlap above ``maximumRoleOverlap``, unmatched roles, zero identifier
hits), else ``valid``. Approval requires not-invalid; activation of an
induced lens additionally requires explicit human approval.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List

from ... import db
from .models import validate_lens_definition

#: A pattern matching more than this many places is flagged as a probable
#: false-collision (it selects "everything", not an identifier scheme).
FALSE_COLLISION_HITS = 500
#: Bounded scan corpus: evidence excerpts per workspace.
MAX_EXCERPTS_SCANNED = 5_000


def _role_matches(role: Dict[str, Any], logical_key: str) -> bool:
    key = logical_key.replace("\\", "/")
    base = key.rsplit("/", 1)[-1]
    for glob in role.get("pathGlobs") or []:
        if fnmatch.fnmatch(key.lower(), glob.lower()):
            return True
    for pattern in role.get("namePatterns") or []:
        try:
            if re.search(pattern, base):
                return True
        except re.error:
            continue
    return False


def _excerpt_corpus(workspace_id: str) -> List[str]:
    """Stored evidence excerpts of CURRENT revisions — bounded, no blob
    reads. Excerpts are exactly what Phase 2/3 recorded for citation display,
    which makes them a fair, cheap sample of real content."""
    conn, lock = db.shared_connection()
    with lock:
        rows = conn.execute(
            "SELECT e.excerpt FROM evidence e "
            "JOIN assets a ON a.current_revision_id = e.revision_id "
            "WHERE a.workspace_id=? AND e.excerpt != '' LIMIT ?",
            (workspace_id, MAX_EXCERPTS_SCANNED)).fetchall()
    return [r["excerpt"] for r in rows]


def validate_lens(workspace_id: str,
                  definition: Dict[str, Any], *,
                  source: str) -> Dict[str, Any]:
    """The deterministic validation report for one lens definition."""
    normalized, schema_errors, schema_warnings = validate_lens_definition(
        definition, source=source)
    assets = db.list_assets(workspace_id, state="active", limit=100_000)
    roles = normalized.get("roles") or []

    matched_by: Dict[str, List[str]] = {}
    for asset in assets:
        hits = [role["name"] for role in roles
                if _role_matches(role, asset.get("logical_key") or "")]
        if hits:
            matched_by[asset["id"]] = hits
    role_hit_counts: Dict[str, int] = {role["name"]: 0 for role in roles}
    for hits in matched_by.values():
        for name in hits:
            role_hit_counts[name] += 1

    asset_coverage = (len(matched_by) / len(assets)) if assets else 0.0
    roles_with_hits = sum(1 for n in role_hit_counts.values() if n > 0)
    role_coverage = (roles_with_hits / len(roles)) if roles else 0.0
    overlapped = sum(1 for hits in matched_by.values() if len(hits) > 1)
    role_overlap = (overlapped / len(matched_by)) if matched_by else 0.0
    unmatched_roles = [name for name, n in role_hit_counts.items() if n == 0]

    # -- identifier hits over the bounded excerpt corpus + symbols + keys --
    corpus = _excerpt_corpus(workspace_id)
    symbols = list(db.list_workspace_symbols(workspace_id, limit=5_000))
    keys = [a.get("logical_key") or "" for a in assets]
    identifier_hits: Dict[str, int] = {}
    false_collisions: List[str] = []
    for ident in normalized.get("identifiers") or []:
        pattern = ident.get("pattern") or ""
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        hits = 0
        for text in corpus:
            hits += len(compiled.findall(text))
            if hits > FALSE_COLLISION_HITS:
                break
        if hits <= FALSE_COLLISION_HITS:
            hits += sum(1 for s in symbols if compiled.search(s))
            hits += sum(1 for k in keys if compiled.search(k))
        identifier_hits[ident["name"]] = hits
        if hits > FALSE_COLLISION_HITS:
            false_collisions.append(ident["name"])

    # -- document-pattern hits against document headings -------------------
    heading_texts: List[str] = []
    for doc in db.list_document_assets(workspace_id, limit=200):
        revision_id = doc.get("current_revision_id")
        if not revision_id:
            continue
        for segment in db.list_segments(workspace_id, revision_id,
                                        limit=300):
            if segment.get("segment_type") == "heading" and \
                    segment.get("symbol"):
                heading_texts.append(segment["symbol"])
    docpat_hits = 0
    for pat in normalized.get("documentPatterns") or []:
        for pattern in pat.get("headingPatterns") or []:
            try:
                compiled = re.compile(pattern)
            except re.error:
                continue
            docpat_hits += sum(1 for h in heading_texts
                               if compiled.search(h))

    from ..tasks import REGISTRY as TASKS
    unsupported_tasks = [entry.get("task") for entry
                         in normalized.get("semanticTasks") or []
                         if entry.get("task") not in TASKS]

    sample_ids = normalized.get("sampleEvidenceIds") or []
    invalid_samples = [evidence_id for evidence_id in sample_ids
                       if not db.get_evidence(workspace_id, evidence_id)]

    thresholds = normalized.get("validation") or {}
    minimum_coverage = float(thresholds.get("minimumAssetCoverage") or 0.0)
    maximum_overlap = float(thresholds.get("maximumRoleOverlap") or 1.0)

    errors = list(schema_errors)
    warnings = list(schema_warnings)
    if unsupported_tasks:
        errors.append(f"unsupported semantic tasks: {unsupported_tasks}")
    if invalid_samples:
        errors.append(f"{len(invalid_samples)} cited sample evidence id(s) "
                      f"do not exist in this workspace")
    if roles and roles_with_hits == 0:
        errors.append("no role matches any asset in this workspace")
    if assets and asset_coverage < minimum_coverage:
        errors.append(f"asset coverage {asset_coverage:.2f} is below the "
                      f"lens's own minimum {minimum_coverage:.2f}")
    if role_overlap > maximum_overlap:
        warnings.append(f"role overlap {role_overlap:.2f} exceeds the "
                        f"lens's maximum {maximum_overlap:.2f}")
    if unmatched_roles:
        warnings.append(f"roles with no match: {sorted(unmatched_roles)}")
    if false_collisions:
        warnings.append(f"identifier patterns matching implausibly often "
                        f"(probable false collisions): {false_collisions}")
    if normalized.get("identifiers") and identifier_hits and \
            all(n == 0 for n in identifier_hits.values()):
        warnings.append("no identifier pattern matches anything in this "
                        "workspace")

    result = ("invalid" if errors
              else "valid-with-warnings" if warnings else "valid")
    return {
        "result": result,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "asset_count": len(assets),
            "asset_coverage": round(asset_coverage, 4),
            "role_coverage": round(role_coverage, 4),
            "role_overlap": round(role_overlap, 4),
            "unmatched_role_count": len(unmatched_roles),
            "identifier_hits": identifier_hits,
            "identifier_false_collisions": false_collisions,
            "document_pattern_hits": docpat_hits,
            "unsupported_task_count": len(unsupported_tasks),
            "invalid_pattern_count": sum(
                1 for e in schema_errors if "pattern" in e),
            "sample_evidence_checked": len(sample_ids),
            "sample_evidence_invalid": len(invalid_samples),
        },
        "normalized": normalized,
    }


__all__ = ["validate_lens", "FALSE_COLLISION_HITS"]
