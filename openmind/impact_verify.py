"""Standalone Change Impact Packet verifier (spec §30).

    python -m openmind.impact_verify ./.openmind-impact

Re-checks a packet directory WITHOUT any database: every file named in the
manifest must exist and hash to its recorded SHA-256; the record counts must
match the emitted rows; every Evidence id referenced by an impact must appear in
``evidence.jsonl``; and no absolute path may appear. Exits 0 on success, 1 on
any failure, printing a machine-readable JSON summary.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Tuple


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _looks_absolute(value: str) -> bool:
    v = str(value or "")
    return v.startswith("/") or (len(v) >= 2 and v[1] == ":" and v[0].isalpha())


def verify_packet(packet_dir: str) -> Tuple[bool, Dict[str, Any]]:
    errors: List[str] = []
    manifest_path = os.path.join(packet_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return False, {"ok": False, "errors": ["manifest.json missing"]}
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    # 1. hash every file named in the manifest.
    for name, expected in (manifest.get("fileHashes") or {}).items():
        path = os.path.join(packet_dir, name)
        if not os.path.isfile(path):
            errors.append(f"file listed in manifest is missing: {name}")
            continue
        actual = _sha256_file(path)
        if actual != expected:
            errors.append(f"hash mismatch for {name}: "
                          f"expected {expected[:12]}, got {actual[:12]}")

    # 2. record counts.
    counts = manifest.get("recordCounts") or {}
    file_rows = _read_jsonl(os.path.join(packet_dir, "file-changes.jsonl"))
    evidence_rows = _read_jsonl(os.path.join(packet_dir, "evidence.jsonl"))
    if "files" in counts and counts["files"] != len(file_rows):
        errors.append(f"file count mismatch: manifest {counts['files']} vs "
                      f"{len(file_rows)} rows")
    if "evidence" in counts and counts["evidence"] != len(evidence_rows):
        errors.append(f"evidence count mismatch: manifest {counts['evidence']} "
                      f"vs {len(evidence_rows)} rows")

    # 3. referential integrity: evidence ids referenced by impacts must exist.
    evidence_ids = {e.get("id") for e in evidence_rows}
    for fname in ("trace-impact.jsonl", "conflict-impact.jsonl"):
        for row in _read_jsonl(os.path.join(packet_dir, fname)):
            for eid in row.get("evidenceIds", []) or []:
                if eid and eid not in evidence_ids:
                    errors.append(f"{fname} references missing evidence {eid}")

    # 4. no absolute paths anywhere in the emitted rows or locators.
    for e in evidence_rows:
        loc = e.get("locator") or {}
        if _looks_absolute(loc.get("path", "")):
            errors.append(f"absolute path in evidence locator: {loc.get('path')}")
    for f in file_rows:
        if _looks_absolute(f.get("newPath", "")) or _looks_absolute(f.get("oldPath", "")):
            errors.append("absolute path in file-changes row")

    ok = not errors
    return ok, {"ok": ok, "errors": errors,
                "schemaVersion": manifest.get("schemaVersion"),
                "overlayId": manifest.get("overlayId"),
                "partial": manifest.get("partial", False),
                "checkedFiles": len(manifest.get("fileHashes") or {})}


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"ok": False,
                          "errors": ["usage: python -m openmind.impact_verify "
                                     "<packet-dir>"]}))
        return 2
    ok, summary = verify_packet(argv[1])
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
