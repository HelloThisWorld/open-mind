"""Explicit live provider test (``provider test --live``).

Pings the provider's cheapest authenticated read endpoint — the model
listing — through the SAME audited transport as real analysis. Sends ZERO
project content and returns only reachability + a bounded detail string.
Never called by CI, never called implicitly: the ``--live`` flag is the
user's explicit consent to one provider request.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ... import netguard
from ..errors import SemanticError
from ..models import ProviderKind, ProviderProfile
from ..transport import SemanticEgressContext, build_semantic_client
from .profiles import expected_host, resolve_api_key


def ping(profile: ProviderProfile,
         transport: Optional[Any] = None) -> Dict[str, Any]:
    """One authenticated reachability probe. Returns ``{reachable, detail}``
    and never raises for a provider-side failure — the CLI reports honestly
    either way."""
    try:
        if profile.kind == ProviderKind.MOCK:
            return {"reachable": True, "detail": "mock provider (no network)"}
        if profile.kind == ProviderKind.LOCAL_OPENAI:
            response = netguard.guarded_semantic_request(
                "GET", profile.endpoint.rstrip("/") + "/models",
                allowed_host=expected_host(profile), remote=False,
                timeout=min(profile.timeout, 10.0), profile=profile.name,
                provider_kind=profile.kind, task="provider-test",
                **({"transport": transport} if transport else {}))
            return {"reachable": response.status_code < 500,
                    "detail": f"http {response.status_code} from "
                              f"{expected_host(profile)}"}

        context = SemanticEgressContext(profile=profile.name,
                                        provider_kind=profile.kind,
                                        task="provider-test")
        http_client = build_semantic_client(profile, context, inner=transport)
        try:
            if profile.kind in (ProviderKind.OPENAI,
                                ProviderKind.AZURE_OPENAI):
                import openai
                if profile.kind == ProviderKind.AZURE_OPENAI:
                    client = openai.AzureOpenAI(
                        api_key=resolve_api_key(profile),
                        azure_endpoint=profile.endpoint,
                        api_version=str((profile.metadata or {})
                                        .get("api_version") or ""),
                        http_client=http_client, max_retries=0,
                        timeout=min(profile.timeout, 15.0))
                else:
                    kwargs: Dict[str, Any] = {
                        "api_key": resolve_api_key(profile),
                        "http_client": http_client, "max_retries": 0,
                        "timeout": min(profile.timeout, 15.0)}
                    if profile.endpoint:
                        kwargs["base_url"] = profile.endpoint
                    client = openai.OpenAI(**kwargs)
                first = next(iter(client.models.list()), None)
                return {"reachable": True,
                        "detail": f"authenticated; models visible "
                                  f"(e.g. {getattr(first, 'id', 'none')})"}
            if profile.kind == ProviderKind.ANTHROPIC:
                import anthropic
                client_kwargs: Dict[str, Any] = {
                    "api_key": resolve_api_key(profile),
                    "http_client": http_client, "max_retries": 0,
                    "timeout": min(profile.timeout, 15.0)}
                if profile.endpoint:
                    client_kwargs["base_url"] = profile.endpoint
                client = anthropic.Anthropic(**client_kwargs)
                page = client.models.list(limit=1)
                first = next(iter(page), None)
                return {"reachable": True,
                        "detail": f"authenticated; models visible "
                                  f"(e.g. {getattr(first, 'id', 'none')})"}
        finally:
            http_client.close()
        return {"reachable": False,
                "detail": f"kind {profile.kind!r} has no live test"}
    except SemanticError as exc:
        return {"reachable": False, "detail": f"{exc.code}: {exc.message}"}
    except ImportError as exc:
        return {"reachable": False, "detail": f"SDK missing: {exc}"}
    except Exception as exc:                       # bounded, honest report
        return {"reachable": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


__all__ = ["ping"]
