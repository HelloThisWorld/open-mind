"""The workspace policy gate: who may talk to which provider about what.

Two authorities meet here and BOTH must consent before a single byte of
project content is serialized for a provider:

* the WORKSPACE policy row (classification, allow_remote, chosen profile,
  budgets) — fail-closed defaults: ``restricted`` + remote off;
* the PROVIDER profile (enabled, max_data_classification, credential
  presence) — machine-local, secret-free.

:func:`authorize` is the single choke point the planner and the runner call.
It returns an auditable decision record on success and raises
:class:`~openmind.semantic.errors.ProviderPolicyBlocked` (or a configuration/
authentication error) on refusal — BEFORE any content packet exists. There is
deliberately no override parameter: a job payload cannot smuggle a policy
bypass because the gate re-reads the stored policy itself.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import store
from .errors import (ProviderAuthenticationError, ProviderConfigurationError,
                     ProviderPolicyBlocked)
from .models import DataClassification, ProviderKind, ProviderProfile
from .providers import profiles as profile_registry

#: The closed set of budget keys a policy may carry (spec §23). Unknown keys
#: are rejected at set time so a typo cannot silently disable a cap.
BUDGET_KEYS = frozenset({
    "max_input_tokens_per_request",
    "max_output_tokens_per_request",
    "max_requests_per_run",
    "max_total_input_tokens_per_run",
    "max_total_output_tokens_per_run",
    "max_strong_model_requests_per_run",
    "max_estimated_cost_per_run",
    "max_daily_requests",
    "max_daily_input_tokens",
    "max_daily_output_tokens",
})

#: Conservative defaults applied when a budget key is unset. They bound any
#: single run without blocking local experimentation; remote use is already
#: impossible until a human flips allow_remote, so these are a second fence,
#: not the first.
DEFAULT_BUDGETS: Dict[str, Any] = {
    # Per-request caps sized to admit the LARGEST registered task (lens
    # induction: 24k in / 6k out) — a default that silently blocked a
    # registered task would read as a bug, not a budget.
    "max_input_tokens_per_request": 32_000,
    "max_output_tokens_per_request": 8_000,
    "max_requests_per_run": 200,
    "max_total_input_tokens_per_run": 1_000_000,
    "max_total_output_tokens_per_run": 200_000,
    "max_strong_model_requests_per_run": 10,
    "max_estimated_cost_per_run": None,     # unlimited only when cost is unknowable
    "max_daily_requests": 1_000,
    "max_daily_input_tokens": 5_000_000,
    "max_daily_output_tokens": 1_000_000,
}


def effective_budgets(policy: Dict[str, Any],
                      overrides: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
    """defaults <- stored policy <- per-run overrides. Overrides may only
    TIGHTEN or set unset keys; loosening beyond the stored policy is refused,
    so a job payload can never expand what the workspace allowed."""
    merged = dict(DEFAULT_BUDGETS)
    stored = {k: v for k, v in (policy.get("budgets") or {}).items()
              if k in BUDGET_KEYS}
    merged.update(stored)
    for key, value in (overrides or {}).items():
        if key not in BUDGET_KEYS:
            raise ProviderConfigurationError(
                f"unknown budget key: {key!r}",
                details={"allowed": sorted(BUDGET_KEYS)})
        current = merged.get(key)
        if value is None:
            continue
        if current is None or float(value) <= float(current):
            merged[key] = value
        else:
            raise ProviderPolicyBlocked(
                f"budget override for {key} ({value}) exceeds the workspace "
                f"policy ({current}); overrides may only tighten budgets",
                details={"budget": key, "policy": current, "override": value})
    return merged


def validate_budgets(budgets: Dict[str, Any]) -> Dict[str, Any]:
    """Reject unknown keys and non-numeric values at policy-set time."""
    clean: Dict[str, Any] = {}
    for key, value in (budgets or {}).items():
        if key not in BUDGET_KEYS:
            raise ProviderConfigurationError(
                f"unknown budget key: {key!r}",
                details={"allowed": sorted(BUDGET_KEYS)})
        if value is None:
            clean[key] = None
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ProviderConfigurationError(
                f"budget {key} must be a number or null, got {value!r}")
        if number < 0:
            raise ProviderConfigurationError(
                f"budget {key} must not be negative")
        clean[key] = int(number) if float(number).is_integer() else number
    return clean


def authorize(workspace_id: str, *, task_type: str,
              profile_name: Optional[str] = None,
              policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The single pre-content policy gate.

    Resolves the workspace policy and the provider profile, applies every
    rule from the spec in order, and returns
    ``{policy, profile, decision: {...}}``. Raises a typed error on the FIRST
    failed rule — always before any project content has been touched.
    """
    policy = policy or store.get_policy(workspace_id)
    name = (profile_name or policy.get("provider_profile") or "").strip()

    if not name:
        raise ProviderPolicyBlocked(
            "no provider profile selected: set one with "
            "'openmind semantic policy set --provider <name>' or pass "
            "--provider",
            details={"workspace_id": workspace_id})

    profile = profile_registry.require_profile(name)

    if not profile.enabled:
        raise ProviderPolicyBlocked(
            f"provider profile {name!r} is disabled",
            details={"profile": name})

    validation = profile_registry.validate_profile(profile)
    if not validation.ok:
        raise ProviderConfigurationError(
            f"provider profile {name!r} is invalid: "
            + "; ".join(validation.errors),
            details={"profile": name, "errors": validation.errors})

    classification = DataClassification.coerce(
        policy.get("data_classification"))

    if profile.is_remote:
        if not policy.get("allow_remote"):
            raise ProviderPolicyBlocked(
                "this workspace does not allow remote semantic analysis "
                "(allow_remote is false; enable it explicitly with "
                "'openmind semantic policy set --allow-remote')",
                details={"workspace_id": workspace_id, "profile": name})
        if not DataClassification.allows(profile.max_data_classification,
                                         classification):
            raise ProviderPolicyBlocked(
                f"workspace classification {classification!r} exceeds what "
                f"profile {name!r} accepts "
                f"({profile.max_data_classification!r})",
                details={"workspace_id": workspace_id, "profile": name,
                         "classification": classification,
                         "profile_max": profile.max_data_classification})
        if profile_registry.resolve_api_key(profile) is None:
            raise ProviderAuthenticationError(
                f"environment variable {profile.api_key_env} is not set; "
                f"the credential is read at call time and is never stored",
                details={"profile": name,
                         "api_key_env": profile.api_key_env})

    return {
        "policy": policy,
        "profile": profile,
        "decision": {
            "workspace_id": workspace_id,
            "task_type": task_type,
            "profile": name,
            "provider_kind": profile.kind,
            "remote": profile.is_remote,
            "classification": classification,
            "allowed": True,
        },
    }


def local_only_authorize(profile: ProviderProfile) -> None:
    """Sanity gate for paths that must stay local regardless of policy —
    raises if a caller wired a remote profile into a local-only path."""
    if profile.kind not in ProviderKind.LOCAL:
        raise ProviderPolicyBlocked(
            f"this operation is local-only; profile kind {profile.kind!r} "
            f"is remote")


__all__ = ["BUDGET_KEYS", "DEFAULT_BUDGETS", "effective_budgets",
           "validate_budgets", "authorize", "local_only_authorize"]
