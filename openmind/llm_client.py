"""Local LLM client — OpenAI-compatible, pinned to the local llama-server.

Every chat/summary/doc-gen LLM call goes through here, and every request is
routed via :mod:`netguard`, which refuses any non-local host (Invariant 1).
No cloud SDKs, no external base URLs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import config, db, netguard


def base_url() -> str:
    cfg = db.get_model_config()
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 7080)
    return f"http://{host}:{port}/v1"


def is_local_endpoint() -> bool:
    return netguard.is_local_host(_host_of(base_url()))


def _host_of(url: str) -> Optional[str]:
    from urllib.parse import urlparse
    return urlparse(url).hostname


def chat(messages: List[Dict[str, str]], *, temperature: float = config.LLM_TEMPERATURE,
         max_tokens: int = 1024, timeout: float = config.LLM_TIMEOUT,
         model: str = "local") -> str:
    """Single chat completion. Returns assistant text. Raises on transport error."""
    url = base_url().rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    resp = netguard.guarded_request("POST", url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def is_ready(timeout: float = 2.0) -> bool:
    """Fast probe: is the local server up AND past model-load (/health == 200)?
    Used to gate LLM enrichment so ingest never blocks on a missing/loading model."""
    cfg = db.get_model_config()
    host, port = cfg.get("host", "127.0.0.1"), cfg.get("port", 7080)
    try:
        r = netguard.guarded_request("GET", f"http://{host}:{port}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def chat_stream(messages: List[Dict[str, str]], *, temperature: float = config.LLM_TEMPERATURE,
                max_tokens: int = 1024, timeout: float = config.LLM_TIMEOUT,
                model: str = "local"):
    """Stream a chat completion from the local server. Yields text deltas.
    All transport goes through netguard (local-only)."""
    import json as _json
    url = base_url().rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model, "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": True,
    }
    for line in netguard.stream_lines("POST", url, json=payload, timeout=timeout):
        if not line:
            continue
        if line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = _json.loads(data)
                delta = obj["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if delta:
                yield delta


def health() -> Dict[str, Any]:
    """Probe the local server's health/models endpoint."""
    cfg = db.get_model_config()
    host, port = cfg.get("host", "127.0.0.1"), cfg.get("port", 7080)
    info: Dict[str, Any] = {"reachable": False, "models": []}
    for ep in (f"http://{host}:{port}/health", f"http://{host}:{port}/v1/models"):
        try:
            r = netguard.guarded_request("GET", ep, timeout=3.0)
            if r.status_code < 500:
                info["reachable"] = True
                if ep.endswith("/models"):
                    try:
                        info["models"] = [m.get("id") for m in r.json().get("data", [])]
                    except Exception:
                        pass
                return info
        except Exception:
            continue
    return info
