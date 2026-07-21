"""Azure OpenAI semantic provider — official ``openai`` SDK's AzureOpenAI
client, audited transport.

SHIPPED, NOT STUBBED — the decision the spec asks for, made against the
installed SDK: ``openai.AzureOpenAI`` accepts ``http_client=`` (verified at
implementation time) and serves the SAME Chat Completions surface with the
same native strict JSON-Schema ``response_format``, so the full required
contract — native structured output + injectable audited transport — holds.
The adapter therefore subclasses the OpenAI adapter and overrides only client
construction.

Azure-specific profile rules (validated statically):

* ``endpoint`` is REQUIRED — the resource URL, e.g.
  ``https://<resource>.openai.azure.com``. HTTPS enforced; the audited
  transport pins to exactly that host.
* ``metadata.api_version`` is REQUIRED — Azure's API surface is versioned by
  date and silently defaulting one here would be a hidden moving part.
* Model names are Azure DEPLOYMENT names (configured per tier, like every
  other profile; nothing is defaulted).
"""
from __future__ import annotations

from typing import Any, List

from ..errors import ProviderConfigurationError
from ..models import (ProviderCapabilities, ProviderKind, ProviderProfile,
                      ProviderValidation)
from .openai_provider import OpenAIProvider
from .profiles import resolve_api_key


class AzureOpenAIProvider(OpenAIProvider):
    kind = ProviderKind.AZURE_OPENAI

    def capabilities(self, profile: ProviderProfile) -> ProviderCapabilities:
        caps = super().capabilities(profile)
        caps.custom_endpoint = True         # the resource endpoint is the point
        return caps

    def validate_profile(self, profile: ProviderProfile) -> ProviderValidation:
        validation = super().validate_profile(profile)
        extra: List[str] = []
        if not str((profile.metadata or {}).get("api_version") or "").strip():
            extra.append("azure-openai requires metadata.api_version "
                         "(e.g. a dated Azure OpenAI API version)")
        if extra:
            return ProviderValidation(ok=False,
                                      errors=list(validation.errors) + extra,
                                      warnings=list(validation.warnings))
        return validation

    def _build_sdk_client(self, openai_mod, profile: ProviderProfile,
                          http_client) -> Any:
        api_version = str((profile.metadata or {}).get("api_version")
                          or "").strip()
        if not profile.endpoint or not api_version:
            raise ProviderConfigurationError(
                f"azure-openai profile {profile.name!r} needs an https "
                f"endpoint and metadata.api_version",
                details={"profile": profile.name})
        return openai_mod.AzureOpenAI(
            api_key=resolve_api_key(profile),
            azure_endpoint=profile.endpoint,
            api_version=api_version,
            http_client=http_client,
            max_retries=0,
            timeout=profile.timeout)


__all__ = ["AzureOpenAIProvider"]
