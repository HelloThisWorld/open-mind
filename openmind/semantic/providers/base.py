"""Provider SPI: the protocol every adapter implements, plus shared helpers.

The SPI is deliberately tiny — capabilities, static validation, one bounded
structured-output call — because everything interesting (policy, budgets,
caching, verification) happens OUTSIDE the provider, where it is enforced
identically for every kind. An adapter's whole job is: build the request its
API wants, through the audited transport, and translate the outcome into
either a :class:`~openmind.semantic.models.SemanticResponse` or a typed
error. Adapters never log content, never see the credential longer than the
call, and never construct their own HTTP client.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from ..errors import ProviderRateLimited, ProviderStructuredOutputError
from ..models import (ProviderCapabilities, ProviderProfile,
                      ProviderValidation, SemanticRequest, SemanticResponse,
                      StructuredSchema)


class SemanticProvider(Protocol):
    """What the runner requires of every provider adapter."""

    kind: str

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        ...

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        ...

    def generate_structured(self, request: SemanticRequest,
                            schema: StructuredSchema,
                            profile: ProviderProfile,
                            **kwargs: Any) -> SemanticResponse:
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def canonical_request_hash(request: SemanticRequest) -> str:
    """A stable identity for one request: what was asked, of which schema,
    over which packet. Used for audit correlation and the usage ledger —
    NEVER contains content itself, only its hash."""
    payload = json.dumps({
        "task": request.task_type,
        "schema": [request.schema_name, request.schema_version],
        "prompt_version": request.prompt_version,
        "packet": request.input_packet,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def parse_structured_text(raw_text: str, *, provider_kind: str,
                          request_id: str) -> Dict[str, Any]:
    """Parse the provider's raw text as a single JSON object.

    Tolerates the one deviation local models actually produce — a Markdown
    code fence around the JSON — and nothing else. Anything unparseable is a
    typed :class:`ProviderStructuredOutputError` carrying a bounded PREFIX
    length, never the content itself.
    """
    text = (raw_text or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1 and text.rstrip().endswith("```"):
            text = text[first_newline + 1:text.rstrip().rfind("```")].strip()
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ProviderStructuredOutputError(
            f"provider returned unparseable structured output: {exc}",
            details={"provider_kind": provider_kind, "request_id": request_id,
                     "response_chars": len(raw_text or "")}) from exc
    if not isinstance(parsed, dict):
        raise ProviderStructuredOutputError(
            "provider returned JSON that is not an object",
            details={"provider_kind": provider_kind, "request_id": request_id,
                     "json_type": type(parsed).__name__})
    return parsed


def run_with_retries(call: Callable[[], Any], *, max_retries: int,
                     classify: Callable[[BaseException], Optional[float]],
                     sleep: Callable[[float], None] = time.sleep
                     ) -> Tuple[Any, int]:
    """Run *call*, retrying ONLY failures *classify* deems retryable.

    ``classify`` returns a suggested delay in seconds for a retryable error
    (a rate limit's Retry-After, a transient 5xx) or ``None`` for a permanent
    one. Retries are bounded by the profile's ``max_retries``; delays are
    capped at 8s so a hostile Retry-After cannot park the worker. Returns
    ``(result, retry_count)``.
    """
    attempt = 0
    while True:
        try:
            return call(), attempt
        except BaseException as exc:
            delay = classify(exc)
            if delay is None or attempt >= max(0, int(max_retries)):
                if isinstance(exc, ProviderRateLimited):
                    exc.details.setdefault("retry_count", attempt)
                raise
            attempt += 1
            sleep(min(max(delay, 0.2), 8.0))


def elapsed_ms(started_monotonic: float) -> int:
    return int((time.monotonic() - started_monotonic) * 1000)


def build_response(request: SemanticRequest, *, provider_kind: str,
                   model: str, raw_text: str,
                   structured_output: Dict[str, Any],
                   input_tokens: Optional[int], output_tokens: Optional[int],
                   cached_tokens: Optional[int], latency_ms: Optional[int],
                   finish_reason: str, provider_request_id: str,
                   retry_count: int,
                   warnings: Optional[list] = None) -> SemanticResponse:
    """Assemble the SPI response. Usage numbers pass through as-is: an
    adapter hands in ``None`` for anything its API did not report."""
    return SemanticResponse(
        request_id=request.request_id,
        provider_kind=provider_kind,
        model=model,
        structured_output=structured_output,
        raw_response_hash=hash_text(raw_text),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        finish_reason=finish_reason or "",
        provider_request_id=provider_request_id or "",
        retry_count=retry_count,
        warnings=list(warnings or []))


def coerce_optional_int(value: Any) -> Optional[int]:
    """int when the provider reported a number, None otherwise — the ledger's
    'unknown is not zero' rule starts here."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["SemanticProvider", "canonical_request_hash", "hash_text",
           "parse_structured_text", "run_with_retries", "elapsed_ms",
           "build_response", "coerce_optional_int"]
