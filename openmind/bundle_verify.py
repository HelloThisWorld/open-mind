"""Standalone Knowledge Bundle 2.0 Draft verifier.

    python -m openmind.bundle_verify <directory>

Deliberately dependency-free and database-free: standard library only, no
OpenMind runtime import, no vector store, no provider. It validates a bundle
directory AS AN ARTIFACT — the way an external consumer would — so a bundle
that only verifies against the database that produced it is caught here.

CHECKS
------
* manifest parses, carries the draft schema version, workspace, revision;
* every file the manifest names exists, hash-matches and record-count-matches;
* every JSONL line parses; ids are unique per file;
* referential integrity: claims -> entities, aliases/bindings -> entities,
  relation endpoints -> entities, claim/relation evidence joins -> claims/
  relations AND -> evidence rows;
* every ACTIVE claim has at least one evidence join;
* deterministic ordering where the exporter guarantees it;
* no machine-absolute path in locators, source paths or binding keys
  (workspace-relative and API-route slashes are legitimate);
* no obvious secret material (bearer tokens, api-key assignments).

Exit code 0 = valid (warnings allowed), 1 = invalid, 2 = unusable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Machine-absolute path shapes. A leading slash alone is NOT absolute here:
#: API routes ("/name-check") and POSIX-relative-ish locators are legitimate;
#: what must never leak is a real filesystem root.
_ABSOLUTE_PATH = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|var|tmp|etc|mnt|opt|srv|root)/)")

_SECRET_HINT = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer\s+[A-Za-z0-9._-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")

_PATH_FIELDS = ("file", "document", "source_path", "ref_key", "logical_key")

_JSONL_FILES = (
    "assets.jsonl", "revisions.jsonl", "segments.jsonl", "evidence.jsonl",
    "entities.jsonl", "aliases.jsonl", "bindings.jsonl", "claims.jsonl",
    "claim-evidence.jsonl", "relations.jsonl", "relation-evidence.jsonl",
    "decisions.jsonl", "knowledge-revisions.jsonl", "lenses.jsonl",
)

#: Phase 6 opt-in files, present only when the manifest mode flags say so.
_TRACE_FILES = (
    "traceability-policies.jsonl", "traceability-runs.jsonl",
    "trace-paths.jsonl", "trace-path-steps.jsonl", "trace-gaps.jsonl",
    "coverage-snapshots.jsonl",
)
_CONFLICT_FILES = (
    "conflicts.jsonl", "conflict-objects.jsonl", "conflict-evidence.jsonl",
    "conflict-decisions.jsonl",
)


class Report:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_jsonl(path: Path, report: Report) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        report.error(f"missing file: {path.name}")
        return records
    for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            report.error(f"{path.name}:{lineno}: invalid JSON ({exc})")
    return records


def _check_unique_ids(name: str, records: List[Dict[str, Any]],
                      report: Report, key: str = "id") -> None:
    seen: set = set()
    for record in records:
        value = record.get(key)
        if value is None:
            continue
        if value in seen:
            report.error(f"{name}: duplicate {key} {value!r}")
        seen.add(value)


def _check_sorted(name: str, records: List[Dict[str, Any]],
                  keys: List[str], report: Report) -> None:
    def sort_key(r: Dict[str, Any]):
        return tuple(str(r.get(k, "") or "") for k in keys)
    actual = [sort_key(r) for r in records]
    if actual != sorted(actual):
        report.error(f"{name}: records are not in deterministic "
                     f"{'/'.join(keys)} order")


def _scan_paths(name: str, records: List[Dict[str, Any]],
                report: Report) -> None:
    def scan(value: Any, context: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in _PATH_FIELDS and isinstance(inner, str) and \
                        _ABSOLUTE_PATH.match(inner):
                    report.error(
                        f"{name}: absolute path in {context}.{key}: "
                        f"{inner!r}")
                scan(inner, f"{context}.{key}")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                scan(inner, f"{context}[{i}]")
        elif isinstance(value, str) and _SECRET_HINT.search(value):
            report.error(f"{name}: possible secret material in {context}")

    for i, record in enumerate(records):
        scan(record, f"record[{i}]")


def verify_bundle(directory: str) -> Report:
    report = Report()
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        report.error(f"missing manifest.json in {root}")
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.error(f"manifest.json: invalid JSON ({exc})")
        return report

    version = str(manifest.get("bundleSchemaVersion") or "")
    if not version.startswith("2.0.0-draft"):
        report.error(f"unexpected bundleSchemaVersion: {version!r}")
    for field in ("workspaceId", "knowledgeRevision", "generatedAt",
                  "files", "counts"):
        if field not in manifest:
            report.error(f"manifest.json: missing field {field!r}")

    # -- file hashes and record counts --------------------------------------
    files: Dict[str, Dict[str, Any]] = manifest.get("files") or {}
    for name, meta in sorted(files.items()):
        path = root / name
        if not path.exists():
            report.error(f"manifest names missing file: {name}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != meta.get("sha256"):
            report.error(f"{name}: sha256 mismatch (manifest "
                         f"{str(meta.get('sha256'))[:12]}..., actual "
                         f"{digest[:12]}...)")
        if "records" in meta:
            actual = sum(1 for line in
                         path.read_text(encoding="utf-8").splitlines()
                         if line.strip())
            if actual != meta["records"]:
                report.error(f"{name}: record count mismatch (manifest "
                             f"{meta['records']}, actual {actual})")
    for name in _JSONL_FILES:
        if name not in files:
            report.error(f"manifest is missing required file entry: {name}")

    # -- load ----------------------------------------------------------------
    data = {name: _read_jsonl(root / name, report)
            for name in _JSONL_FILES}
    entities = data["entities.jsonl"]
    claims = data["claims.jsonl"]
    relations = data["relations.jsonl"]
    evidence = data["evidence.jsonl"]

    for name in _JSONL_FILES:
        if name in ("claim-evidence.jsonl", "relation-evidence.jsonl"):
            continue
        _check_unique_ids(name, data[name], report)

    entity_ids = {e.get("id") for e in entities}
    claim_ids = {c.get("id") for c in claims}
    relation_ids = {r.get("id") for r in relations}
    evidence_ids = {e.get("id") for e in evidence}

    # -- referential integrity ----------------------------------------------
    for claim in claims:
        if claim.get("entity_id") not in entity_ids:
            report.error(f"claims: {claim.get('id')} references missing "
                         f"entity {claim.get('entity_id')}")
    for alias in data["aliases.jsonl"]:
        if alias.get("entity_id") not in entity_ids:
            report.error(f"aliases: {alias.get('id')} references missing "
                         f"entity {alias.get('entity_id')}")
    for binding in data["bindings.jsonl"]:
        if binding.get("entity_id") not in entity_ids:
            report.error(f"bindings: {binding.get('id')} references missing "
                         f"entity {binding.get('entity_id')}")
    for relation in relations:
        for end in ("source_entity_id", "target_entity_id"):
            if relation.get(end) not in entity_ids:
                report.error(
                    f"relations: {relation.get('id')} {end} references "
                    f"missing entity {relation.get(end)}")
    claim_evidence_by_claim: Dict[Any, int] = {}
    for join in data["claim-evidence.jsonl"]:
        claim_evidence_by_claim[join.get("claim_id")] = \
            claim_evidence_by_claim.get(join.get("claim_id"), 0) + 1
        if join.get("claim_id") not in claim_ids:
            report.error(f"claim-evidence: join references missing claim "
                         f"{join.get('claim_id')}")
        if join.get("evidence_id") not in evidence_ids:
            report.error(f"claim-evidence: join references missing evidence "
                         f"{join.get('evidence_id')}")
    for join in data["relation-evidence.jsonl"]:
        if join.get("relation_id") not in relation_ids:
            report.error(
                f"relation-evidence: join references missing relation "
                f"{join.get('relation_id')}")
        if join.get("evidence_id") not in evidence_ids:
            report.error(
                f"relation-evidence: join references missing evidence "
                f"{join.get('evidence_id')}")

    # -- active claims must carry evidence -----------------------------------
    for claim in claims:
        if claim.get("lifecycle_status") == "active" and \
                not claim_evidence_by_claim.get(claim.get("id")):
            report.error(f"claims: active claim {claim.get('id')} has no "
                         f"evidence join")

    # -- deterministic ordering ----------------------------------------------
    _check_sorted("entities.jsonl", entities, ["canonical_key", "id"],
                  report)
    _check_sorted("claims.jsonl", claims, ["entity_id", "id"], report)
    _check_sorted("relations.jsonl", relations,
                  ["relation_type", "source_entity_id", "target_entity_id",
                   "id"], report)
    _check_sorted("evidence.jsonl", evidence, ["id"], report)
    ledger = data["knowledge-revisions.jsonl"]
    numbers = [int(r.get("revision_number") or 0) for r in ledger]
    if numbers != sorted(numbers):
        report.error("knowledge-revisions.jsonl: revision numbers are not "
                     "ascending")

    # -- path and secret scan -------------------------------------------------
    for name in _JSONL_FILES:
        _scan_paths(name, data[name], report)

    counts = manifest.get("counts") or {}
    expectation = {
        "entities": len(entities), "claims": len(claims),
        "relations": len(relations), "evidence": len(evidence),
        "aliases": len(data["aliases.jsonl"]),
        "bindings": len(data["bindings.jsonl"]),
        "decisions": len(data["decisions.jsonl"]),
        "knowledgeRevisions": len(ledger),
    }
    for key, actual in expectation.items():
        if key in counts and int(counts[key]) != actual:
            report.error(f"manifest counts.{key} = {counts[key]} but file "
                         f"has {actual} records")

    # -- Phase 6: traceability + conflicts (opt-in files) --------------------
    mode = manifest.get("mode") or {}
    current_only = bool(mode.get("currentOnly"))
    if mode.get("includeTraceability"):
        _verify_traceability(root, report, files, entity_ids, relation_ids,
                             current_only)
    if mode.get("includeConflicts"):
        _verify_conflicts(root, report, files, entity_ids, claim_ids,
                          relation_ids, evidence_ids)
    return report


def _verify_traceability(root: Path, report: Report,
                         files: Dict[str, Any], entity_ids: set,
                         relation_ids: set, current_only: bool) -> None:
    for name in _TRACE_FILES:
        if name not in files:
            report.error(f"manifest declares includeTraceability but is "
                         f"missing file entry: {name}")
    data = {name: _read_jsonl(root / name, report)
            for name in _TRACE_FILES}
    for name in ("traceability-runs.jsonl", "trace-paths.jsonl",
                 "trace-gaps.jsonl", "coverage-snapshots.jsonl"):
        _check_unique_ids(name, data[name], report)
    for name in _TRACE_FILES:
        _scan_paths(name, data[name], report)

    paths = data["trace-paths.jsonl"]
    steps = data["trace-path-steps.jsonl"]
    path_ids = {p.get("id") for p in paths}

    # Trace root/target entities and step relations must exist.
    for path in paths:
        if path.get("root_entity_id") not in entity_ids:
            report.error(f"trace-paths: {path.get('id')} references "
                         f"missing root entity "
                         f"{path.get('root_entity_id')}")
        target = path.get("target_entity_id")
        if target and target not in entity_ids:
            report.error(f"trace-paths: {path.get('id')} references "
                         f"missing target entity {target}")
        if not path.get("policy_checksum"):
            report.error(f"trace-paths: {path.get('id')} has no policy "
                         f"checksum")
    steps_by_path: Dict[Any, List[int]] = {}
    for step in steps:
        path_id = step.get("trace_path_id")
        if path_id not in path_ids:
            report.error(f"trace-path-steps: {step.get('id')} references "
                         f"missing path {path_id}")
            continue
        steps_by_path.setdefault(path_id, []).append(
            int(step.get("ordinal") or 0))
        if step.get("node_kind") == "entity" and \
                step.get("node_id") not in entity_ids:
            report.error(f"trace-path-steps: {step.get('id')} references "
                         f"missing entity {step.get('node_id')}")
        relation_id = step.get("relation_id")
        if relation_id and relation_id not in relation_ids:
            report.error(f"trace-path-steps: {step.get('id')} references "
                         f"missing relation {relation_id}")
    for path_id, ordinals in sorted(steps_by_path.items(),
                                    key=lambda kv: str(kv[0])):
        if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
            report.error(f"trace-path-steps: path {path_id} steps are not "
                         f"densely ordered 1..n (got {sorted(ordinals)})")

    for gap in data["trace-gaps.jsonl"]:
        gap_root = gap.get("root_entity_id")
        if gap_root and gap_root not in entity_ids:
            report.error(f"trace-gaps: {gap.get('id')} references missing "
                         f"root entity {gap_root}")
        if not gap.get("policy_checksum"):
            report.error(f"trace-gaps: {gap.get('id')} has no policy "
                         f"checksum")

    for snapshot in data["coverage-snapshots.jsonl"]:
        if not snapshot.get("policy_checksum"):
            report.error(f"coverage-snapshots: {snapshot.get('id')} has no "
                         f"policy checksum")
        if current_only and snapshot.get("stale_at"):
            report.error(f"coverage-snapshots: current-only export "
                         f"contains stale snapshot {snapshot.get('id')}")
        metrics = (snapshot.get("metrics") or {}).get("requirements") or {}
        for key, ratio in sorted(metrics.items()):
            if not isinstance(ratio, dict) or "numerator" not in ratio:
                continue
            numerator = ratio.get("numerator")
            denominator = ratio.get("denominator")
            percentage = ratio.get("percentage")
            if denominator == 0 and percentage is not None:
                report.error(
                    f"coverage-snapshots: {snapshot.get('id')} "
                    f"{key}: zero denominator must have null percentage")
            if denominator and percentage is not None:
                expected = round(100.0 * numerator / denominator, 2)
                if abs(percentage - expected) > 0.01:
                    report.error(
                        f"coverage-snapshots: {snapshot.get('id')} {key}: "
                        f"percentage {percentage} does not match "
                        f"{numerator}/{denominator}")


def _verify_conflicts(root: Path, report: Report, files: Dict[str, Any],
                      entity_ids: set, claim_ids: set, relation_ids: set,
                      evidence_ids: set) -> None:
    for name in _CONFLICT_FILES:
        if name not in files:
            report.error(f"manifest declares includeConflicts but is "
                         f"missing file entry: {name}")
    data = {name: _read_jsonl(root / name, report)
            for name in _CONFLICT_FILES}
    _check_unique_ids("conflicts.jsonl", data["conflicts.jsonl"], report)
    _check_unique_ids("conflict-decisions.jsonl",
                      data["conflict-decisions.jsonl"], report)
    for name in _CONFLICT_FILES:
        _scan_paths(name, data[name], report)

    conflicts = data["conflicts.jsonl"]
    conflict_ids = {c.get("id") for c in conflicts}
    by_kind = {"entity": entity_ids, "claim": claim_ids,
               "relation": relation_ids}
    for obj in data["conflict-objects.jsonl"]:
        if obj.get("conflict_id") not in conflict_ids:
            report.error(f"conflict-objects: join references missing "
                         f"conflict {obj.get('conflict_id')}")
        known = by_kind.get(obj.get("object_kind"))
        if known is not None and obj.get("object_id") not in known:
            report.error(f"conflict-objects: conflict "
                         f"{obj.get('conflict_id')} references missing "
                         f"{obj.get('object_kind')} {obj.get('object_id')}")
    for join in data["conflict-evidence.jsonl"]:
        if join.get("conflict_id") not in conflict_ids:
            report.error(f"conflict-evidence: join references missing "
                         f"conflict {join.get('conflict_id')}")
        if join.get("evidence_id") not in evidence_ids:
            report.error(f"conflict-evidence: join references missing "
                         f"evidence {join.get('evidence_id')}")
    for decision in data["conflict-decisions.jsonl"]:
        if decision.get("conflict_id") not in conflict_ids:
            report.error(f"conflict-decisions: {decision.get('id')} "
                         f"references missing conflict "
                         f"{decision.get('conflict_id')}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openmind.bundle_verify",
        description="Verify a Knowledge Bundle 2.0 Draft directory.")
    parser.add_argument("directory", help="the bundle directory "
                                          "(contains manifest.json)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print one machine-readable JSON object")
    args = parser.parse_args(argv)

    if not Path(args.directory).is_dir():
        message = f"not a directory: {args.directory}"
        if args.as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2

    report = verify_bundle(args.directory)
    if args.as_json:
        print(json.dumps({"ok": report.ok, "errors": report.errors,
                          "warnings": report.warnings}, indent=2))
    else:
        for warning in report.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
        print(("VALID" if report.ok else "INVALID")
              + f" — {len(report.errors)} error(s), "
                f"{len(report.warnings)} warning(s)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
