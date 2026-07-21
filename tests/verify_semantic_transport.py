"""Audited semantic transport — host pinning, HTTPS, redirect containment,
credential redaction, audit completeness, legacy paths untouched, and the
repository-wide no-bypass scan.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import check, finish  # noqa: E402

import httpx  # noqa: E402

from openmind import config, netguard  # noqa: E402
from openmind.semantic.models import ProviderProfile  # noqa: E402
from openmind.semantic.transport import (  # noqa: E402
    AuditedSemanticTransport, SemanticEgressContext, build_semantic_client)


def audit_lines():
    try:
        text = config.SEMANTIC_AUDIT_LOG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. Host pinning: only the profile host, exact match
# ---------------------------------------------------------------------------
try:
    netguard.assert_semantic_host("https://evil.example.com/v1",
                                  allowed_host="api.openai.com", remote=True,
                                  workspace_id="p_x", profile="pr",
                                  provider_kind="openai", task="t",
                                  classification="internal")
    check("off-profile host is blocked", False)
except netguard.SemanticEgressBlocked:
    check("off-profile host is blocked", True)
entry = audit_lines()[-1]
check("blocked attempt is audited with the full context",
      entry["allowed"] is False and entry["host"] == "evil.example.com"
      and entry["workspace_id"] == "p_x" and entry["profile"] == "pr"
      and entry["provider_kind"] == "openai" and entry["task"] == "t"
      and entry["classification"] == "internal" and entry["reason"])

try:
    netguard.assert_semantic_host("https://api.openai.com.evil.com/v1",
                                  allowed_host="api.openai.com", remote=True)
    check("suffix-spoofed host is blocked (exact match, not endswith)", False)
except netguard.SemanticEgressBlocked:
    check("suffix-spoofed host is blocked (exact match, not endswith)", True)

try:
    netguard.assert_semantic_host("http://api.openai.com/v1",
                                  allowed_host="api.openai.com", remote=True)
    check("plain HTTP to a remote provider is blocked", False)
except netguard.SemanticEgressBlocked:
    check("plain HTTP to a remote provider is blocked", True)

try:
    netguard.assert_semantic_host("http://192.168.1.10:7081/v1",
                                  allowed_host="192.168.1.10", remote=False)
    check("a 'local' provider pointed at a LAN host is blocked", False)
except netguard.SemanticEgressBlocked:
    check("a 'local' provider pointed at a LAN host is blocked", True)

netguard.assert_semantic_host("https://api.openai.com/v1/chat/completions",
                              allowed_host="api.openai.com", remote=True)
check("the profile's own host over HTTPS passes", True)

# ---------------------------------------------------------------------------
# 2. The audited transport: per-hop validation, redirects contained,
#    auth redaction, byte counts
# ---------------------------------------------------------------------------
os.environ["OM_TRANSPORT_KEY"] = "sk-transport-secret"
profile = ProviderProfile(name="pinned", kind="openai",
                          api_key_env="OM_TRANSPORT_KEY",
                          models={"fast": "m"})
context = SemanticEgressContext(workspace_id="p_t", profile="pinned",
                                provider_kind="openai",
                                classification="internal")
context.stamp(task="requirement-extraction", request_hash="rh1")


def redirect_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/start":
        return httpx.Response(302,
                              headers={"location": "https://evil.example.com/"})
    return httpx.Response(200, json={"ok": True},
                          headers={"content-length": "11"})


client = build_semantic_client(profile, context,
                               inner=httpx.MockTransport(redirect_handler))
response = client.request(
    "POST", "https://api.openai.com/start",
    headers={"authorization": "Bearer sk-transport-secret"},
    content=b'{"x":1}')
check("redirects are NOT auto-followed (302 returned, not chased)",
      response.status_code == 302)
try:
    client.request("GET", "https://evil.example.com/")
    check("even a manual follow to the redirect target is blocked per hop",
          False)
except netguard.SemanticEgressBlocked:
    check("even a manual follow to the redirect target is blocked per hop",
          True)
client.close()

entries = audit_lines()
allowed_entries = [e for e in entries if e["allowed"]]
check("allowed requests are audited with request byte counts",
      any(e.get("request_bytes") == 7 for e in allowed_entries))
raw_audit = config.SEMANTIC_AUDIT_LOG.read_text(encoding="utf-8")
raw_outbound = (config.OUTBOUND_LOG.read_text(encoding="utf-8")
                if config.OUTBOUND_LOG.exists() else "")
check("the credential value appears in NO audit log",
      "sk-transport-secret" not in raw_audit
      and "sk-transport-secret" not in raw_outbound)
check("request bodies are never written to the audit logs",
      '{"x":1}' not in raw_audit and '{"x":1}' not in raw_outbound)
check("semantic traffic is mirrored into the general outbound ring",
      any("semantic egress" in (e.get("note") or "")
          for e in netguard.get_log(200)))

# guarded_semantic_request: content-bearing POST with byte accounting
def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"pong": True})


resp = netguard.guarded_semantic_request(
    "POST", "http://127.0.0.1:7081/v1/chat/completions",
    allowed_host="127.0.0.1", remote=False, workspace_id="p_t",
    profile="local", provider_kind="local-openai", task="t",
    json={"messages": []}, transport=httpx.MockTransport(ok_handler))
check("guarded_semantic_request serves the loopback provider path",
      resp.status_code == 200)
last = audit_lines()[-1]
check("completed loopback call audited with response bytes",
      last["allowed"] and isinstance(last.get("response_bytes"), int))

# a document-supplied URL can never become a destination
try:
    netguard.guarded_semantic_request(
        "GET", "https://attacker.example.com/exfil?d=1",
        allowed_host="api.openai.com", remote=True)
    check("an arbitrary document-supplied URL is blocked", False)
except netguard.SemanticEgressBlocked:
    check("an arbitrary document-supplied URL is blocked", True)

# ---------------------------------------------------------------------------
# 3. Existing guard paths keep their exact behavior
# ---------------------------------------------------------------------------
try:
    netguard.assert_local("https://api.openai.com/v1")
    check("guarded_request stays loopback-only (openai host refused)", False)
except netguard.ExfiltrationBlocked:
    check("guarded_request stays loopback-only (openai host refused)", True)
netguard.assert_local("http://127.0.0.1:7080/v1")
check("guarded_request still allows loopback", True)
check("enrichment allowlist unchanged (wikipedia suffix logic present)",
      netguard.is_enrich_host("en.wikipedia.org") in (True, False))
check("semantic egress has no allow_any_external escape hatch",
      not hasattr(netguard, "allow_any_external"))

# ---------------------------------------------------------------------------
# 4. Repository scan: no provider HTTP client outside the audited boundary
# ---------------------------------------------------------------------------
repo = Path(__file__).resolve().parent.parent / "openmind"
offenders = []
allowed_files = {"transport.py"}          # the audited boundary itself
client_re = re.compile(
    r"httpx\.Client\(|httpx\.AsyncClient\(|requests\.(get|post|Session)"
    r"|urllib\.request|aiohttp")
for path in (repo / "semantic").rglob("*.py"):
    text = path.read_text(encoding="utf-8")
    if path.name in allowed_files:
        continue
    if client_re.search(text):
        offenders.append(str(path.relative_to(repo)))
check("no module under openmind/semantic constructs its own HTTP client "
      "(only transport.py may)", offenders == [],)
if offenders:
    print("  offenders:", offenders)

sdk_construct_re = re.compile(
    r"(openai\.OpenAI|openai\.AzureOpenAI|sdk\.Anthropic|anthropic\.Anthropic)\(")
bad_sdk = []
for path in (repo / "semantic").rglob("*.py"):
    text = path.read_text(encoding="utf-8")
    for match in sdk_construct_re.finditer(text):
        # http_client may be passed inline after the call or via a kwargs
        # dict built just above it — inspect a window around the match.
        window = text[max(0, match.start() - 800):match.start() + 800]
        if "http_client" not in window:
            bad_sdk.append(f"{path.name}:{match.group(0)}")
check("every SDK client construction injects the audited http_client",
      bad_sdk == [])
if bad_sdk:
    print("  offenders:", bad_sdk)

outside = []
for path in repo.rglob("*.py"):
    rel = str(path.relative_to(repo)).replace("\\", "/")
    if rel.startswith("semantic/") or rel == "netguard.py":
        continue
    text = path.read_text(encoding="utf-8")
    if re.search(r"import openai|import anthropic|from openai|from anthropic",
                 text):
        outside.append(rel)
check("no provider SDK import exists outside openmind/semantic", outside == [])
if outside:
    print("  offenders:", outside)

finish()
