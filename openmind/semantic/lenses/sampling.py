"""Deterministic representative sampling for lens induction.

The whole workspace NEVER goes to a model. This module picks a bounded,
reproducible sample: cluster the active assets by (asset type × top-level
path × extension), then take up to a few representatives per cluster, with
extra weight for entry points, high-structure modules, parsed documents,
interface specifications, test material and outliers (single-member
clusters). Same workspace state + same options → byte-identical plan; the
plan reports exactly what was omitted and why.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ... import db
from .. import context as context_mod

# Hard limits (spec §27). All visible, all reported.
MAX_SAMPLED_ASSETS = 24
MAX_SAMPLED_SEGMENTS = 48
MAX_SAMPLE_CHARS = 60_000
MAX_SAMPLES_PER_CLUSTER = 3
MAX_ESTIMATED_TOKENS = 18_000

_ENTRY_HINTS = ("main", "app", "application", "index", "server", "cli")
_INTERFACE_PARSERS = ("openapi", "json-schema")


def _cluster_key(asset: Dict[str, Any]) -> str:
    key = (asset.get("logical_key") or "").replace("\\", "/")
    top = key.split("/", 1)[0] if "/" in key else ""
    ext = ("." + key.rsplit(".", 1)[-1].lower()) if "." in key else ""
    return f"{asset.get('asset_type')}|{top}|{ext}"


def _category(asset: Dict[str, Any], parse: Any,
              segment_count: int, cluster_size: int) -> str:
    key = (asset.get("logical_key") or "").lower()
    base = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if parse is not None:
        if parse.get("parser_name") in _INTERFACE_PARSERS:
            return "interface-specification"
        if "test" in key:
            return "test-document"
        return "document"
    if asset.get("asset_type") == "test-source":
        return "test-source"
    if any(base == hint or base.startswith(hint + "-") or
           base.endswith("-" + hint) for hint in _ENTRY_HINTS):
        return "entry-point"
    if cluster_size == 1:
        return "outlier"
    if segment_count >= 12:
        return "high-degree-module"
    return "representative"


#: Categories picked first when the asset budget runs short.
_PRIORITY = {"entry-point": 0, "interface-specification": 1, "document": 2,
             "high-degree-module": 3, "test-document": 4, "test-source": 5,
             "outlier": 6, "representative": 7}


def build_sample_plan(workspace_id: str) -> Dict[str, Any]:
    """The deterministic sample plan. Reads the database and (bounded)
    immutable content; performs no provider call."""
    assets = db.list_assets(workspace_id, state="active", limit=100_000)
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for asset in assets:
        if not asset.get("current_revision_id"):
            continue
        clusters.setdefault(_cluster_key(asset), []).append(asset)

    scored: List[Dict[str, Any]] = []
    for cluster_key in sorted(clusters):
        members = sorted(clusters[cluster_key],
                         key=lambda a: a["logical_key"])
        taken = 0
        for asset in members:
            if taken >= MAX_SAMPLES_PER_CLUSTER:
                break
            revision_id = asset["current_revision_id"]
            parse = db.get_document_parse(workspace_id, revision_id)
            segment_count = db.count_segments(workspace_id, revision_id)
            if segment_count == 0:
                continue
            scored.append({
                "asset": asset, "revision_id": revision_id,
                "cluster": cluster_key,
                "cluster_size": len(members),
                "category": _category(asset, parse, segment_count,
                                      len(members)),
            })
            taken += 1

    scored.sort(key=lambda s: (_PRIORITY.get(s["category"], 9),
                               s["asset"]["logical_key"]))
    omitted_assets = max(0, len(scored) - MAX_SAMPLED_ASSETS)
    picked = scored[:MAX_SAMPLED_ASSETS]

    samples: List[Dict[str, Any]] = []
    total_chars = 0
    total_segments = 0
    omitted_by_chars = 0
    for entry in picked:
        revision_id = entry["revision_id"]
        segments = db.list_segments(workspace_id, revision_id, limit=200)
        seg_budget = max(1, MAX_SAMPLED_SEGMENTS // max(1, len(picked)))
        sample = {"asset_id": entry["asset"]["id"],
                  "logical_key": entry["asset"]["logical_key"],
                  "asset_type": entry["asset"]["asset_type"],
                  "revision_id": revision_id,
                  "category": entry["category"],
                  "cluster": entry["cluster"],
                  "evidence": []}
        for segment in segments:
            if len(sample["evidence"]) >= seg_budget or \
                    total_segments >= MAX_SAMPLED_SEGMENTS:
                break
            ev = db.get_evidence_for_segment(workspace_id, segment["id"])
            if not ev:
                continue
            text = context_mod.resolve_evidence_text(workspace_id, ev["id"])
            if not text or not text.strip():
                continue
            excerpt = text[:2_000]
            if total_chars + len(excerpt) > MAX_SAMPLE_CHARS:
                omitted_by_chars += 1
                break
            total_chars += len(excerpt)
            total_segments += 1
            sample["evidence"].append({"evidence_id": ev["id"],
                                       "chars": len(excerpt)})
        if sample["evidence"]:
            samples.append(sample)

    # Deterministic inventory the induction prompt gets as structural context.
    by_type: Dict[str, int] = {}
    by_ext: Dict[str, int] = {}
    for asset in assets:
        by_type[asset["asset_type"]] = by_type.get(asset["asset_type"], 0) + 1
        key = asset.get("logical_key") or ""
        ext = ("." + key.rsplit(".", 1)[-1].lower()) if "." in key else ""
        if ext:
            by_ext[ext] = by_ext.get(ext, 0) + 1
    doc_titles = [d.get("document", {}).get("title") or d["logical_key"]
                  for d in db.list_document_assets(workspace_id, limit=20)]

    return {
        "workspace_id": workspace_id,
        "samples": samples,
        "sample_count": len(samples),
        "segment_count": total_segments,
        "total_chars": total_chars,
        "estimated_tokens": context_mod.estimate_tokens("x" * total_chars),
        "token_estimate_basis": "estimated (local chars/4 heuristic)",
        "limits": {"max_assets": MAX_SAMPLED_ASSETS,
                   "max_segments": MAX_SAMPLED_SEGMENTS,
                   "max_chars": MAX_SAMPLE_CHARS,
                   "max_per_cluster": MAX_SAMPLES_PER_CLUSTER},
        "omitted": {"assets_beyond_cap": omitted_assets,
                    "segments_beyond_char_budget": omitted_by_chars,
                    "clusters_total": len(clusters)},
        "inventory": {"assets_by_type": by_type,
                      "files_by_extension": dict(sorted(by_ext.items())),
                      "document_titles": doc_titles},
        "provider_calls_made": 0,
    }


__all__ = ["build_sample_plan", "MAX_SAMPLED_ASSETS", "MAX_SAMPLED_SEGMENTS",
           "MAX_SAMPLE_CHARS", "MAX_SAMPLES_PER_CLUSTER"]
