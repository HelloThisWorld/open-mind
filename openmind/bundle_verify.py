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
    return report


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
