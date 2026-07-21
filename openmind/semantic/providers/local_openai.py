"""Local OpenAI-compatible semantic provider (loopback llama-server et al.).

This is a SEPARATE profile from the legacy interactive Ask client: it may
point at a different port and a different model, and nothing about
``llm_client.py`` or the managed model server changes. What they share is the
invariant — loopback only, enforced twice (profile validation refuses a
non-loopback endpoint; the audited transport refuses the request).

HONEST DEGRADATION
------------------
llama-server's OpenAI-compatible surface accepts ``response_format`` of type
``json_object`` (and grammar-constrained variants), but there is no reliable,
version-independent guarantee of NATIVE JSON-Schema enforcement across local
servers. This adapter therefore requests JSON-object output, embeds the
schema in the instructions, and relies on the SAME local validation every
remote provider's output goes through anyway. ``capabilities`` reports
``json_schema=False`` so the planner and tests can see the difference — it
does not pretend.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from ... import netguard
from ..errors import (ProviderStructuredOutputError, ProviderTimeout,
                      ProviderUnavailable)
from ..models import (ProviderCapabilities, ProviderKind, ProviderProfile,
                      ProviderValidation, SemanticRequest, SemanticResponse,
                      StructuredSchema)
from . import base
from .profiles import expected_host


class LocalOpenAIProvider:
    kind = ProviderKind.LOCAL_OPENAI

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True, json_schema=False, tool_schema=False,
            streaming=False, token_usage=True, cached_token_usage=False,
            custom_endpoint=True, local=True, remote=False)

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        from . import profiles as registry
        return registry.validate_profile(profile)

    def generate_structured(self, request: SemanticRequest,
                            schema: StructuredSchema,
                            profile: ProviderProfile,
                            transport: Optional[Any] = None,
                            **_: Any) -> SemanticResponse:
        model = profile.model_for_tier(request.model_tier) or "local"
        url = profile.endpoint.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content":
                    request.system_instructions
                    + "\n\nReturn ONLY a single JSON object conforming to "
                      "this JSON Schema (no prose, no markdown):\n"
                    + json.dumps(schema.json_schema, sort_keys=True)},
                {"role": "user", "content":
                    json.dumps(request.input_packet, ensure_ascii=False,
                               sort_keys=True)},
            ],
            "temperature": 0.0,
            "max_tokens": int(request.max_output_tokens),
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        started = time.monotonic()
        request_hash = base.canonical_request_hash(request)

        def _call() -> httpx.Response:
            try:
                resp = netguard.guarded_semantic_request(
                    "POST", url, allowed_host=expected_host(profile),
                    remote=False, timeout=request.timeout,
                    workspace_id=request.workspace_id, profile=profile.name,
                    provider_kind=self.kind, task=request.task_type,
                    classification=request.classification,
                    request_hash=request_hash, json=payload,
                    **({"transport": transport} if transport else {}))
            except netguard.SemanticEgressBlocked:
                raise
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(
                    f"local provider timed out after {request.timeout:g}s",
                    details={"profile": profile.name}) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(
                    f"local provider unreachable: {exc}",
                    details={"profile": profile.name,
                             "endpoint": profile.endpoint}) from exc
            if resp.status_code == 429:
                from ..errors import ProviderRateLimited
                raise ProviderRateLimited("local provider rate limited")
            if resp.status_code >= 500:
                raise ProviderUnavailable(
                    f"local provider returned http {resp.status_code}")
            if resp.status_code >= 400:
                raise ProviderStructuredOutputError(
                    f"local provider rejected the request "
                    f"(http {resp.status_code})")
            return resp

        def _classify(exc: BaseException) -> Optional[float]:
            from ..errors import ProviderRateLimited
            if isinstance(exc, (ProviderRateLimited, ProviderUnavailable)):
                return 0.5
            return None

        resp, retries = base.run_with_retries(
            _call, max_retries=profile.max_retries, classify=_classify)

        try:
            data = resp.json()
            raw_text = str(data["choices"][0]["message"]["content"] or "")
            finish = str(data["choices"][0].get("finish_reason") or "")
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderStructuredOutputError(
                f"local provider returned an unexpected response shape: {exc}",
                details={"profile": profile.name}) from exc

        structured = base.parse_structured_text(
            raw_text, provider_kind=self.kind, request_id=request.request_id)
        return base.build_response(
            request, provider_kind=self.kind, model=model, raw_text=raw_text,
            structured_output=structured,
            input_tokens=base.coerce_optional_int(usage.get("prompt_tokens")),
            output_tokens=base.coerce_optional_int(
                usage.get("completion_tokens")),
            cached_tokens=None,
            latency_ms=base.elapsed_ms(started), finish_reason=finish,
            provider_request_id=str(data.get("id") or ""),
            retry_count=retries)


__all__ = ["LocalOpenAIProvider"]
