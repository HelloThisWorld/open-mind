"""Knowledge Bundle 2.0 Draft export.

A SEPARATE contract from the frozen ``.openmind`` 1.x artifact: its own
command (``openmind bundle export``), its own directory (``.openmind-v2``),
its own schema version (``2.0.0-draft.1``). Draft means draft — the layout
may change until the freeze, and there is no import.

GUARANTEES (spec §31, verified by openmind.bundle_verify and the tests)
-----------------------------------------------------------------------
deterministic ordering (stable sort key per file); UTF-8 JSONL with LF;
workspace-relative locators only; no absolute paths; no provider secrets,
profiles, prompts, raw model responses or semantic cache; every ACTIVE claim
carries evidence; every relation endpoint and relation-evidence row resolves
inside the bundle; the manifest records runtime version, bundle schema
version, workspace, knowledge revision, timestamp, per-file SHA-256 hashes,
record counts and honest warnings.

KNOWN DRAFT LIMITATION: ``--knowledge-revision N`` filters records by their
created/updated revision stamps (created <= N). It does NOT reconstruct
point-in-time lifecycle states — an object superseded after revision N still
reports its current lifecycle. The manifest says so.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import db
from ..version import RUNTIME_VERSION
from . import store
from .reconciliation import reconcile_graph_staleness
from .vocabularies import GraphLifecycleStatus

BUNDLE_SCHEMA_VERSION = "2.0.0-draft.1"

#: Row caps per file — a bundle is an export, not a database dump. Exceeding
#: a cap truncates WITH a manifest warning, never silently.
MAX_ROWS_PER_FILE = 200_000

_ACTIVE = GraphLifecycleStatus.ACTIVE


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _jsonl(records: Sequence[Dict[str, Any]]) -> str:
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")) for r in records]
    return "\n".join(lines) + ("\n" if lines else "")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sorted(records: List[Dict[str, Any]], *keys: str
            ) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda r: tuple(str(r.get(k, "") or "")
                                               for k in keys))


def export_bundle(workspace_id: str, output_dir: str, *,
                  current_only: bool = True,
                  knowledge_revision: Optional[int] = None,
                  generated_at: str = "") -> Dict[str, Any]:
    """Write the bundle directory. Returns the manifest."""
    workspace = db.get_project(workspace_id)
    if not workspace:
        from ..domain.errors import WorkspaceNotFound
        raise WorkspaceNotFound(workspace_id)
    if current_only:
        # A current-only export must not present knowledge whose sources
        # moved on as current (spec §22).
        reconcile_graph_staleness(workspace_id)

    warnings: List[str] = []
    revision_cap = (int(knowledge_revision)
                    if knowledge_revision is not None else None)

    def lifecycle_ok(row: Dict[str, Any]) -> bool:
        if revision_cap is not None and \
                int(row.get("created_knowledge_revision") or 0) > revision_cap:
            return False
        if current_only:
            return row.get("lifecycle_status") == _ACTIVE
        return True

    def cap(records: List[Dict[str, Any]], name: str
            ) -> List[Dict[str, Any]]:
        if len(records) > MAX_ROWS_PER_FILE:
            warnings.append(
                f"{name}: truncated to {MAX_ROWS_PER_FILE} of "
                f"{len(records)} records")
            return records[:MAX_ROWS_PER_FILE]
        return records

    # -- graph rows ----------------------------------------------------------
    entities = [e for e in store.list_entities(workspace_id,
                                               lifecycle_status=None,
                                               limit=MAX_ROWS_PER_FILE + 1)
                if lifecycle_ok(e)]
    entity_ids = {e["id"] for e in entities}

    claims: List[Dict[str, Any]] = []
    claim_evidence: List[Dict[str, Any]] = []
    for claim in store.list_claims(workspace_id, lifecycle_status=None,
                                   limit=MAX_ROWS_PER_FILE + 1):
        if not lifecycle_ok(claim) or claim["entity_id"] not in entity_ids:
            continue
        claims.append(claim)
        for ev in store.claim_evidence(claim["id"]):
            claim_evidence.append({"claim_id": claim["id"], **ev})
    claim_ids = {c["id"] for c in claims}

    relations: List[Dict[str, Any]] = []
    relation_evidence: List[Dict[str, Any]] = []
    for relation in store.list_relations(workspace_id, lifecycle_status=None,
                                         limit=MAX_ROWS_PER_FILE + 1):
        if not lifecycle_ok(relation):
            continue
        if relation["source_entity_id"] not in entity_ids or \
                relation["target_entity_id"] not in entity_ids:
            # Endpoint filtered out (revision cap / current-only): the
            # relation cannot be represented whole, so it is excluded.
            continue
        relations.append(relation)
        for ev in store.relation_evidence(relation["id"]):
            relation_evidence.append({"relation_id": relation["id"], **ev})

    aliases: List[Dict[str, Any]] = []
    bindings: List[Dict[str, Any]] = []
    for entity in entities:
        for alias in store.list_aliases(workspace_id, entity["id"],
                                        status=None):
            if current_only and alias["status"] != "active":
                continue
            if revision_cap is not None and \
                    int(alias.get("created_knowledge_revision") or 0) > \
                    revision_cap:
                continue
            aliases.append(alias)
        for binding in store.list_bindings(workspace_id, entity["id"]):
            if current_only and binding["status"] != "active":
                continue
            if revision_cap is not None and \
                    int(binding.get("created_knowledge_revision") or 0) > \
                    revision_cap:
                continue
            bindings.append(binding)

    revisions_ledger = [r for r in store.list_revisions(
        workspace_id, limit=MAX_ROWS_PER_FILE + 1)
        if revision_cap is None or r["revision_number"] <= revision_cap]
    decision_rows = [d for d in store.list_decisions(
        workspace_id, limit=MAX_ROWS_PER_FILE + 1)]
    if revision_cap is not None:
        ledger_ids = {r["id"] for r in revisions_ledger}
        decision_rows = [d for d in decision_rows
                         if d["knowledge_revision_id"] in ledger_ids]

    # -- source plane --------------------------------------------------------
    assets = db.list_assets(workspace_id, state=None,
                            limit=MAX_ROWS_PER_FILE + 1)
    if current_only:
        assets = [a for a in assets if a["state"] == "active"]
    asset_rows = [dict(a) for a in assets]

    revision_rows: List[Dict[str, Any]] = []
    segment_rows: List[Dict[str, Any]] = []
    evidence_rows: List[Dict[str, Any]] = []
    seen_evidence: set = set()
    for asset in assets:
        asset_revisions = db.list_revisions(workspace_id, asset["id"],
                                            limit=1000)
        if current_only:
            asset_revisions = [r for r in asset_revisions
                               if r["id"] == asset.get(
                                   "current_revision_id")]
        for revision in asset_revisions:
            revision_rows.append({**revision, "asset_id": asset["id"]})
            for segment in db.list_segments(workspace_id, revision["id"],
                                            limit=5000):
                segment_rows.append(segment)
                evidence = db.get_evidence_for_segment(workspace_id,
                                                       segment["id"])
                if evidence and evidence["id"] not in seen_evidence:
                    seen_evidence.add(evidence["id"])
                    evidence_rows.append(evidence)
    # Evidence cited by exported claims/relations must resolve inside the
    # bundle even when its revision fell outside the source-plane slice.
    for join in claim_evidence + relation_evidence:
        evidence_id = join["evidence_id"]
        if evidence_id in seen_evidence:
            continue
        record = db.get_evidence(workspace_id, evidence_id)
        if record:
            seen_evidence.add(evidence_id)
            evidence_rows.append(record)
        else:
            warnings.append(
                f"evidence {evidence_id} cited by the graph no longer "
                f"resolves in this workspace")

    # -- lenses (definitions only; provider provenance names are stripped) ---
    from ..semantic import store as semantic_store
    lens_rows = []
    for lens in semantic_store.list_lenses(workspace_id, limit=1000):
        lens_rows.append({
            "id": lens["id"], "name": lens["name"],
            "version": lens["version"], "source": lens["source"],
            "status": lens["status"],
            "schema_version": lens["schema_version"],
            "definition": lens["definition"],
            "created_at": lens["created_at"],
        })

    # -- write ---------------------------------------------------------------
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schemas").mkdir(exist_ok=True)

    files: Dict[str, str] = {
        "workspace.json": json.dumps({
            "id": workspace["id"], "name": workspace["name"],
            "created_at": workspace["created_at"],
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        "assets.jsonl": _jsonl(cap(_sorted(asset_rows, "logical_key", "id"),
                                   "assets")),
        "revisions.jsonl": _jsonl(cap(_sorted(revision_rows, "asset_id",
                                              "id"), "revisions")),
        "segments.jsonl": _jsonl(cap(_sorted(segment_rows, "revision_id",
                                             "id"), "segments")),
        "evidence.jsonl": _jsonl(cap(_sorted(evidence_rows, "id"),
                                     "evidence")),
        "entities.jsonl": _jsonl(cap(_sorted(entities, "canonical_key",
                                             "id"), "entities")),
        "aliases.jsonl": _jsonl(cap(_sorted(aliases, "entity_id",
                                            "normalized_alias", "id"),
                                    "aliases")),
        "bindings.jsonl": _jsonl(cap(_sorted(bindings, "entity_id",
                                             "ref_kind", "ref_id", "id"),
                                     "bindings")),
        "claims.jsonl": _jsonl(cap(_sorted(claims, "entity_id", "id"),
                                   "claims")),
        "claim-evidence.jsonl": _jsonl(cap(_sorted(
            claim_evidence, "claim_id", "evidence_id", "quote_hash"),
            "claim-evidence")),
        "relations.jsonl": _jsonl(cap(_sorted(
            relations, "relation_type", "source_entity_id",
            "target_entity_id", "id"), "relations")),
        "relation-evidence.jsonl": _jsonl(cap(_sorted(
            relation_evidence, "relation_id", "evidence_id", "quote_hash"),
            "relation-evidence")),
        "decisions.jsonl": _jsonl(cap(_sorted(decision_rows, "created_at",
                                              "id"), "decisions")),
        "knowledge-revisions.jsonl": _jsonl(cap(sorted(
            revisions_ledger, key=lambda r: int(r["revision_number"])),
            "knowledge-revisions")),
        "lenses.jsonl": _jsonl(cap(_sorted(lens_rows, "name", "id"),
                                   "lenses")),
    }
    files.update(_schemas())

    file_meta: Dict[str, Dict[str, Any]] = {}
    for name, content in files.items():
        data = content.encode("utf-8")
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        records = (content.count("\n") if name.endswith(".jsonl") else None)
        file_meta[name] = {"sha256": _sha256(data)}
        if records is not None:
            file_meta[name]["records"] = records

    manifest = {
        "bundleSchemaVersion": BUNDLE_SCHEMA_VERSION,
        "generator": {"name": "open-mind", "version": RUNTIME_VERSION},
        "workspaceId": workspace["id"],
        "workspaceName": workspace["name"],
        "knowledgeRevision": store.current_revision_number(workspace_id),
        "generatedAt": generated_at or _now_iso(),
        "mode": {"currentOnly": bool(current_only),
                 "knowledgeRevisionCap": revision_cap},
        "counts": {
            "assets": len(asset_rows), "revisions": len(revision_rows),
            "segments": len(segment_rows), "evidence": len(evidence_rows),
            "entities": len(entities), "aliases": len(aliases),
            "bindings": len(bindings), "claims": len(claims),
            "claimEvidence": len(claim_evidence),
            "relations": len(relations),
            "relationEvidence": len(relation_evidence),
            "decisions": len(decision_rows),
            "knowledgeRevisions": len(revisions_ledger),
            "lenses": len(lens_rows),
        },
        "files": dict(sorted(file_meta.items())),
        "warnings": warnings,
        "limitations": [
            "draft schema: layout may change until the 2.0.0 freeze",
            "knowledge-revision cap filters by creation revision stamps; "
            "it does not reconstruct point-in-time lifecycle states",
        ],
    }
    (out / "manifest.json").write_bytes(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        indent=2).encode("utf-8") + b"\n")
    return manifest


def _schemas() -> Dict[str, str]:
    """Minimal draft JSON Schemas shipped inside the bundle."""
    def schema(title: str, required: List[str]) -> str:
        return json.dumps({
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": title, "type": "object",
            "required": required,
            "additionalProperties": True,
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    return {
        "schemas/manifest.schema.json": schema(
            "KnowledgeBundleManifest",
            ["bundleSchemaVersion", "workspaceId", "knowledgeRevision",
             "generatedAt", "files", "counts"]),
        "schemas/entity.schema.json": schema(
            "EngineeringEntity",
            ["id", "workspace_id", "entity_type", "canonical_key",
             "lifecycle_status", "authority_status", "origin"]),
        "schemas/claim.schema.json": schema(
            "EngineeringClaim",
            ["id", "workspace_id", "entity_id", "claim_type", "statement",
             "lifecycle_status", "origin"]),
        "schemas/relation.schema.json": schema(
            "EngineeringRelation",
            ["id", "workspace_id", "source_entity_id", "target_entity_id",
             "relation_type", "relation_state", "lifecycle_status",
             "origin"]),
        "schemas/alias.schema.json": schema(
            "EntityAlias", ["id", "entity_id", "alias", "normalized_alias",
                            "alias_type", "status"]),
        "schemas/binding.schema.json": schema(
            "EntityBinding", ["id", "entity_id", "ref_kind", "ref_id",
                              "binding_role", "status"]),
        "schemas/decision.schema.json": schema(
            "KnowledgeDecision", ["id", "workspace_id", "decision_type",
                                  "target_kind", "created_at"]),
        "schemas/knowledge-revision.schema.json": schema(
            "KnowledgeRevision", ["id", "workspace_id", "revision_number",
                                  "action", "created_at"]),
        "schemas/evidence.schema.json": schema(
            "Evidence", ["id", "revision_id", "locator"]),
    }


__all__ = ["export_bundle", "BUNDLE_SCHEMA_VERSION"]
