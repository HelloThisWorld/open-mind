"""Change Impact Packet Draft exporter (spec §30).

Writes a deterministic, hash-manifested directory describing one overlay's
impact. Schema ``1.0.0-draft.1``. This is NOT a Feature Evidence Packet (Phase
9) and it is separate from the canonical Knowledge Bundle (which stays
``2.0.0-draft.2``).

Guarantees: deterministic ordering, SHA-256 file hashes in the manifest, no
absolute paths, no provider secrets/prompts, no semantic cache, no Git
credentials, no author emails, no unavailable LFS contents, and explicit
partial/truncation state.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from . import CHANGE_IMPACT_SCHEMA_VERSION
from . import store as ovl_store
from ..git import store as git_store
from ..version import RUNTIME_VERSION


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, sort_keys=True, ensure_ascii=False, indent=2)
        fh.write("\n")


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            fh.write("\n")


def export_packet(workspace_id: str, overlay_id: str, output_dir: str
                  ) -> Dict[str, Any]:
    overlay = ovl_store.get_overlay(workspace_id, overlay_id)
    if not overlay:
        raise ValueError(f"overlay not found: {overlay_id}")
    rep = ovl_store.latest_report(overlay_id)
    report = (rep or {}).get("report", {})
    os.makedirs(output_dir, exist_ok=True)

    repos = ovl_store.list_overlay_repositories(overlay_id)
    repo_rows = []
    for r in repos:
        rec = git_store.get_repository(workspace_id, r["repository_id"])
        repo_rows.append({"repositoryKey": (rec or {}).get("repository_key", ""),
                          "baseCommit": r["base_commit"],
                          "headCommit": r["head_commit"],
                          "mergeBase": r["merge_base_commit"],
                          "branch": r["branch_name"]})
    files = ovl_store.list_files(overlay_id)
    file_rows = [{"changeType": f["change_type"], "oldPath": f["old_path"],
                  "newPath": f["new_path"], "similarity": f["similarity"],
                  "isBinary": bool(f["is_binary"]), "status": f["status"]}
                 for f in files]
    segment_rows = [{"side": s["side"], "changeClass": s["change_class"],
                     "symbol": s["symbol"], "segmentType": s["segment_type"],
                     "startLine": s["start_line"], "endLine": s["end_line"]}
                    for s in ovl_store.list_segments(overlay_id)]
    entity_rows = [{"deltaType": d["delta_type"], "canonicalKey": d["canonical_key"],
                    "entityType": d["entity_type"], "baseEntityId": d["base_entity_id"],
                    "reason": d["reason"]}
                   for d in ovl_store.list_entity_deltas(overlay_id)]
    relation_rows = [{"deltaType": d["delta_type"], "relationType": d["relation_type"]}
                     for d in ovl_store.list_relation_deltas(overlay_id)]
    trace_rows = [{"rootRequirementId": t["root_requirement_id"],
                   "impactType": t["impact_type"], "severity": t["severity"],
                   "reason": t["reason"]}
                  for t in ovl_store.list_trace_impacts(overlay_id)]
    conflict_rows = [{"subjectKey": c["subject_key"], "impactType": c["impact_type"],
                      "category": c["category"], "severity": c["severity"],
                      "reason": c["reason"]}
                     for c in ovl_store.list_conflict_impacts(overlay_id)]
    evidence_rows = [{"id": e["id"], "side": e["side"], "locator": e["locator"],
                      "contentHash": e["content_hash"]}
                     for e in ovl_store.list_evidence(overlay_id)]
    commit_rows = [{"repository": r["repository_id"], "baseCommit": r["base_commit"],
                    "headCommit": r["head_commit"]} for r in repos]

    summary = {
        "overlayId": overlay_id, "overlayRevision": overlay["overlay_revision"],
        "overlayKind": overlay["overlay_kind"], "state": overlay["state"],
        "counts": {
            "files": len(file_rows), "segments": len(segment_rows),
            "entityDeltas": len(entity_rows), "traceImpacts": len(trace_rows),
            "conflictImpacts": len(conflict_rows), "evidence": len(evidence_rows),
        },
        "overallRisk": (report.get("riskSummary", {}) or {}).get("overallRisk", "unknown"),
    }

    # Deterministic file set (sorted).
    jsonl_files = {
        "repositories.jsonl": repo_rows,
        "commits.jsonl": commit_rows,
        "file-changes.jsonl": file_rows,
        "segment-changes.jsonl": segment_rows,
        "entity-deltas.jsonl": entity_rows,
        "relation-deltas.jsonl": relation_rows,
        "requirement-impact.jsonl": [t for t in trace_rows
                                     if t["impactType"] != "test-impacted"],
        "test-impact.jsonl": [t for t in trace_rows
                              if t["impactType"] == "test-impacted"],
        "trace-impact.jsonl": trace_rows,
        "gap-impact.jsonl": _gap_rows(trace_rows),
        "conflict-impact.jsonl": conflict_rows,
        "evidence.jsonl": evidence_rows,
    }
    for name, rows in jsonl_files.items():
        _write_jsonl(os.path.join(output_dir, name), rows)
    _write_json(os.path.join(output_dir, "summary.json"), summary)
    with open(os.path.join(output_dir, "report.md"), "w", encoding="utf-8",
              newline="\n") as fh:
        from .report import render_markdown
        fh.write(render_markdown(report) if report else "# (no report)\n")

    partial = bool((overlay.get("summary") or {}).get("partial"))
    warnings = (overlay.get("summary") or {}).get("warnings", [])

    # Hash every emitted file (except the manifest itself), deterministically.
    file_hashes: Dict[str, str] = {}
    for name in sorted(list(jsonl_files) + ["summary.json", "report.md"]):
        path = os.path.join(output_dir, name)
        if os.path.isfile(path):
            file_hashes[name] = _sha256_file(path)

    manifest = {
        "schemaVersion": CHANGE_IMPACT_SCHEMA_VERSION,
        "runtimeVersion": RUNTIME_VERSION,
        "workspaceId": workspace_id,
        "overlayId": overlay_id,
        "overlayRevision": overlay["overlay_revision"],
        "baseKnowledgeRevision": overlay["base_knowledge_revision"],
        "basePolicyChecksum": overlay["base_policy_checksum"],
        "baseCommits": sorted({r["base_commit"] for r in repos if r["base_commit"]}),
        "headCommits": sorted({r["head_commit"] for r in repos if r["head_commit"]}),
        "fileHashes": file_hashes,
        "recordCounts": summary["counts"],
        "partial": partial,
        "warnings": warnings,
    }
    _write_json(os.path.join(output_dir, "manifest.json"), manifest)
    return {"overlay_id": overlay_id, "output": output_dir,
            "manifest": manifest, "partial": partial}


def _gap_rows(trace_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Gap rows are derived in the report; the packet mirrors the introduced set
    # by scanning trace impacts of broken type.
    out = []
    for t in trace_rows:
        if t["impactType"] == "trace-broken":
            out.append({"gapType": "missing-implementation",
                        "rootRequirementId": t["rootRequirementId"],
                        "state": "introduced"})
    return out


__all__ = ["export_packet"]
