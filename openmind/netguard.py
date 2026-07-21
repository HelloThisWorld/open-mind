"""Outbound network guard + audit log (Invariant 1: LOCAL-FIRST, AUDITED).

ALL HTTP in this application must go through this module. The guard:

  * by default refuses any host not in ``config.ALLOWED_HOSTS`` (loopback only) —
    the LLM/model calls and all default traffic stay strictly local;
  * additionally permits a SMALL, explicit reference-host allowlist
    (``config.ENRICH_ALLOWED_*``, e.g. ``*.wikipedia.org``) but ONLY via the
    dedicated :func:`guarded_external_request` used by the glossary enrichment
    engine — never the default path;
  * logs every single outbound call (host, port, method, url, allowed?) to an
    in-memory ring buffer and to ``data/outbound.log`` — egress is AUDITED, not
    silent. Enrichment only ever sends single glossary TERMS, never project source.

Embeddings run fully in-process (no HTTP). Legitimate egress is the local
llama-server (loopback) plus audited enrichment lookups to the allowlist above.
Anything else is blocked here.

To self-verify: grep the codebase for ``httpx`` / ``requests`` / ``urllib`` /
``aiohttp`` — the only client construction must be in this module; the only
non-loopback destinations must be ``config.ENRICH_ALLOWED_*``.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from . import config


class ExfiltrationBlocked(RuntimeError):
    """Raised when code attempts to reach a non-local host."""


_LOG: Deque[Dict[str, Any]] = deque(maxlen=500)
_LOCK = threading.Lock()


def _stamp() -> str:
    # local time string; avoids importing datetime everywhere
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _record(method: str, url: str, host: Optional[str], port: Optional[int],
            allowed: bool, note: str = "") -> Dict[str, Any]:
    entry = {
        "ts": _stamp(),
        "method": method.upper(),
        "url": url,
        "host": host,
        "port": port,
        "allowed": allowed,
        "note": note,
    }
    with _LOCK:
        _LOG.append(entry)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.OUTBOUND_LOG, "a", encoding="utf-8") as fh:
            verdict = "ALLOW" if allowed else "BLOCK"
            fh.write(f"{entry['ts']}\t{verdict}\t{entry['method']}\t{url}\t{note}\n")
    except Exception:
        pass
    return entry


def is_local_host(host: Optional[str]) -> bool:
    if not host:
        return False
    return host.lower() in config.ALLOWED_HOSTS


def is_enrich_host(host: Optional[str]) -> bool:
    """True for the explicit reference-host allowlist (e.g. ``*.wikipedia.org``),
    used ONLY by the audited enrichment path. Off entirely if ENRICH_EGRESS=0."""
    if not host or not config.ENRICH_EGRESS:
        return False
    h = host.lower()
    return (h in config.ENRICH_ALLOWED_HOSTS
            or any(h.endswith(suf) for suf in config.ENRICH_ALLOWED_SUFFIXES))


def is_sourcelink_host(host: Optional[str]) -> bool:
    """True for the source-link allowlist (``raw.githubusercontent.com``), used ONLY
    by the audited jump-to-source GitHub fetch. Off entirely if SOURCELINK_EGRESS=0."""
    if not host or not config.SOURCELINK_EGRESS:
        return False
    return host.lower() in config.SOURCELINK_ALLOWED_HOSTS


def assert_reachable(url: str, method: str = "GET", *, allow_enrich: bool = False,
                     allow_sourcelink: bool = False) -> None:
    """Validate a URL is reachable under policy; log every attempt; raise if not.
    Loopback is always allowed; the enrichment / source-link allowlists are allowed
    only when their respective flag is set (each a dedicated, separately-audited path)."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    local = is_local_host(host)
    enrich = bool(allow_enrich and is_enrich_host(host))
    sourcelink = bool(allow_sourcelink and is_sourcelink_host(host))
    allowed = local or enrich or sourcelink
    if local:
        note = ""
    elif enrich:
        note = "enrichment egress (audited)"
    elif sourcelink:
        note = "source-link egress (audited)"
    else:
        note = "REFUSED non-local host"
    _record(method, url, host, port, allowed, note=note)
    if not allowed:
        raise ExfiltrationBlocked(
            f"Refusing outbound request to non-local host {host!r} ({url}). "
            "Open Mind is local-first; only the audited enrichment / source-link "
            "allowlists may egress, and project content must never leave this machine."
        )


def assert_local(url: str, method: str = "GET") -> None:
    """Validate a URL is local (loopback only); log + raise if not."""
    assert_reachable(url, method, allow_enrich=False)


def guarded_external_request(method: str, url: str, *, timeout: float = 20.0,
                             **kwargs) -> httpx.Response:
    """Perform a single guarded request that MAY reach the enrichment allowlist
    (audited). Used by the glossary enrichment engine only. Redirects are followed
    but stay within the same allowlisted domains in practice."""
    assert_reachable(url, method, allow_enrich=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.request(method, url, **kwargs)


def guarded_sourcelink_request(method: str, url: str, *, timeout: float = 20.0,
                               **kwargs) -> httpx.Response:
    """Perform a single guarded request that MAY reach the source-link allowlist
    (audited). Used ONLY by the jump-to-source GitHub fetch. Redirects are followed
    but stay within the allowlisted host in practice (raw.githubusercontent.com)."""
    assert_reachable(url, method, allow_sourcelink=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.request(method, url, **kwargs)


def guarded_request(method: str, url: str, *, timeout: float = 60.0, **kwargs) -> httpx.Response:
    """Perform a single guarded HTTP request (sync)."""
    assert_local(url, method)
    with httpx.Client(timeout=timeout) as client:
        return client.request(method, url, **kwargs)


def stream_lines(method: str, url: str, *, timeout: float = 600.0, **kwargs):
    """Guarded streaming request — yields decoded response lines (for SSE from
    the local llama-server). Refuses + logs any non-local host (Invariant 1)."""
    assert_local(url, method)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(method, url, **kwargs) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                yield line


class GuardedAsyncClient:
    """Async httpx client that enforces the local-only policy per request."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        assert_local(url, method)
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GuardedAsyncClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def get_log(limit: int = 200) -> List[Dict[str, Any]]:
    with _LOCK:
        items = list(_LOG)
    return items[-limit:]


# ---------------------------------------------------------------------------
# Semantic egress (OpenMind v2 Phase 4) — a DEDICATED, separately audited path.
#
# The existing guarded_request stays loopback-only, untouched. A semantic
# provider call is different in kind: it may reach exactly ONE remote host —
# the host its provider profile names — over HTTPS, only after the workspace
# policy gate has said yes, and every request (allowed or blocked) is written
# to a structured JSON-lines audit (config.SEMANTIC_AUDIT_LOG) carrying the
# workspace, profile, task, byte counts, request hash and classification.
# Request/response BODIES and authorization headers are never logged anywhere.
#
# There is deliberately no allow_any_external flag and no wildcard: the
# allowed host is an exact string comparison against the profile's endpoint
# host, so a redirect, a document-supplied URL, or an SDK surprise cannot
# reach anywhere else. Cloud SDKs get their HTTP client from
# openmind.semantic.transport, whose transport validates EVERY hop through
# assert_semantic_host below.
# ---------------------------------------------------------------------------
class SemanticEgressBlocked(ExfiltrationBlocked):
    """A semantic provider call tried to reach a host outside its profile."""


def _semantic_audit(entry: Dict[str, Any]) -> None:
    """Append one structured record to the semantic audit log, and mirror a
    one-line summary into the general outbound ring so /netlog shows semantic
    traffic beside everything else. Never raises."""
    import json as _json
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.SEMANTIC_AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    note = (f"semantic egress ({entry.get('provider_kind', '')}"
            f"/{entry.get('task', '')})"
            if entry.get("allowed") else
            f"REFUSED semantic egress: {entry.get('reason', '')}")
    _record(str(entry.get("method", "")), str(entry.get("url", "")),
            entry.get("host"), entry.get("port"),
            bool(entry.get("allowed")), note=note)


def semantic_audit_record(*, method: str, url: str, allowed: bool,
                          reason: str, workspace_id: str = "",
                          profile: str = "", provider_kind: str = "",
                          task: str = "", classification: str = "",
                          request_hash: str = "",
                          request_bytes: Optional[int] = None,
                          response_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Write one semantic audit record (the Phase 4 audit contract). Byte
    counts are COUNTS, never content; None means "not known yet" (a blocked
    request has no response size)."""
    parsed = urlparse(url)
    entry = {
        "ts": _stamp(),
        "workspace_id": workspace_id,
        "profile": profile,
        "provider_kind": provider_kind,
        "task": task,
        "host": parsed.hostname,
        "port": parsed.port,
        "method": method.upper(),
        "url": f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path or ''}",
        "allowed": allowed,
        "request_bytes": request_bytes,
        "response_bytes": response_bytes,
        "request_hash": request_hash,
        "classification": classification,
        "reason": reason,
    }
    _semantic_audit(entry)
    return entry


def assert_semantic_host(url: str, *, allowed_host: str, remote: bool,
                         method: str = "POST", workspace_id: str = "",
                         profile: str = "", provider_kind: str = "",
                         task: str = "", classification: str = "",
                         request_hash: str = "",
                         request_bytes: Optional[int] = None) -> None:
    """Validate ONE semantic request hop. Raises SemanticEgressBlocked (and
    writes a blocked audit record) unless the URL's host is exactly the
    profile's allowed host, with HTTPS for remote and loopback for local.

    Called for every hop the audited transport sees, so a redirect can never
    escape the provider host even if a caller enabled redirect following.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    expected = (allowed_host or "").lower()
    reason = ""
    if not expected:
        reason = "profile has no allowed host configured"
    elif host != expected:
        reason = f"host {host!r} is not the profile host {expected!r}"
    elif remote and parsed.scheme != "https":
        reason = f"remote provider requires https, got {parsed.scheme!r}"
    elif not remote and not is_local_host(host):
        reason = f"local provider must be loopback, got {host!r}"
    if reason:
        semantic_audit_record(
            method=method, url=url, allowed=False, reason=reason,
            workspace_id=workspace_id, profile=profile,
            provider_kind=provider_kind, task=task,
            classification=classification, request_hash=request_hash,
            request_bytes=request_bytes)
        raise SemanticEgressBlocked(
            f"Refusing semantic egress to {host!r}: {reason}. A provider call "
            f"may reach only its profile's configured host.")


def guarded_semantic_request(method: str, url: str, *, allowed_host: str,
                             remote: bool, timeout: float = 120.0,
                             workspace_id: str = "", profile: str = "",
                             provider_kind: str = "", task: str = "",
                             classification: str = "",
                             request_hash: str = "",
                             transport: Optional[Any] = None,
                             **kwargs) -> httpx.Response:
    """One audited semantic HTTP request (used by the SDK-less local provider
    and by explicit `provider test` pings). Validates the host, performs the
    call with redirects DISABLED, and writes the completed audit record with
    request/response byte counts. Bodies and auth headers are never logged.

    *transport* is a test seam: a stub ``httpx.BaseTransport`` lets suites
    exercise this exact audited path with zero network access. Host
    validation and audit logging run identically either way."""
    body = kwargs.get("content")
    if body is None and "json" in kwargs:
        import json as _json
        body = _json.dumps(kwargs["json"]).encode("utf-8")
    request_bytes = len(body) if isinstance(body, (bytes, bytearray)) else None
    assert_semantic_host(url, allowed_host=allowed_host, remote=remote,
                         method=method, workspace_id=workspace_id,
                         profile=profile, provider_kind=provider_kind,
                         task=task, classification=classification,
                         request_hash=request_hash,
                         request_bytes=request_bytes)
    client_kwargs: Dict[str, Any] = {"timeout": timeout,
                                     "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    with httpx.Client(**client_kwargs) as client:
        resp = client.request(method, url, **kwargs)
    semantic_audit_record(
        method=method, url=url, allowed=True, reason="ok",
        workspace_id=workspace_id, profile=profile,
        provider_kind=provider_kind, task=task,
        classification=classification, request_hash=request_hash,
        request_bytes=request_bytes,
        response_bytes=len(resp.content) if resp.content is not None else None)
    return resp
