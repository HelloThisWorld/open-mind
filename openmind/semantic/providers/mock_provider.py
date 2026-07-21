"""Deterministic mock provider — the test double for the whole plane.

Reads canned structured outputs, records every request it receives, and can
be scripted to fail in each way a real provider fails. It performs NO network
access of any kind, which is what lets the analysis/cache/budget/staleness
suites and CI exercise the full pipeline with zero credentials and zero
egress.

CONFIGURATION (all via ``profile.metadata`` — plain data, no code):

``responses``      task_type -> structured output dict to return.
``fixtures_dir``   directory of ``<task_type>.json`` files consulted when
                   ``responses`` has no entry for the task.
``fail``           ``{"kind": ..., "times": N}`` — the first N calls raise:
                   ``rate-limit`` / ``timeout`` / ``unavailable`` / ``auth`` /
                   ``malformed`` (returns unparseable text instead of JSON).
``latency_ms``     fixed latency to report (default 1).

Requests are recorded in the module-level :data:`RECORDED_REQUESTS` (bounded)
so a test can assert exactly what would have been sent — including that
document text only ever appears under ``untrustedContent``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import (ProviderAuthenticationError, ProviderRateLimited,
                      ProviderStructuredOutputError, ProviderTimeout,
                      ProviderUnavailable)
from ..models import (ProviderCapabilities, ProviderKind, ProviderProfile,
                      ProviderValidation, SemanticRequest, SemanticResponse,
                      StructuredSchema)
from . import base

#: Every request the mock has served this process, newest last. Bounded so a
#: long dev session cannot grow it without limit.
RECORDED_REQUESTS: List[Dict[str, Any]] = []
_MAX_RECORDED = 500
_lock = threading.Lock()
#: Remaining scripted failures per profile name (consumed per call).
_fail_budget: Dict[str, int] = {}


def reset_recorder() -> None:
    with _lock:
        RECORDED_REQUESTS.clear()
        _fail_budget.clear()


class MockProvider:
    kind = ProviderKind.MOCK

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True, json_schema=True, tool_schema=False,
            streaming=False, token_usage=True, cached_token_usage=False,
            custom_endpoint=False, local=True, remote=False)

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        from . import profiles as registry
        return registry.validate_profile(profile)

    def generate_structured(self, request: SemanticRequest,
                            schema: StructuredSchema,
                            profile: ProviderProfile,
                            **_: Any) -> SemanticResponse:
        meta = profile.metadata or {}
        with _lock:
            RECORDED_REQUESTS.append({
                "profile": profile.name,
                "task_type": request.task_type,
                "model_tier": request.model_tier,
                "schema_name": schema.name,
                "schema_version": schema.version,
                "system_instructions": request.system_instructions,
                "input_packet": request.input_packet,
                "idempotency_key": request.idempotency_key,
            })
            del RECORDED_REQUESTS[:-_MAX_RECORDED]
            remaining = _fail_budget.get(profile.name)
            if remaining is None:
                remaining = int((meta.get("fail") or {}).get("times") or 0)
            should_fail = remaining > 0
            _fail_budget[profile.name] = max(0, remaining - 1)

        if should_fail:
            self._raise_scripted(str((meta.get("fail") or {}).get("kind") or ""),
                                 request)

        output = self._resolve_output(meta, request.task_type)
        raw_text = json.dumps(output, sort_keys=True)
        structured = base.parse_structured_text(
            raw_text, provider_kind=self.kind, request_id=request.request_id)
        return base.build_response(
            request, provider_kind=self.kind,
            model=profile.model_for_tier(request.model_tier) or "mock-model",
            raw_text=raw_text, structured_output=structured,
            input_tokens=len(json.dumps(request.input_packet)) // 4,
            output_tokens=len(raw_text) // 4,
            cached_tokens=None,
            latency_ms=int(meta.get("latency_ms") or 1),
            finish_reason="stop",
            provider_request_id=f"mock-{len(RECORDED_REQUESTS)}",
            retry_count=0)

    # -- internals ----------------------------------------------------------
    def _raise_scripted(self, kind: str, request: SemanticRequest) -> None:
        if kind == "rate-limit":
            raise ProviderRateLimited("mock: scripted rate limit",
                                      retry_after=0.0)
        if kind == "timeout":
            raise ProviderTimeout("mock: scripted timeout")
        if kind == "unavailable":
            raise ProviderUnavailable("mock: scripted unavailability")
        if kind == "auth":
            raise ProviderAuthenticationError("mock: scripted auth failure")
        if kind == "malformed":
            base.parse_structured_text(
                "this is not JSON {", provider_kind=self.kind,
                request_id=request.request_id)
        raise ProviderUnavailable(f"mock: unknown scripted failure {kind!r}")

    @staticmethod
    def _resolve_output(meta: Dict[str, Any], task_type: str) -> Dict[str, Any]:
        responses = meta.get("responses") or {}
        if task_type in responses:
            return dict(responses[task_type])
        fixtures_dir = str(meta.get("fixtures_dir") or "")
        if fixtures_dir:
            path = Path(fixtures_dir) / f"{task_type}.json"
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        raise ProviderStructuredOutputError(
            f"mock provider has no scripted response for task {task_type!r}",
            details={"task_type": task_type,
                     "hint": "set profile.metadata.responses[task] or "
                             "metadata.fixtures_dir"})


__all__ = ["MockProvider", "RECORDED_REQUESTS", "reset_recorder"]
