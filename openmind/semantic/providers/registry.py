"""Provider registry: kind -> adapter, with lazy construction.

Adapters are imported on first REQUEST of their kind, never at registry
import — that is what keeps ``import openmind.semantic`` (and everything
above it) working on a machine with no provider SDK installed. Requesting a
kind whose SDK is missing raises the typed configuration error from the
adapter itself, naming exactly the missing package.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

from ..errors import ProviderConfigurationError
from ..models import ProviderKind

_lock = threading.Lock()
_instances: Dict[str, Any] = {}


def _make_local_openai():
    from .local_openai import LocalOpenAIProvider
    return LocalOpenAIProvider()


def _make_openai():
    from .openai_provider import OpenAIProvider
    return OpenAIProvider()


def _make_anthropic():
    from .anthropic_provider import AnthropicProvider
    return AnthropicProvider()


def _make_azure():
    from .azure_openai_provider import AzureOpenAIProvider
    return AzureOpenAIProvider()


def _make_mock():
    from .mock_provider import MockProvider
    return MockProvider()


_FACTORIES: Dict[str, Callable[[], Any]] = {
    ProviderKind.LOCAL_OPENAI: _make_local_openai,
    ProviderKind.OPENAI: _make_openai,
    ProviderKind.ANTHROPIC: _make_anthropic,
    ProviderKind.AZURE_OPENAI: _make_azure,
    ProviderKind.MOCK: _make_mock,
}


def supported_kinds() -> List[str]:
    return sorted(_FACTORIES)


def get_provider(kind: str) -> Any:
    """The adapter instance for *kind*. Construction (and therefore any SDK
    import) happens here, once per process."""
    key = str(kind or "").strip().lower()
    if key not in _FACTORIES:
        raise ProviderConfigurationError(
            f"unknown provider kind: {kind!r}",
            details={"supported": supported_kinds()})
    with _lock:
        if key not in _instances:
            _instances[key] = _FACTORIES[key]()
        return _instances[key]


def reset() -> None:
    """Drop cached instances (tests that monkeypatch adapters)."""
    with _lock:
        _instances.clear()


__all__ = ["get_provider", "supported_kinds", "reset"]
