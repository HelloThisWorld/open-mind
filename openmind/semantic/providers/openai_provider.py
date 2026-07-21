"""OpenAI semantic provider — official ``openai`` SDK, audited transport.

Verified against the installed SDK (2.x) at implementation time rather than
copied from an old example:

* ``OpenAI(...)`` accepts ``http_client=`` — the ONLY client this adapter
  passes is one built by :func:`openmind.semantic.transport.build_semantic_client`,
  so every request (including SDK-internal retries, which are disabled here
  in favor of our own bounded, counted retries) crosses the audited
  transport pinned to ``api.openai.com`` (or the profile's explicit HTTPS
  endpoint).
* Structured output uses the native strict JSON-Schema response format
  (``response_format={"type": "json_schema", ...}``) on Chat Completions.
* ``max_completion_tokens`` bounds the output; token usage is read from
  ``response.usage`` and cached input tokens from
  ``usage.prompt_tokens_details.cached_tokens`` WHEN present — absent values
  stay ``None``.
* The provider request id is taken from the raw-response headers
  (``x-request-id``) via ``with_raw_response``, where available.

The SDK import is lazy: a machine without the ``openai`` package loses
exactly this kind (and Azure), nothing else.
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
        import openai
    except ImportError as exc:
        raise ProviderConfigurationError(
            "the 'openai' package is not installed; install it to use "
            "OpenAI or Azure OpenAI profiles (pip install openai)",
            details={"dependency": "openai"}) from exc
    return openai


def _classify_sdk_error(openai_mod, exc: BaseException) -> Optional[float]:
    """Retry-delay for retryable SDK errors, None for permanent ones."""
    if isinstance(exc, openai_mod.RateLimitError):
        return 1.0
    if isinstance(exc, (openai_mod.APIConnectionError,
                        openai_mod.InternalServerError)):
        return 0.5
    return None


def _translate_sdk_error(openai_mod, exc: BaseException, *,
                         profile: ProviderProfile) -> BaseException:
    """Map SDK exceptions onto the typed taxonomy. The original exception is
    chained for --verbose diagnostics; messages never include content."""
    if isinstance(exc, openai_mod.AuthenticationError):
        return ProviderAuthenticationError(
            f"OpenAI rejected the credential from {profile.api_key_env} "
            f"(http 401)", details={"profile": profile.name})
    if isinstance(exc, openai_mod.RateLimitError):
        return ProviderRateLimited("OpenAI rate limit (http 429)",
                                   details={"profile": profile.name})
    if isinstance(exc, openai_mod.APITimeoutError):
        return ProviderTimeout(
            f"OpenAI request timed out", details={"profile": profile.name})
    if isinstance(exc, openai_mod.APIConnectionError):
        return ProviderUnavailable(
            f"OpenAI endpoint unreachable: {exc}",
            details={"profile": profile.name})
    if isinstance(exc, openai_mod.BadRequestError):
        return ProviderStructuredOutputError(
            f"OpenAI rejected the request (http 400): {exc}",
            details={"profile": profile.name})
    if isinstance(exc, openai_mod.APIStatusError):
        if exc.status_code >= 500:
            return ProviderUnavailable(
                f"OpenAI returned http {exc.status_code}",
                details={"profile": profile.name})
        return ProviderStructuredOutputError(
            f"OpenAI returned http {exc.status_code}",
            details={"profile": profile.name})
    return exc


class OpenAIProvider:
    kind = ProviderKind.OPENAI

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True, json_schema=True, tool_schema=True,
            streaming=True, token_usage=True, cached_token_usage=True,
            custom_endpoint=bool(profile.endpoint), local=False, remote=True)

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        from . import profiles as registry
        return registry.validate_profile(profile)

    # -- SDK client construction (overridden by the Azure subclass) ---------
    def _build_sdk_client(self, openai_mod, profile: ProviderProfile,
                          http_client) -> Any:
        kwargs: dict = {
            "api_key": resolve_api_key(profile),
            "http_client": http_client,
            "max_retries": 0,          # our retries are bounded and counted
            "timeout": profile.timeout,
        }
        if profile.endpoint:
            kwargs["base_url"] = profile.endpoint
        return openai_mod.OpenAI(**kwargs)

    def generate_structured(self, request: SemanticRequest,
                            schema: StructuredSchema,
                            profile: ProviderProfile,
                            transport: Optional[Any] = None,
                            **_: Any) -> SemanticResponse:
        openai_mod = _import_sdk()
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
        client = self._build_sdk_client(openai_mod, profile, http_client)

        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.name, "strict": bool(schema.strict),
                            "schema": schema.json_schema},
        }
        messages = [
            {"role": "system", "content": request.system_instructions},
            {"role": "user", "content": json.dumps(
                request.input_packet, ensure_ascii=False, sort_keys=True)},
        ]
        started = time.monotonic()

        def _call():
            try:
                return client.chat.completions.with_raw_response.create(
                    model=model, messages=messages,
                    max_completion_tokens=int(request.max_output_tokens),
                    response_format=response_format,
                    timeout=request.timeout)
            except BaseException as exc:
                raise _translate_sdk_error(openai_mod, exc, profile=profile)

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

        provider_request_id = str(raw.headers.get("x-request-id") or "")
        completion = raw.parse()
        try:
            choice = completion.choices[0]
            raw_text = str(choice.message.content or "")
            finish = str(choice.finish_reason or "")
        except (AttributeError, IndexError) as exc:
            raise ProviderStructuredOutputError(
                "OpenAI returned no completion choice",
                details={"profile": profile.name}) from exc
        if getattr(choice.message, "refusal", None):
            raise ProviderStructuredOutputError(
                "OpenAI declined to produce the structured output",
                details={"profile": profile.name, "finish_reason": finish})

        usage = getattr(completion, "usage", None)
        cached = None
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = base.coerce_optional_int(
                getattr(details, "cached_tokens", None))
        structured = base.parse_structured_text(
            raw_text, provider_kind=self.kind, request_id=request.request_id)
        return base.build_response(
            request, provider_kind=self.kind, model=model, raw_text=raw_text,
            structured_output=structured,
            input_tokens=base.coerce_optional_int(
                getattr(usage, "prompt_tokens", None)),
            output_tokens=base.coerce_optional_int(
                getattr(usage, "completion_tokens", None)),
            cached_tokens=cached,
            latency_ms=base.elapsed_ms(started), finish_reason=finish,
            provider_request_id=provider_request_id, retry_count=retries)


__all__ = ["OpenAIProvider"]
