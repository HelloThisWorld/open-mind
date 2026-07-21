"""Machine-local provider profiles.

Profiles live in ``<OPENMIND_MACHINE_DIR>/providers.json`` — the same sidecar
family as ``paths.json`` — because they are configuration about THIS machine's
reachable endpoints and credentials, and must never travel with a copied
``data/`` folder, an exported ``.openmind`` artifact, or the portable SQLite
database.

SECRET HANDLING (the load-bearing rule)
---------------------------------------
A profile stores the NAME of an environment variable (``api_key_env``), never
a key value. :func:`resolve_api_key` reads the variable at call time; nothing
in this module, the store, the logs or the CLI ever persists or prints the
value. There is deliberately no ``--api-key`` CLI flag and no ``api_key``
field for this file — a key pasted into either would end up in shell history
or on disk.

Validation is static (:func:`validate_profile`): it inspects the
configuration and the presence of the SDK and the environment variable, and
performs NO network call. ``provider test --live`` is the explicit,
user-invoked way to spend a request.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from ... import machine
from ..errors import ProviderConfigurationError
from ..models import (DataClassification, ModelTier, ProviderKind,
                      ProviderProfile, ProviderValidation)

_lock = threading.RLock()

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The official API hosts for the cloud kinds. Used when a profile leaves
#: ``endpoint`` empty; an explicit endpoint (self-hosted gateway, Azure
#: resource) overrides it and is validated instead. These are HOSTS the
#: transport pins to — model names are never defaulted anywhere.
OFFICIAL_HOSTS = {
    ProviderKind.OPENAI: "api.openai.com",
    ProviderKind.ANTHROPIC: "api.anthropic.com",
}


def providers_file():
    """Resolved lazily so tests that repoint OPENMIND_MACHINE_DIR work."""
    return machine.MACHINE_DIR / "providers.json"


def _load_raw() -> Dict[str, Any]:
    try:
        data = json.loads(providers_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        # An unreadable file is surfaced per-profile as a validation error by
        # list_profiles, not silently treated as empty on WRITE — see _store.
        return {}
    return data if isinstance(data, dict) else {}


def _store(data: Dict[str, Any]) -> None:
    """Atomic write: temp file in the same directory + os.replace, matching
    the sidecar convention in :mod:`openmind.machine`."""
    directory = providers_file().parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp = providers_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, providers_file())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def list_profiles() -> List[Dict[str, Any]]:
    """Every stored profile with its static validation attached. Invalid
    profiles are LISTED (with errors), never hidden — an operator must be able
    to see why a profile is unusable."""
    with _lock:
        raw = _load_raw()
    out: List[Dict[str, Any]] = []
    for name in sorted(raw):
        profile = ProviderProfile.from_dict(
            {**(raw[name] if isinstance(raw[name], dict) else {}),
             "name": name})
        validation = validate_profile(profile)
        record = profile.as_dict()
        record["validation"] = validation.as_dict()
        out.append(record)
    return out


def get_profile(name: str) -> Optional[ProviderProfile]:
    with _lock:
        raw = _load_raw()
    data = raw.get(str(name or "").strip())
    if not isinstance(data, dict):
        return None
    return ProviderProfile.from_dict({**data, "name": name})


def require_profile(name: str) -> ProviderProfile:
    profile = get_profile(name)
    if profile is None:
        raise ProviderConfigurationError(
            f"unknown provider profile: {name!r}",
            details={"profile": name,
                     "available": [p["name"] for p in list_profiles()]})
    return profile


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def upsert_profile(profile: ProviderProfile) -> Dict[str, Any]:
    """Create or replace a profile. The write itself requires only a valid
    NAME — an incomplete profile may be saved (it stays visible with its
    validation errors and cannot run), so configuration can be built up in
    steps without ever passing a secret through the CLI."""
    name = (profile.name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ProviderConfigurationError(
            "profile name must be a lowercase slug (letters, digits, hyphens)",
            details={"name": profile.name})
    profile.name = name
    env = (profile.api_key_env or "").strip()
    if env and not _ENV_RE.match(env):
        # Defense in depth against a pasted secret: anything that is not a
        # plausible environment-variable NAME (keys contain dashes/dots) is
        # refused BEFORE it can be written to disk. The error echoes nothing.
        raise ProviderConfigurationError(
            "api_key_env must be an environment-variable NAME (letters, "
            "digits, underscores). If you pasted a key value, unset it and "
            "export the key as an environment variable instead — OpenMind "
            "never stores key values.",
            details={"field": "api_key_env"})
    with _lock:
        data = _load_raw()
        record = profile.as_dict()
        record.pop("name", None)          # the key IS the name
        data[name] = record
        _store(data)
    stored = profile.as_dict()
    stored["validation"] = validate_profile(profile).as_dict()
    return stored


def remove_profile(name: str) -> bool:
    with _lock:
        data = _load_raw()
        if str(name) not in data:
            return False
        data.pop(str(name))
        _store(data)
    return True


# ---------------------------------------------------------------------------
# Static validation (no network)
# ---------------------------------------------------------------------------
def expected_host(profile: ProviderProfile) -> str:
    """The ONE host this profile's transport will allow. From the explicit
    endpoint when set, else the official API host for the kind. Empty means
    'nothing is reachable' (mock, or a misconfigured profile)."""
    endpoint = (profile.endpoint or "").strip()
    if endpoint:
        from urllib.parse import urlparse
        return (urlparse(endpoint).hostname or "").lower()
    return OFFICIAL_HOSTS.get(profile.kind, "")


def endpoint_issues(profile: ProviderProfile) -> List[str]:
    """Endpoint rules per kind: remote requires HTTPS (or nothing, for kinds
    with an official host); local requires an explicit loopback endpoint."""
    from urllib.parse import urlparse
    issues: List[str] = []
    endpoint = (profile.endpoint or "").strip()
    if profile.kind == ProviderKind.MOCK:
        return issues
    if profile.kind == ProviderKind.LOCAL_OPENAI:
        if not endpoint:
            issues.append("local-openai requires an endpoint, e.g. "
                          "http://127.0.0.1:7081/v1")
            return issues
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https"):
            issues.append(f"endpoint scheme must be http(s), got "
                          f"{parsed.scheme!r}")
        if host not in ("127.0.0.1", "localhost", "::1"):
            issues.append(f"local-openai endpoint must be loopback, got "
                          f"{host!r}")
        return issues
    # remote kinds
    if profile.kind == ProviderKind.AZURE_OPENAI and not endpoint:
        issues.append("azure-openai requires the resource endpoint, e.g. "
                      "https://<resource>.openai.azure.com")
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https":
            issues.append(f"remote endpoint must be https, got "
                          f"{parsed.scheme!r}")
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            issues.append("remote provider endpoint must not be loopback")
    return issues


def sdk_available(kind: str) -> Tuple[bool, str]:
    """Whether the SDK a kind needs is importable. Lazy and narrow: checking
    OpenAI must not import anthropic and vice versa."""
    try:
        if kind in (ProviderKind.OPENAI, ProviderKind.AZURE_OPENAI):
            import importlib.util
            ok = importlib.util.find_spec("openai") is not None
            return ok, "" if ok else "the 'openai' package is not installed"
        if kind == ProviderKind.ANTHROPIC:
            import importlib.util
            ok = importlib.util.find_spec("anthropic") is not None
            return ok, "" if ok else "the 'anthropic' package is not installed"
    except Exception as exc:  # pragma: no cover - importlib failure is exotic
        return False, f"SDK availability check failed: {exc}"
    return True, ""            # local-openai and mock need no SDK


def validate_profile(profile: ProviderProfile) -> ProviderValidation:
    """Static validation: configuration shape, endpoint policy, SDK presence,
    credential PRESENCE (never its value). No network call is made."""
    errors: List[str] = []
    warnings: List[str] = []

    if not _NAME_RE.match(profile.name or ""):
        errors.append("name must be a lowercase slug")
    if profile.kind not in ProviderKind.VALUES:
        errors.append(f"unknown kind {profile.kind!r} (supported: "
                      f"{', '.join(sorted(ProviderKind.VALUES))})")
        return ProviderValidation(ok=False, errors=errors, warnings=warnings)

    errors.extend(endpoint_issues(profile))

    if profile.max_data_classification not in DataClassification.VALUES:
        errors.append(f"unknown max_data_classification "
                      f"{profile.max_data_classification!r}")
    if profile.timeout <= 0:
        errors.append("timeout must be greater than zero")
    if profile.max_retries < 0:
        errors.append("max_retries must not be negative")

    unknown_tiers = sorted(set(profile.models) - ModelTier.VALUES)
    if unknown_tiers:
        errors.append(f"unknown model tiers: {', '.join(unknown_tiers)}")

    if profile.kind in ProviderKind.REMOTE:
        env = (profile.api_key_env or "").strip()
        if not env:
            errors.append("api_key_env is required for a remote provider "
                          "(the NAME of the environment variable holding the "
                          "key; the value itself is never stored)")
        elif not _ENV_RE.match(env):
            errors.append(f"api_key_env {env!r} is not a valid environment "
                          f"variable name")
        elif not os.environ.get(env):
            warnings.append(f"environment variable {env} is not set in this "
                            f"process; calls will fail until it is")
        if not any(str(profile.models.get(t) or "").strip()
                   for t in ModelTier.VALUES):
            errors.append("at least one model must be configured (fast/"
                          "standard/strong); OpenMind never defaults a cloud "
                          "model name")
        if not expected_host(profile):
            errors.append("no reachable host: set an https endpoint")
    elif profile.kind == ProviderKind.LOCAL_OPENAI:
        if not any(str(profile.models.get(t) or "").strip()
                   for t in ModelTier.VALUES):
            warnings.append("no model name configured; the literal 'local' "
                            "is sent (llama-server ignores it)")

    return ProviderValidation(ok=not errors, errors=errors, warnings=warnings)


def resolve_api_key(profile: ProviderProfile) -> Optional[str]:
    """The credential VALUE, read from the named environment variable at call
    time. Returns None when unset. Callers must never log or persist it."""
    env = (profile.api_key_env or "").strip()
    return os.environ.get(env) if env else None
