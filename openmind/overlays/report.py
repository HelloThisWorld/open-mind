"""Change Impact Report generation (spec §29).

Renders a deterministic JSON report (schema ``1.0.0-draft.1``) and a
byte-identical Markdown view from the overlay's persisted impact records — never
from a model, and never with an embedded generation timestamp in the Markdown
body. The same overlay state always produces the same report hash.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from . import CHANGE_IMPACT_SCHEMA_VERSION
from . import store as ovl_store
from ..git import store as git_store
from ..git.vocabularies import RiskLevel


def build_report(workspace_id: str, overlay: Dict[str, Any], *,
                 risk: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the structured report document (deterministic ordering)."""
    overlay_id = overlay["id"]
    repos = ovl_store.list_overlay_repositories(overlay_id)
    files = ovl_store.list_files(overlay_id)
    entity_deltas = ovl_store.list_entity_deltas(overlay_id)
    relation_deltas = ovl_store.list_relation_deltas(overlay_id)
    trace_impacts = ovl_store.list_trace_impacts(overlay_id)
    conflict_impacts = ovl_store.list_conflict_impacts(overlay_id)
    evidence = ovl_store.list_evidence(overlay_id)

    repo_views = []
    for r in repos:
        rec = git_store.get_repository(workspace_id, r["repository_id"])
        repo_views.append({
            "repository": (rec or {}).get("repository_key", r["repository_id"]),
            "baseCommit": r["base_commit"], "headCommit": r["head_commit"],
            "mergeBase": r["merge_base_commit"], "branch": r["branch_name"],
            "targetBranch": r["target_branch"],
            "worktreeHash": r["worktree_hash"]})

    file_summary = _file_summary(files)
    requirement_impacts = [t for t in trace_impacts
                           if t["impact_type"] not in ("test-impacted",)]
    test_impacts = [t for t in trace_impacts if t["impact_type"] == "test-impacted"]

    report = {
        "schemaVersion": CHANGE_IMPACT_SCHEMA_VERSION,
        "identity": {
            "overlayId": overlay_id,
            "overlayRevision": overlay["overlay_revision"],
            "overlayKind": overlay["overlay_kind"],
            "name": overlay.get("name", ""),
            "state": overlay["state"],
        },
        "baseline": {
            "baseKnowledgeRevision": overlay["base_knowledge_revision"],
            "baseTraceabilityRun": overlay["base_traceability_run_id"],
            "basePolicyChecksum": overlay["base_policy_checksum"],
        },
        "head": {"sourceHash": overlay["source_hash"]},
        "repositories": repo_views,
        "commits": _commits(repos),
        "fileSummary": file_summary,
        "changedFiles": [_file_view(f) for f in files],
        "changedSegments": _segment_summary(overlay_id),
        "directGraphDeltas": {
            "entities": [_delta_view(d) for d in entity_deltas],
            "relations": [_rel_delta_view(d) for d in relation_deltas],
        },
        "impactedRequirements": [_req_view(t) for t in requirement_impacts],
        "impactedTests": [_test_view(t) for t in test_impacts
                          if not t.get("after", {}).get("recommended")],
        "recommendedTests": [_test_view(t) for t in test_impacts
                             if t.get("after", {}).get("recommended")],
        "traceImpact": [_trace_view(t) for t in trace_impacts],
        "gapImpact": _gap_impact(trace_impacts),
        "conflictImpact": [_conflict_view(c) for c in conflict_impacts],
        "riskSummary": risk,
        "unknowns": risk.get("unknowns", []),
        "limits": {
            "partial": overlay.get("summary", {}).get("partial", False),
            "warnings": overlay.get("summary", {}).get("warnings", []),
        },
        "evidenceIndex": [_evidence_view(e) for e in evidence],
    }
    return report


def report_hash(report: Dict[str, Any]) -> str:
    """Deterministic SHA-256 of the canonical JSON serialization."""
    return hashlib.sha256(
        _canonical_json(report).encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


# ---------------------------------------------------------------------------
# Markdown (deterministic, no timestamp in the body)
# ---------------------------------------------------------------------------
def render_markdown(report: Dict[str, Any]) -> str:
    ident = report["identity"]
    lines: List[str] = []
    lines.append(f"# Change Impact Report — {ident['overlayKind']} "
                 f"`{ident['overlayId']}` (revision {ident['overlayRevision']})")
    lines.append("")
    lines.append("> Provisional overlay analysis. The canonical Base Workspace "
                 "is unchanged. Projected gaps are NOT canonical gaps and "
                 "projected conflicts are NOT canonical conflicts.")
    lines.append("")
    b = report["baseline"]
    lines.append("## Baseline")
    lines.append(f"- Base Knowledge Revision: {b['baseKnowledgeRevision']}")
    lines.append(f"- Base Trace Policy checksum: `{b['basePolicyChecksum'] or '—'}`")
    lines.append("")
    lines.append("## Repositories")
    for r in report["repositories"]:
        lines.append(f"- `{r['repository']}` base `{_short(r['baseCommit'])}` "
                     f"head `{_short(r['headCommit'])}` "
                     f"(merge-base `{_short(r['mergeBase'])}`)")
    lines.append("")
    fs = report["fileSummary"]
    lines.append("## File summary")
    lines.append(f"- Files changed: {fs['total']} "
                 f"(+{fs['additions']} / -{fs['deletions']})")
    for k in sorted(fs["byChangeType"]):
        lines.append(f"  - {k}: {fs['byChangeType'][k]}")
    lines.append("")
    lines.append("## Risk")
    lines.append(f"- Overall risk: **{report['riskSummary']['overallRisk']}**")
    for reason in report["riskSummary"]["reasons"]:
        lines.append(f"  - [{reason['level']}] {reason['reason']}")
    if report["unknowns"]:
        lines.append("- Unknowns:")
        for u in report["unknowns"]:
            lines.append(f"  - {u.get('kind','unknown')}: "
                         f"{u.get('path') or u.get('subjectKey') or ''}")
    lines.append("")
    lines.append("## Impacted requirements")
    if not report["impactedRequirements"]:
        lines.append("- (none)")
    for r in report["impactedRequirements"]:
        lines.append(f"- `{r['rootRequirementId']}` — {r['impactType']} "
                     f"({r['severity']}): {r['reason']}")
    lines.append("")
    lines.append("## Impacted tests")
    if not report["impactedTests"]:
        lines.append("- (none)")
    for t in report["impactedTests"]:
        lines.append(f"- `{t['entityId']}` — {t['reason']}")
    lines.append("")
    lines.append("## Conflict impact")
    if not report["conflictImpact"]:
        lines.append("- (none)")
    for c in report["conflictImpact"]:
        lines.append(f"- {c['impactType']} [{c['category']}] "
                     f"`{c['subjectKey']}`: {c['reason']}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def _short(sha: str) -> str:
    return (sha or "")[:12] or "—"


def _file_summary(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    additions = deletions = 0
    for f in files:
        by_type[f["change_type"]] = by_type.get(f["change_type"], 0) + 1
        additions += f["additions"]
        deletions += f["deletions"]
    return {"total": len(files), "additions": additions,
            "deletions": deletions, "byChangeType": by_type}


def _file_view(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f["id"], "changeType": f["change_type"],
        "oldPath": f["old_path"], "newPath": f["new_path"],
        "similarity": f["similarity"], "additions": f["additions"],
        "deletions": f["deletions"], "isBinary": bool(f["is_binary"]),
        "isSymlink": bool(f["is_symlink"]), "isSubmodule": bool(f["is_submodule"]),
        "isLfsPointer": bool(f["is_lfs_pointer"]), "status": f["status"],
        "layer": f["layer"],
    }


def _segment_summary(overlay_id: str) -> Dict[str, int]:
    segs = ovl_store.list_segments(overlay_id)
    by_class: Dict[str, int] = {}
    for s in segs:
        by_class[s["change_class"]] = by_class.get(s["change_class"], 0) + 1
    return by_class


def _delta_view(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"deltaType": d["delta_type"], "baseEntityId": d["base_entity_id"],
            "canonicalKey": d["canonical_key"], "entityType": d["entity_type"],
            "reason": d["reason"], "confidence": d["confidence"]}


def _rel_delta_view(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"deltaType": d["delta_type"], "baseRelationId": d["base_relation_id"],
            "relationType": d["relation_type"], "reason": d["reason"]}


def _req_view(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"rootRequirementId": t["root_requirement_id"],
            "impactType": t["impact_type"], "severity": t["severity"],
            "reason": t["reason"], "evidenceIds": t.get("evidence_ids", []),
            "sourceSide": "after",
            "changedObjects": t.get("after", {}).get("changedObjects", [])}


def _test_view(t: Dict[str, Any]) -> Dict[str, Any]:
    after = t.get("after", {})
    return {"entityId": t["root_requirement_id"],
            "canonicalKey": after.get("canonicalKey", ""),
            "reason": t["reason"], "confidence": "high",
            "supportingTrace": after.get("supportingTrace", "")}


def _trace_view(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"rootRequirementId": t["root_requirement_id"],
            "impactType": t["impact_type"], "severity": t["severity"],
            "introducedGaps": t.get("introduced_gaps", []),
            "resolvedGaps": t.get("resolved_gaps", []),
            "reason": t["reason"]}


def _gap_impact(trace_impacts: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    introduced, resolved = [], []
    for t in trace_impacts:
        introduced.extend(t.get("introduced_gaps", []))
        resolved.extend(t.get("resolved_gaps", []))
    return {"introduced": introduced, "resolved": resolved}


def _conflict_view(c: Dict[str, Any]) -> Dict[str, Any]:
    return {"impactType": c["impact_type"], "category": c["category"],
            "severity": c["severity"], "subjectKey": c["subject_key"],
            "baseConflictId": c["base_conflict_id"], "reason": c["reason"],
            "before": c.get("before", {}), "after": c.get("after", {})}


def _evidence_view(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": e["id"], "side": e["side"], "locator": e.get("locator", {}),
            "contentHash": e["content_hash"]}


def _commits(repos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"repository": r["repository_id"], "baseCommit": r["base_commit"],
             "headCommit": r["head_commit"]} for r in repos]


__all__ = ["build_report", "report_hash", "render_markdown", "_canonical_json"]
