"""Typed errors for the semantic plane.

Every provider/policy/budget failure is a distinct class, because the runner
has to make DIFFERENT decisions for them: a rate limit is retried with
backoff, a timeout fails one target and moves on, an authentication failure
aborts the run before another byte is sent, a budget stop is an orderly
``partial``. Mapping them all to ``RuntimeError`` would force string matching
at every one of those decision points.

All of them extend :class:`openmind.domain.errors.OpenMindError`, so the CLI
and REST adapters translate them exactly like every other application error —
machine-readable ``code``, honest ``http_status``, stable ``exit_code``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..domain.errors import OpenMindError


class SemanticError(OpenMindError):
    """Base class for every semantic-plane error."""

    code = "semantic_error"
    exit_code = 4
    http_status = 500


class ProviderConfigurationError(SemanticError):
    """The provider profile is unusable (bad endpoint, missing model names,
    unknown kind, missing SDK for the kind). Fix the configuration; retrying
    the same call cannot succeed."""

    code = "provider_configuration_error"
    exit_code = 2
    http_status = 400


class ProviderAuthenticationError(SemanticError):
    """The credential is missing or was rejected by the provider. The key
    VALUE is never included in this error — only the environment-variable
    name the profile points at."""

    code = "provider_authentication_error"
    exit_code = 2
    http_status = 401


class ProviderPolicyBlocked(SemanticError):
    """The workspace semantic policy forbids this call (remote disabled,
    classification above the profile's allowance, task disabled). Raised
    BEFORE any project content is serialized for transport."""

    code = "provider_policy_blocked"
    exit_code = 1
    http_status = 403


class ProviderRateLimited(SemanticError):
    """The provider returned a rate-limit response after bounded retries."""

    code = "provider_rate_limited"
    exit_code = 4
    http_status = 429

    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if retry_after is not None:
            merged.setdefault("retry_after_seconds", retry_after)
        super().__init__(message, details=merged)
        self.retry_after = retry_after


class ProviderTimeout(SemanticError):
    """The provider did not answer within the profile's timeout."""

    code = "provider_timeout"
    exit_code = 5
    http_status = 504


class ProviderUnavailable(SemanticError):
    """The provider endpoint is unreachable or returned a 5xx."""

    code = "provider_unavailable"
    exit_code = 3
    http_status = 503


class ProviderStructuredOutputError(SemanticError):
    """The provider answered, but not with parseable JSON for the declared
    schema (truncated stream, refusal text, malformed JSON)."""

    code = "provider_structured_output_error"
    exit_code = 4
    http_status = 502


class ProviderResponseValidationError(SemanticError):
    """The provider returned well-formed JSON that fails the local schema
    validation (unknown fields, unknown candidate types, empty statements).
    The output is rejected — it never reaches the candidate store."""

    code = "provider_response_validation_error"
    exit_code = 4
    http_status = 502


class SemanticBudgetExceeded(SemanticError):
    """A configured budget stops the run from creating more provider
    requests. Completed work is preserved; the run reports ``partial`` with
    ``budget_exhausted`` — never a silent success."""

    code = "budget_exhausted"
    exit_code = 4
    http_status = 409

    def __init__(self, message: str, *, budget: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        merged = dict(details or {})
        if budget:
            merged.setdefault("budget", budget)
        super().__init__(message, details=merged)
        self.budget = budget


class LensNotFound(SemanticError):
    """The named Project Lens does not exist in this workspace."""

    code = "lens_not_found"
    exit_code = 1
    http_status = 404


class LensInvalid(SemanticError):
    """A lens definition failed schema or safe-pattern validation, or an
    operation is illegal for its status (approving an invalid lens,
    activating an unapproved induced lens)."""

    code = "lens_invalid"
    exit_code = 2
    http_status = 400


class CandidateNotFound(SemanticError):
    """The referenced semantic candidate does not exist in this workspace."""

    code = "candidate_not_found"
    exit_code = 1
    http_status = 404


class AnalysisRunNotFound(SemanticError):
    """The referenced analysis run does not exist in this workspace."""

    code = "analysis_run_not_found"
    exit_code = 1
    http_status = 404


__all__ = [
    "SemanticError",
    "ProviderConfigurationError", "ProviderAuthenticationError",
    "ProviderPolicyBlocked", "ProviderRateLimited", "ProviderTimeout",
    "ProviderUnavailable", "ProviderStructuredOutputError",
    "ProviderResponseValidationError", "SemanticBudgetExceeded",
    "LensNotFound", "LensInvalid", "CandidateNotFound", "AnalysisRunNotFound",
]
