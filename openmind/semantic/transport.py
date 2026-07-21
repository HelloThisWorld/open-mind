"""Audited HTTP transport for semantic provider calls.

Every byte a provider SDK sends leaves through :class:`AuditedSemanticTransport`
— an ``httpx.BaseTransport`` that validates EVERY request hop against the
profile's single allowed host (via :func:`openmind.netguard.assert_semantic_host`)
and writes one structured audit record per request to
``config.SEMANTIC_AUDIT_LOG``. Because validation happens inside the
transport, nothing above it — SDK retries, redirect following someone turns
on, a crafted base_url — can produce an unaudited or off-host request.

WHAT IS LOGGED AND WHAT NEVER IS
--------------------------------
Logged: timestamp, workspace, profile, provider kind, task, host, method,
path, allowed/blocked, request byte count, response byte count (from
``Content-Length``; ``None`` when the server did not say — never a guess),
request hash, classification, reason. NOT logged, anywhere, ever: request or
response bodies, credentials, or any header. Authorization material exists
only inside the in-flight request object.

The SDK-less local provider and the explicit ``provider test`` ping use
:func:`openmind.netguard.guarded_semantic_request` directly; this module is
for building clients to INJECT into the official SDKs (``openai``,
``anthropic``), which accept an ``http_client``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from .. import netguard
from .models import ProviderProfile
from .providers.profiles import expected_host


@dataclass
class SemanticEgressContext:
    """Mutable per-run audit context. One client serves many requests, so the
    runner updates ``task`` and ``request_hash`` before each provider call and
    the transport stamps whatever is current into the audit record."""
    workspace_id: str = ""
    profile: str = ""
    provider_kind: str = ""
    classification: str = ""
    task: str = ""
    request_hash: str = ""
    # purely informational counters, handy in tests and run summaries
    requests: int = field(default=0)

    def stamp(self, *, task: str, request_hash: str) -> None:
        self.task = task
        self.request_hash = request_hash


class AuditedSemanticTransport(httpx.BaseTransport):
    """Validates + audits every hop, then delegates to a real HTTP transport.

    *inner* is injectable for tests: a stub transport lets the provider
    adapters be exercised end-to-end (auth failure, rate limit, malformed
    output) with zero network access while STILL passing through this exact
    validation and audit path.
    """

    def __init__(self, *, allowed_host: str, remote: bool,
                 context: SemanticEgressContext,
                 inner: Optional[httpx.BaseTransport] = None) -> None:
        self._allowed_host = allowed_host
        self._remote = remote
        self._context = context
        self._inner: httpx.BaseTransport = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        ctx = self._context
        body = request.content or b""
        netguard.assert_semantic_host(
            str(request.url), allowed_host=self._allowed_host,
            remote=self._remote, method=request.method,
            workspace_id=ctx.workspace_id, profile=ctx.profile,
            provider_kind=ctx.provider_kind, task=ctx.task,
            classification=ctx.classification, request_hash=ctx.request_hash,
            request_bytes=len(body))
        response = self._inner.handle_request(request)
        content_length = response.headers.get("content-length")
        try:
            response_bytes: Optional[int] = (int(content_length)
                                             if content_length else None)
        except (TypeError, ValueError):
            response_bytes = None
        netguard.semantic_audit_record(
            method=request.method, url=str(request.url), allowed=True,
            reason=f"http {response.status_code}",
            workspace_id=ctx.workspace_id, profile=ctx.profile,
            provider_kind=ctx.provider_kind, task=ctx.task,
            classification=ctx.classification, request_hash=ctx.request_hash,
            request_bytes=len(body), response_bytes=response_bytes)
        ctx.requests += 1
        return response

    def close(self) -> None:
        self._inner.close()


def build_semantic_client(profile: ProviderProfile,
                          context: SemanticEgressContext,
                          inner: Optional[httpx.BaseTransport] = None
                          ) -> httpx.Client:
    """The ONLY constructor of HTTP clients for provider SDK injection.

    Redirect following is disabled at the client level, and — defense in
    depth — a hop would be revalidated by the transport even if it were on.
    """
    return httpx.Client(
        transport=AuditedSemanticTransport(
            allowed_host=expected_host(profile), remote=profile.is_remote,
            context=context, inner=inner),
        timeout=profile.timeout,
        follow_redirects=False)


__all__ = ["SemanticEgressContext", "AuditedSemanticTransport",
           "build_semantic_client"]
