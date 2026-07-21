"""Anthropic semantic provider — official ``anthropic`` SDK, audited transport.

Verified against the installed SDK at implementation time:

* ``Anthropic(...)`` accepts ``http_client=`` — this adapter only ever passes
  the audited client pinned to ``api.anthropic.com`` (or the profile's
  explicit HTTPS endpoint). SDK-internal retries are disabled in favor of
  our bounded, counted retries.
* Structured output uses the CURRENT native mechanism:
  ``messages.create(..., output_config={"format": {"type": "json_schema",
  "schema": ...}})``. The result is still locally validated like every other
  provider's — native enforcement is an optimization, not a trust boundary.
* Usage comes from ``response.usage`` (``input_tokens`` / ``output_tokens`` /
  ``cache_read_input_tokens``); anything absent stays ``None``.
* No extended-thinking mode is requested and nothing resembling
  chain-of-thought is stored: the ONLY text this adapter keeps is the JSON
  payload matching the declared schema (hashed, parsed, then discarded).

Lazy import: a machine without the ``anthropic`` package loses exactly this
kind, nothing else.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..errors import (ProviderAuthenticationError, ProviderConfigurationError,
                      ProviderRateLimited, ProviderStructuredOutputError,
                      ProviderTimeout, ProviderUnavailable)
from ..models import (ProviderCapabilities, ProviderKind, ProviderProfile,
                      ProviderValidation, SemanticRequest, SemanticResponse,
                      StructuredSchema)
from ..transport import SemanticEgressContext, build_semantic_client
from . import base
from .profiles import resolve_api_key


def _import_sdk():
    try:
        import anthropic
    except ImportError as exc:
        raise ProviderConfigurationError(
            "the 'anthropic' package is not installed; install it to use "
            "Anthropic profiles (pip install anthropic)",
            details={"dependency": "anthropic"}) from exc
    return anthropic


def _translate_sdk_error(sdk, exc: BaseException, *,
                         profile: ProviderProfile) -> BaseException:
    if isinstance(exc, sdk.AuthenticationError):
        return ProviderAuthenticationError(
            f"Anthropic rejected the credential from {profile.api_key_env} "
            f"(http 401)", details={"profile": profile.name})
    if isinstance(exc, sdk.RateLimitError):
        return ProviderRateLimited("Anthropic rate limit (http 429)",
                                   details={"profile": profile.name})
    if isinstance(exc, sdk.APITimeoutError):
        return ProviderTimeout("Anthropic request timed out",
                               details={"profile": profile.name})
    if isinstance(exc, sdk.APIConnectionError):
        return ProviderUnavailable(
            f"Anthropic endpoint unreachable: {exc}",
            details={"profile": profile.name})
    if isinstance(exc, sdk.BadRequestError):
        return ProviderStructuredOutputError(
            f"Anthropic rejected the request (http 400): {exc}",
            details={"profile": profile.name})
    if isinstance(exc, sdk.APIStatusError):
        if exc.status_code >= 500:
            return ProviderUnavailable(
                f"Anthropic returned http {exc.status_code}",
                details={"profile": profile.name})
        return ProviderStructuredOutputError(
            f"Anthropic returned http {exc.status_code}",
            details={"profile": profile.name})
    return exc


class AnthropicProvider:
    kind = ProviderKind.ANTHROPIC

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True, json_schema=True, tool_schema=True,
            streaming=True, token_usage=True, cached_token_usage=True,
            custom_endpoint=bool(profile.endpoint), local=False, remote=True)

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        from . import profiles as registry
        return registry.validate_profile(profile)

    def generate_structured(self, request: SemanticRequest,
                            schema: StructuredSchema,
                            profile: ProviderProfile,
                            transport: Optional[Any] = None,
                            **_: Any) -> SemanticResponse:
        sdk = _import_sdk()
        model = profile.model_for_tier(request.model_tier)
        if not model:
            raise ProviderConfigurationError(
                f"profile {profile.name!r} has no model configured for tier "
                f"{request.model_tier!r}", details={"profile": profile.name})
        if resolve_api_key(profile) is None:
            raise ProviderAuthenticationError(
                f"environment variable {profile.api_key_env} is not set",
                details={"profile": profile.name,
                         "api_key_env": profile.api_key_env})

        context = SemanticEgressContext(
            workspace_id=request.workspace_id, profile=profile.name,
            provider_kind=self.kind, classification=request.classification)
        context.stamp(task=request.task_type,
                      request_hash=base.canonical_request_hash(request))
        http_client = build_semantic_client(profile, context, inner=transport)

        client_kwargs: dict = {
            "api_key": resolve_api_key(profile),
            "http_client": http_client,
            "max_retries": 0,
            "timeout": profile.timeout,
        }
        if profile.endpoint:
            client_kwargs["base_url"] = profile.endpoint
        client = sdk.Anthropic(**client_kwargs)

        started = time.monotonic()

        def _call():
            try:
                return client.messages.with_raw_response.create(
                    model=model,
                    system=request.system_instructions,
                    messages=[{"role": "user", "content": json.dumps(
                        request.input_packet, ensure_ascii=False,
                        sort_keys=True)}],
                    max_tokens=int(request.max_output_tokens),
                    output_config={"format": {"type": "json_schema",
                                              "schema": schema.json_schema}},
                    timeout=request.timeout)
            except BaseException as exc:
                raise _translate_sdk_error(sdk, exc, profile=profile)

        def _classify(exc: BaseException) -> Optional[float]:
            if isinstance(exc, ProviderRateLimited):
                return 1.0
            if isinstance(exc, ProviderUnavailable):
                return 0.5
            return None

        try:
            raw, retries = base.run_with_retries(
                _call, max_retries=profile.max_retries, classify=_classify)
        finally:
            http_client.close()

        provider_request_id = str(raw.headers.get("request-id") or "")
        message = raw.parse()
        raw_text = "".join(
            getattr(block, "text", "") or ""
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "type", "") == "text")
        if not raw_text.strip():
            raise ProviderStructuredOutputError(
                "Anthropic returned no text content for the structured "
                "request", details={"profile": profile.name,
                                    "stop_reason": str(
                                        getattr(message, "stop_reason", ""))})

        usage = getattr(message, "usage", None)
        structured = base.parse_structured_text(
            raw_text, provider_kind=self.kind, request_id=request.request_id)
        return base.build_response(
            request, provider_kind=self.kind, model=model, raw_text=raw_text,
            structured_output=structured,
            input_tokens=base.coerce_optional_int(
                getattr(usage, "input_tokens", None)),
            output_tokens=base.coerce_optional_int(
                getattr(usage, "output_tokens", None)),
            cached_tokens=base.coerce_optional_int(
                getattr(usage, "cache_read_input_tokens", None)),
            latency_ms=base.elapsed_ms(started),
            finish_reason=str(getattr(message, "stop_reason", "") or ""),
            provider_request_id=provider_request_id, retry_count=retries)


__all__ = ["AnthropicProvider"]
