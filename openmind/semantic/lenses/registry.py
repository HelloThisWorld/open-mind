"""Organization lens files: discovery, checksum, listing.

Organization lenses are user-managed files (``.json`` / ``.yaml`` / ``.yml``)
in a local directory — ``<data dir>/lenses`` by default, overridable with
``OPENMIND_LENSES_DIR``. Like templates: files are re-read on every listing,
INVALID files stay visible with their errors, and nothing here is secret
(the schema has no field a secret could hide in, and the validator rejects
URLs inside patterns).

A directory file is only a SOURCE. Using one in a workspace is an explicit
import (:meth:`LensService.import_organization_lens`), which snapshots the
validated definition into a workspace-scoped row carrying the file's
checksum — so a later file edit never silently changes an imported lens.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import config
from .models import validate_lens_definition

_LENS_EXTS = (".json", ".yaml", ".yml")


def lenses_dir() -> Path:
    override = os.environ.get("OPENMIND_LENSES_DIR", "").strip()
    return Path(override) if override else (config.DATA_DIR / "lenses")


def _load_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"unreadable file: {exc}", ""
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text), None, checksum
        except Exception as exc:
            return None, f"invalid JSON: {exc}", checksum
    try:
        import yaml
    except Exception:
        return None, ("PyYAML is not installed — provide this lens as .json"), checksum
    try:
        return yaml.safe_load(text), None, checksum
    except Exception as exc:
        return None, f"invalid YAML: {exc}", checksum


def list_organization_lenses() -> List[Dict[str, Any]]:
    """Every lens file in the organization directory, valid or not, with
    validation attached. Deterministic order (by filename)."""
    directory = lenses_dir()
    if not directory.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _LENS_EXTS:
            continue
        data, load_error, checksum = _load_file(path)
        if load_error:
            out.append({
                "file": path.name, "checksum": checksum,
                "name": path.stem.lower(), "source": "organization",
                "definition": {},
                "validation": {"result": "invalid",
                               "errors": [load_error], "warnings": []},
                "stored": False,
            })
            continue
        normalized, errors, warnings = validate_lens_definition(
            data, source="organization")
        out.append({
            "file": path.name, "checksum": checksum,
            "name": normalized.get("name") or path.stem.lower(),
            "source": "organization",
            "definition": normalized,
            "validation": {
                "result": "valid" if not errors else "invalid",
                "errors": errors, "warnings": warnings},
            "stored": False,
        })
    return out


def get_organization_lens(name_or_file: str) -> Optional[Dict[str, Any]]:
    wanted = str(name_or_file or "").strip().lower()
    for record in list_organization_lenses():
        if record["name"] == wanted or record["file"].lower() == wanted:
            return record
    return None


__all__ = ["lenses_dir", "list_organization_lenses",
           "get_organization_lens"]
