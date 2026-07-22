"""Built-in Traceability Policies, organization policy files and policy
resolution.

Built-ins are conservative declarative data in this module. Organization
policies are user-managed ``.json`` / ``.yaml`` / ``.yml`` files in a
machine-local directory (``OPENMIND_TRACE_POLICY_DIR``, default
``<data dir>/trace-policies``) — schema-validated, checksummed, size-capped,
listable even when invalid, never containing executable code. Selecting a
policy for a workspace is a governance action handled by the service; this
module only resolves names to validated policies.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from .errors import PolicyInvalid, PolicyNotFound
from .models import TraceabilityPolicy
from .validator import validate_policy_data
from .vocabularies import PolicySource

_POLICY_EXTS = (".json", ".yaml", ".yml")
MAX_POLICY_FILE_BYTES = 256 * 1024


def policies_dir() -> Path:
    override = os.environ.get("OPENMIND_TRACE_POLICY_DIR", "").strip()
    return Path(override) if override else (config.DATA_DIR
                                            / "trace-policies")


# ---------------------------------------------------------------------------
# Built-in policies (conservative; spec §8)
# ---------------------------------------------------------------------------
def _builtin(name: str, title: str, root_types: List[str],
             stages: List[Dict[str, Any]],
             transitions: List[Dict[str, Any]],
             rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "schemaVersion": "1.0.0", "name": name, "title": title,
        "rootTypes": root_types, "stages": stages,
        "transitions": transitions, "rules": rules or {},
    }


_IMPLEMENTATION_TYPES = ["code-component", "code-symbol", "configuration",
                         "database-object", "message-topic"]

BUILTIN_POLICY_DATA: Dict[str, Dict[str, Any]] = {
    "generic-engineering": _builtin(
        "generic-engineering", "Generic Engineering Traceability",
        ["requirement", "business-rule", "constraint"],
        stages=[
            {"name": "requirement", "required": True,
             "entityTypes": ["requirement", "business-rule", "constraint"]},
            {"name": "design", "required": False,
             "entityTypes": ["design", "decision", "workflow"]},
            {"name": "interface", "required": False,
             "entityTypes": ["interface", "data-model"]},
            {"name": "implementation", "required": True,
             "entityTypes": _IMPLEMENTATION_TYPES},
            {"name": "verification", "required": True,
             "entityTypes": ["acceptance-criterion", "test-case"]},
            {"name": "evidence", "required": False,
             "requiresEvidence": True, "entityTypes": ["test-result"]},
        ],
        transitions=[
            {"from": "requirement", "to": "design",
             "relationTypes": ["refines", "derived-from"]},
            {"from": "requirement", "to": "interface",
             "relationTypes": ["refines", "contains", "derived-from"]},
            {"from": "requirement", "to": "implementation",
             "relationTypes": ["implements", "partially-implements"]},
            {"from": "design", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "design", "to": "implementation",
             "relationTypes": ["implements", "partially-implements"]},
            {"from": "interface", "to": "implementation",
             "relationTypes": ["implements", "partially-implements",
                               "configures"]},
            {"from": "implementation", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "requirement", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "verification", "to": "evidence",
             "relationTypes": ["evidenced-by", "verifies"]},
        ]),
    "api-service": _builtin(
        "api-service", "API Service Traceability",
        ["requirement", "business-rule"],
        stages=[
            {"name": "requirement", "required": True,
             "entityTypes": ["requirement", "business-rule"]},
            {"name": "design", "required": False,
             "entityTypes": ["design", "decision", "constraint",
                             "workflow"]},
            {"name": "interface", "required": True,
             "entityTypes": ["interface", "data-model"]},
            {"name": "implementation", "required": True,
             "entityTypes": _IMPLEMENTATION_TYPES},
            {"name": "verification", "required": True,
             "entityTypes": ["acceptance-criterion", "test-case"]},
            {"name": "evidence", "required": True,
             "requiresEvidence": True, "entityTypes": ["test-result"]},
        ],
        transitions=[
            {"from": "requirement", "to": "design",
             "relationTypes": ["refines", "derived-from"]},
            {"from": "requirement", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "design", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "interface", "to": "implementation",
             "relationTypes": ["implements", "partially-implements",
                               "configures"]},
            {"from": "implementation", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "verification", "to": "evidence",
             "relationTypes": ["evidenced-by", "verifies"]},
        ]),
    "event-driven-service": _builtin(
        "event-driven-service", "Event-Driven Service Traceability",
        ["requirement", "business-rule"],
        stages=[
            {"name": "requirement", "required": True,
             "entityTypes": ["requirement", "business-rule"]},
            {"name": "design", "required": False,
             "entityTypes": ["design", "decision", "workflow"]},
            {"name": "interface", "required": True,
             "entityTypes": ["interface", "message-topic", "data-model"]},
            {"name": "implementation", "required": True,
             "entityTypes": ["code-component", "code-symbol",
                             "configuration", "database-object"]},
            {"name": "verification", "required": True,
             "entityTypes": ["acceptance-criterion", "test-case"]},
            {"name": "evidence", "required": False,
             "requiresEvidence": True, "entityTypes": ["test-result"]},
        ],
        transitions=[
            {"from": "requirement", "to": "design",
             "relationTypes": ["refines", "derived-from"]},
            {"from": "requirement", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "design", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "interface", "to": "implementation",
             "relationTypes": ["implements", "partially-implements",
                               "publishes", "consumes", "configures"]},
            {"from": "implementation", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "verification", "to": "evidence",
             "relationTypes": ["evidenced-by", "verifies"]},
        ]),
    "batch-processing": _builtin(
        "batch-processing", "Batch Processing Traceability",
        ["requirement", "business-rule"],
        stages=[
            {"name": "requirement", "required": True,
             "entityTypes": ["requirement", "business-rule"]},
            {"name": "workflow", "required": True,
             "entityTypes": ["workflow", "batch-job"]},
            {"name": "data", "required": False,
             "entityTypes": ["data-model", "database-object"]},
            {"name": "implementation", "required": True,
             "entityTypes": ["code-component", "code-symbol",
                             "configuration"]},
            {"name": "verification", "required": True,
             "entityTypes": ["acceptance-criterion", "test-case"]},
            {"name": "evidence", "required": False,
             "requiresEvidence": True, "entityTypes": ["test-result"]},
        ],
        transitions=[
            {"from": "requirement", "to": "workflow",
             "relationTypes": ["refines", "derived-from", "contains"]},
            {"from": "workflow", "to": "data",
             "relationTypes": ["reads", "writes", "contains", "refines"]},
            {"from": "workflow", "to": "implementation",
             "relationTypes": ["implements", "partially-implements"]},
            {"from": "data", "to": "implementation",
             "relationTypes": ["implements", "configures"]},
            {"from": "implementation", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "verification", "to": "evidence",
             "relationTypes": ["evidenced-by", "verifies"]},
        ]),
    "japanese-v-model": _builtin(
        "japanese-v-model", "Japanese V-Model Traceability",
        ["requirement", "business-rule"],
        stages=[
            {"name": "requirement", "required": True,
             "entityTypes": ["requirement", "business-rule"]},
            {"name": "design", "required": True,
             "entityTypes": ["design", "decision", "constraint"]},
            {"name": "interface", "required": False,
             "entityTypes": ["interface", "data-model"]},
            {"name": "implementation", "required": True,
             "entityTypes": _IMPLEMENTATION_TYPES},
            {"name": "verification", "required": True,
             "entityTypes": ["test-case", "acceptance-criterion"]},
            # The evidence requirement lives ON the test-result stage: the
            # terminal result record must carry verified current Evidence.
            {"name": "test-result", "required": True,
             "requiresEvidence": True, "entityTypes": ["test-result"]},
        ],
        transitions=[
            {"from": "requirement", "to": "design",
             "relationTypes": ["refines", "derived-from"]},
            {"from": "design", "to": "interface",
             "relationTypes": ["refines", "contains"]},
            {"from": "design", "to": "implementation",
             "relationTypes": ["implements", "partially-implements"]},
            {"from": "interface", "to": "implementation",
             "relationTypes": ["implements", "partially-implements",
                               "configures"]},
            {"from": "implementation", "to": "verification",
             "relationTypes": ["verifies"]},
            {"from": "verification", "to": "test-result",
             "relationTypes": ["evidenced-by", "verifies"]},
        ]),
}


def _validated_builtin(name: str) -> TraceabilityPolicy:
    policy, errors = validate_policy_data(BUILTIN_POLICY_DATA[name],
                                          source=PolicySource.BUILTIN)
    if errors:      # pragma: no cover - a broken built-in is a build bug
        raise PolicyInvalid(name, errors)
    return policy


def builtin_policies() -> List[TraceabilityPolicy]:
    return [_validated_builtin(name)
            for name in sorted(BUILTIN_POLICY_DATA)]


# ---------------------------------------------------------------------------
# Organization policy files
# ---------------------------------------------------------------------------
def _load_file(path: Path) -> Tuple[Any, Optional[str], str]:
    """(data, error, checksum-of-file-text)."""
    try:
        if path.stat().st_size > MAX_POLICY_FILE_BYTES:
            return None, (f"file exceeds {MAX_POLICY_FILE_BYTES} bytes"), ""
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"unreadable file: {exc}", ""
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path.suffix.lower() == ".json":
        import json
        try:
            return json.loads(text), None, checksum
        except Exception as exc:
            return None, f"invalid JSON: {exc}", checksum
    try:
        import yaml
    except Exception:
        return None, ("PyYAML is not installed — provide this policy as "
                      ".json"), checksum
    try:
        return yaml.safe_load(text), None, checksum
    except Exception as exc:
        return None, f"invalid YAML: {exc}", checksum


def list_organization_policies() -> List[Dict[str, Any]]:
    """Every policy file in the organization directory, valid or not, with
    validation errors attached. Deterministic order (by file name)."""
    directory = policies_dir()
    out: List[Dict[str, Any]] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in _POLICY_EXTS or not path.is_file():
            continue
        data, load_error, file_checksum = _load_file(path)
        record: Dict[str, Any] = {
            "file": path.name,
            "name": path.stem.lower(),
            "source": PolicySource.ORGANIZATION,
            "file_checksum": file_checksum,
            "valid": False, "errors": [],
        }
        if load_error:
            record["errors"] = [load_error]
            out.append(record)
            continue
        policy, errors = validate_policy_data(
            data, source=PolicySource.ORGANIZATION)
        record["name"] = policy.name or record["name"]
        record["title"] = policy.title
        record["errors"] = errors
        record["valid"] = not errors
        if not errors:
            record["policy"] = policy
            record["checksum"] = policy.checksum
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def list_policies() -> List[Dict[str, Any]]:
    """Built-ins plus organization files, in one bounded, deterministic
    listing. An organization policy with a built-in's name SHADOWS the
    built-in (resolution prefers organization), and the listing says so."""
    org = list_organization_policies()
    org_names = {r["name"] for r in org if r.get("valid")}
    out: List[Dict[str, Any]] = []
    for policy in builtin_policies():
        out.append({
            "name": policy.name, "title": policy.title,
            "source": PolicySource.BUILTIN,
            "checksum": policy.checksum, "valid": True, "errors": [],
            "shadowed_by_organization": policy.name in org_names,
        })
    for record in org:
        entry = {k: v for k, v in record.items() if k != "policy"}
        out.append(entry)
    return out


def resolve_policy(name: str) -> TraceabilityPolicy:
    """Resolve a policy name to a VALIDATED policy. Organization files take
    precedence over a built-in of the same name; an invalid organization
    policy is a typed failure, never a silent fallback to the built-in."""
    clean = str(name or "").strip().lower()
    if not clean:
        raise PolicyNotFound(name)
    for record in list_organization_policies():
        if record["name"] == clean:
            if not record["valid"]:
                raise PolicyInvalid(clean, record["errors"])
            return record["policy"]
    if clean in BUILTIN_POLICY_DATA:
        return _validated_builtin(clean)
    raise PolicyNotFound(name)


def validate_policy_document(data: Any) -> Dict[str, Any]:
    """Validate one raw policy document (CLI ``trace policy validate``).
    Never raises for validation failures — returns the report."""
    policy, errors = validate_policy_data(
        data, source=PolicySource.ORGANIZATION)
    report: Dict[str, Any] = {
        "valid": not errors, "errors": errors,
        "name": policy.name, "title": policy.title,
    }
    if not errors:
        report["checksum"] = policy.checksum
        report["stages"] = [s.name for s in policy.stages]
        report["required_stages"] = policy.required_stages()
    return report


DEFAULT_POLICY_NAME = "generic-engineering"

__all__ = [
    "BUILTIN_POLICY_DATA", "DEFAULT_POLICY_NAME", "MAX_POLICY_FILE_BYTES",
    "builtin_policies", "policies_dir", "list_organization_policies",
    "list_policies", "resolve_policy", "validate_policy_document",
]
