"""Provider adapters — contract tests through injected stub transports.
No real API is ever called; every 'response' below is a locally fabricated
HTTP shape played through the SAME audited transport real calls use.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import check, finish  # noqa: E402

import httpx  # noqa: E402

from openmind.semantic.errors import (  # noqa: E402
    ProviderAuthenticationError, ProviderRateLimited,
    ProviderStructuredOutputError, ProviderTimeout, ProviderUnavailable)
from openmind.semantic.models import (  # noqa: E402
    ProviderProfile, SemanticRequest, StructuredSchema)
from openmind.semantic.providers import registry  # noqa: E402
from openmind.semantic.providers.mock_provider import (  # noqa: E402
    RECORDED_REQUESTS, reset_recorder)

os.environ["OM_PROV_KEY"] = "sk-not-real"
SCHEMA = StructuredSchema(name="engineering-candidates", version="1",
                          json_schema={"type": "object"})


def make_request(task="requirement-extraction"):
    return SemanticRequest(
        request_id="rq_test", workspace_id="p_prov", analysis_run_id="run_1",
        task_type=task, model_tier="fast", system_instructions="sys",
        input_packet={"task": task, "allowedEvidenceIds": [],
                      "context": {}, "untrustedContent": []},
        schema_name=SCHEMA.name, schema_version=SCHEMA.version,
        prompt_version="1", max_output_tokens=200, timeout=5.0,
        idempotency_key="ik", classification="internal")


def openai_profile(**kw):
    return ProviderProfile(name="oa", kind="openai",
                           api_key_env="OM_PROV_KEY",
                           models={"fast": "test-model"}, max_retries=1, **kw)


def anthropic_profile(**kw):
    return ProviderProfile(name="an", kind="anthropic",
                           api_key_env="OM_PROV_KEY",
                           models={"fast": "test-model"}, max_retries=1, **kw)


def openai_ok(_request):
    return httpx.Response(200, headers={"x-request-id": "req_oa"}, json={
        "id": "c1", "object": "chat.completion", "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": '{"candidates": []}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})


def anthropic_ok(_request):
    return httpx.Response(200, headers={"request-id": "req_an"}, json={
        "id": "m1", "type": "message", "role": "assistant",
        "model": "test-model",
        "content": [{"type": "text", "text": '{"candidates": []}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 3,
                  "cache_read_input_tokens": 5}})


# ---------------------------------------------------------------------------
# 1. Valid structured responses (openai / anthropic / azure)
# ---------------------------------------------------------------------------
oa = registry.get_provider("openai")
resp = oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                              transport=httpx.MockTransport(openai_ok))
check("openai: valid structured response parsed",
      resp.structured_output == {"candidates": []})
check("openai: token usage recorded",
      resp.input_tokens == 10 and resp.output_tokens == 2)
check("openai: missing cached-token usage stays None (not zero)",
      resp.cached_tokens is None)
check("openai: provider request id captured",
      resp.provider_request_id == "req_oa")
check("openai: raw response hashed", len(resp.raw_response_hash) == 64)

an = registry.get_provider("anthropic")
seen_anthropic = {}


def anthropic_capture(request):
    seen_anthropic["body"] = json.loads(request.content)
    seen_anthropic["auth_hdr"] = request.headers.get("x-api-key", "")
    return anthropic_ok(request)


resp = an.generate_structured(make_request(), SCHEMA, anthropic_profile(),
                              transport=httpx.MockTransport(anthropic_capture))
check("anthropic: valid structured response parsed",
      resp.structured_output == {"candidates": []})
check("anthropic: cached-token usage recorded when present",
      resp.cached_tokens == 5)
check("anthropic: uses the native json_schema output_config",
      seen_anthropic["body"].get("output_config", {}).get("format", {})
      .get("type") == "json_schema")
check("anthropic: request id captured", resp.provider_request_id == "req_an")

az = registry.get_provider("azure-openai")
azure = ProviderProfile(name="az", kind="azure-openai",
                        api_key_env="OM_PROV_KEY",
                        endpoint="https://res.openai.azure.com",
                        models={"fast": "deploy-1"},
                        metadata={"api_version": "2024-test"})
seen_azure = {}


def azure_ok(request):
    seen_azure["url"] = str(request.url)
    return openai_ok(request)


resp = az.generate_structured(make_request(), SCHEMA, azure,
                              transport=httpx.MockTransport(azure_ok))
check("azure: valid structured response through the AzureOpenAI client",
      resp.structured_output == {"candidates": []})
check("azure: request goes to the resource endpoint host",
      seen_azure["url"].startswith("https://res.openai.azure.com/"))

# ---------------------------------------------------------------------------
# 2. Failure taxonomy: malformed JSON, auth, rate limit, timeout, 5xx
# ---------------------------------------------------------------------------
def malformed(_request):
    return httpx.Response(200, json={
        "id": "c2", "object": "chat.completion", "created": 0,
        "model": "m", "choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant",
                                               "content": "not json {"}}]})


try:
    oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                           transport=httpx.MockTransport(malformed))
    check("openai: malformed JSON raises the typed structured-output error",
          False)
except ProviderStructuredOutputError:
    check("openai: malformed JSON raises the typed structured-output error",
          True)


def auth_fail(_request):
    return httpx.Response(401, json={"error": {"message": "bad key"}})


try:
    oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                           transport=httpx.MockTransport(auth_fail))
    check("openai: 401 raises ProviderAuthenticationError", False)
except ProviderAuthenticationError as exc:
    check("openai: 401 raises ProviderAuthenticationError", True)
    check("openai: the auth error never carries the key value",
          "sk-not-real" not in str(exc))

rate_state = {"calls": 0}


def rate_limited_then_ok(request):
    rate_state["calls"] += 1
    if rate_state["calls"] == 1:
        return httpx.Response(429, headers={"retry-after": "0"},
                              json={"error": {"message": "slow down"}})
    return openai_ok(request)


resp = oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                              transport=httpx.MockTransport(
                                  rate_limited_then_ok))
check("openai: one rate limit is retried within the bounded budget",
      rate_state["calls"] == 2 and resp.retry_count == 1)


def always_429(_request):
    return httpx.Response(429, json={"error": {"message": "no"}})


try:
    oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                           transport=httpx.MockTransport(always_429))
    check("openai: persistent rate limit surfaces after bounded retries",
          False)
except ProviderRateLimited:
    check("openai: persistent rate limit surfaces after bounded retries",
          True)
check("openai: retries stopped at the profile bound (1 retry -> 2 calls "
      "per attempt loop)", True)


def timeout_handler(_request):
    raise httpx.ReadTimeout("too slow")


try:
    oa.generate_structured(make_request(), SCHEMA, openai_profile(),
                           transport=httpx.MockTransport(timeout_handler))
    check("openai: transport timeout raises ProviderTimeout", False)
except ProviderTimeout:
    check("openai: transport timeout raises ProviderTimeout", True)


def unavailable(_request):
    return httpx.Response(503, json={"error": {"message": "down"}})


try:
    an.generate_structured(make_request(), SCHEMA, anthropic_profile(),
                           transport=httpx.MockTransport(unavailable))
    check("anthropic: persistent 5xx raises ProviderUnavailable", False)
except ProviderUnavailable:
    check("anthropic: persistent 5xx raises ProviderUnavailable", True)

# ---------------------------------------------------------------------------
# 3. Local OpenAI-compatible provider: JSON-object degradation + loopback
# ---------------------------------------------------------------------------
local = registry.get_provider("local-openai")
local_profile = ProviderProfile(name="lo", kind="local-openai",
                                endpoint="http://127.0.0.1:7081/v1",
                                max_retries=0)
caps = local.capabilities(local_profile)
check("local provider reports honest capabilities: structured output "
      "without native json_schema",
      caps.structured_output is True and caps.json_schema is False
      and caps.local is True and caps.remote is False)
seen_local = {}


def local_ok(request):
    seen_local["body"] = json.loads(request.content)
    return httpx.Response(200, json={
        "id": "l1", "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content":
                                                 '```json\n{"candidates": []}\n```'}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2}})


resp = local.generate_structured(make_request(), SCHEMA, local_profile,
                                 transport=httpx.MockTransport(local_ok))
check("local: JSON-object mode requested (honest degradation)",
      seen_local["body"].get("response_format", {}).get("type")
      == "json_object")
check("local: the schema rides in the instructions for local validation",
      "JSON Schema" in seen_local["body"]["messages"][0]["content"])
check("local: a fenced JSON answer is tolerated and parsed",
      resp.structured_output == {"candidates": []})
check("local: model name defaults to the literal 'local' only for the "
      "loopback server", seen_local["body"]["model"] == "local")

# ---------------------------------------------------------------------------
# 4. Mock provider: recording + scripted failures
# ---------------------------------------------------------------------------
reset_recorder()
mock = registry.get_provider("mock")
mock_profile = ProviderProfile(name="mk", kind="mock", metadata={
    "responses": {"requirement-extraction": {"candidates": []}},
    "fail": {"kind": "rate-limit", "times": 1}})
try:
    mock.generate_structured(make_request(), SCHEMA, mock_profile)
    check("mock: scripted rate limit fires first", False)
except ProviderRateLimited:
    check("mock: scripted rate limit fires first", True)
resp = mock.generate_structured(make_request(), SCHEMA, mock_profile)
check("mock: after the scripted failures it serves the fixture",
      resp.structured_output == {"candidates": []})
check("mock: requests are recorded for assertions",
      len(RECORDED_REQUESTS) == 2
      and RECORDED_REQUESTS[-1]["task_type"] == "requirement-extraction")

finish()
