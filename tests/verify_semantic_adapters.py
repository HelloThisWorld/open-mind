"""Phase 4 adapter + compatibility gate — additive REST routes, exactly
19 + 7 = 26 MCP tools, artifact 1.1.0 without provider SDKs, Skill Bridge
database independence, local Ask loopback, and the semantic REST surface
driven end-to-end.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    check, finish, find_evidence, make_workspace, mock_profile,
    requirement_response)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

# ---------------------------------------------------------------------------
# 1. MCP: the frozen 19 remain, exactly 7 read-only tools are added
# ---------------------------------------------------------------------------
from openmind import mcp_server  # noqa: E402

check("the nine core MCP tools are unchanged",
      mcp_server.TOOL_NAMES == ("search", "route", "dispatch", "get_glossary",
                                "find_similar_cases", "save_case", "get_doc",
                                "propose_fix", "apply_fix"))
check("the four asset tools are unchanged",
      mcp_server.ASSET_TOOL_NAMES == ("list_assets", "get_asset",
                                      "get_asset_revisions", "get_evidence"))
check("the six document tools are unchanged",
      mcp_server.DOCUMENT_TOOL_NAMES == (
          "list_documents", "get_document", "get_document_outline",
          "search_documents", "search_knowledge",
          "find_document_related_candidates"))
check("EXACTLY the seven semantic/lens tools are added",
      mcp_server.SEMANTIC_TOOL_NAMES == (
          "list_semantic_runs", "get_semantic_run",
          "list_semantic_candidates", "get_semantic_candidate",
          "list_project_lenses", "get_project_lens", "get_semantic_usage"))
total = (len(mcp_server.TOOLS) + len(mcp_server.ASSET_TOOLS)
         + len(mcp_server.DOCUMENT_TOOLS) + len(mcp_server.SEMANTIC_TOOLS))
check("the complete MCP set is 26 tools", total == 26)
forbidden = {"configure_provider", "set_semantic_policy", "start_analysis",
             "review_candidate", "approve_lens", "activate_lens",
             "semantic_analyze"}
all_names = set(mcp_server.TOOL_NAMES + mcp_server.ASSET_TOOL_NAMES
                + mcp_server.DOCUMENT_TOOL_NAMES
                + mcp_server.SEMANTIC_TOOL_NAMES)
check("no write/configure/paid-trigger tool exists on MCP",
      not (forbidden & all_names))

import asyncio  # noqa: E402
server = mcp_server.create_mcp_server()
registered = asyncio.new_event_loop().run_until_complete(server.list_tools())
# Phase 5 registers nine additive read-only graph tools beside these 26;
# the graph suite (verify_knowledge_adapters) accounts for each by name.
check("FastMCP registers the 26 pre-Phase-5 tools (35 with the graph set)",
      len(registered) == 26 + len(mcp_server.KNOWLEDGE_TOOLS)
      and len(mcp_server.KNOWLEDGE_TOOLS) == 9)

# ---------------------------------------------------------------------------
# 2. REST: every legacy route intact; semantic routes additive
# ---------------------------------------------------------------------------
from openmind.main import app  # noqa: E402

paths = {route.path for route in app.routes}
legacy = {"/projects", "/projects/{project_id}",
          "/projects/{project_id}/assets",
          "/projects/{project_id}/documents", "/ingest", "/search", "/ask",
          "/glossary", "/jobs", "/jobs/{job_id}", "/model-config",
          "/netlog", "/api/health"}
check("every pre-Phase-4 route is still registered", legacy <= paths)
check("the API still says /projects, not /workspaces",
      not any("/workspaces" in p for p in paths))
semantic_routes = {
    "/providers", "/providers/{name}",
    "/projects/{project_id}/semantic/policy",
    "/projects/{project_id}/semantic/plan",
    "/projects/{project_id}/semantic/analyses",
    "/projects/{project_id}/semantic/analyses/{run_id}",
    "/projects/{project_id}/semantic/analyses/{run_id}/resume",
    "/projects/{project_id}/semantic/analyses/{run_id}/usage",
    "/projects/{project_id}/semantic/candidates",
    "/projects/{project_id}/semantic/candidates/{candidate_id}",
    "/projects/{project_id}/semantic/candidates/{candidate_id}/review",
    "/projects/{project_id}/semantic/relations",
    "/projects/{project_id}/semantic/conflicts",
    "/projects/{project_id}/lenses",
    "/projects/{project_id}/lenses/{lens_id}",
    "/projects/{project_id}/lenses/induction-plan",
    "/projects/{project_id}/lenses/{lens_id}/validate",
    "/projects/{project_id}/lenses/{lens_id}/approve",
    "/projects/{project_id}/lenses/{lens_id}/reject",
    "/projects/{project_id}/lenses/{lens_id}/activate",
}
check("all additive semantic/lens routes are registered",
      semantic_routes <= paths)

# ---------------------------------------------------------------------------
# 3. REST end-to-end through the TestClient
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
pid = make_workspace(runtime, "adapters-ws")
evidence_id = find_evidence(pid, "shall respond to a status query")
os.environ["OM_ADAPTER_SECRET"] = "sk-adapter-secret"
mock_profile("mock-rest", responses={
    "requirement-extraction": requirement_response(evidence_id)})

client = TestClient(app)
response = client.get("/providers")
check("GET /providers lists profiles without any key value",
      response.status_code == 200
      and "sk-adapter-secret" not in response.text)
response = client.get("/providers/mock-rest")
check("GET /providers/{name} works", response.status_code == 200)
response = client.get("/providers/absent")
check("unknown provider maps to the typed 400",
      response.status_code == 400)

response = client.get(f"/projects/{pid}/semantic/policy")
check("GET semantic policy reports fail-closed defaults",
      response.json()["policy"]["data_classification"] == "restricted")
response = client.post(f"/projects/{pid}/semantic/policy",
                       json={"provider_profile": "mock-rest"})
check("POST semantic policy selects the provider",
      response.json()["policy"]["provider_profile"] == "mock-rest")
response = client.post(f"/projects/{pid}/semantic/policy",
                       json={"budgets": {"bogus_key": 1}})
check("unknown budget key maps to 400", response.status_code == 400)

response = client.post(f"/projects/{pid}/semantic/plan",
                       json={"tasks": ["requirement-extraction"]})
check("POST plan returns the dry-run",
      response.status_code == 200
      and response.json()["plan"]["provider_calls_made"] == 0)

response = client.post(
    f"/projects/{pid}/semantic/analyses",
    json={"tasks": ["requirement-extraction"], "wait": True,
          "timeout": 180})
body = response.json()
check("POST analyses runs to done",
      response.status_code == 200 and body["run"]["status"] == "done")
run_id = body["run_id"]

response = client.get(f"/projects/{pid}/semantic/analyses")
check("GET analyses lists the run",
      any(r["id"] == run_id for r in response.json()["runs"]))
response = client.get(f"/projects/{pid}/semantic/analyses/{run_id}/usage")
check("GET usage returns the ledger",
      response.json()["totals"]["requests"] >= 1)

response = client.get(f"/projects/{pid}/semantic/candidates",
                      params={"type": "requirement"})
candidates = response.json()["candidates"]
check("GET candidates returns verified candidates", len(candidates) >= 1)
candidate_id = candidates[0]["id"]
response = client.post(
    f"/projects/{pid}/semantic/candidates/{candidate_id}/review",
    json={"decision": "confirm", "note": "ok", "reviewer": "rest"})
check("POST review confirms",
      response.json()["candidate"]["review_status"] == "confirmed")
response = client.get(f"/projects/p_other/semantic/candidates/{candidate_id}")
check("a candidate is not readable through another workspace",
      response.status_code == 404)
response = client.get(f"/projects/{pid}/semantic/relations")
check("GET relations works", response.status_code == 200)
response = client.get(f"/projects/{pid}/semantic/conflicts")
check("GET conflicts works", response.status_code == 200)

response = client.get(f"/projects/{pid}/lenses")
check("GET lenses lists built-ins",
      any(l["source"] == "builtin" for l in response.json()["lenses"]))
response = client.post(f"/projects/{pid}/lenses/induction-plan",
                       json={"provider": "mock-rest"})
check("POST induction-plan is deterministic and call-free",
      response.json()["plan"]["provider_calls_made"] == 0)
response = client.post(f"/projects/{pid}/lenses/builtin:generic/activate")
check("POST lens activate works over REST",
      response.status_code == 200
      and response.json()["lens"]["status"] == "active")

# ---------------------------------------------------------------------------
# 4. MCP read-only tools end-to-end
# ---------------------------------------------------------------------------
runs = mcp_server.list_semantic_runs(pid)
check("MCP list_semantic_runs returns the run",
      any(r["id"] == run_id for r in runs["runs"]))
run_detail = mcp_server.get_semantic_run(pid, run_id)
check("MCP get_semantic_run includes target counts",
      run_detail["targets"])
cands = mcp_server.list_semantic_candidates(pid,
                                            candidate_type="requirement")
check("MCP list_semantic_candidates labels everything candidate",
      all(c["status"] == "candidate" for c in cands["candidates"]))
detail = mcp_server.get_semantic_candidate(pid, candidate_id)
check("MCP get_semantic_candidate returns evidence", detail["evidence"])
lens_list = mcp_server.list_project_lenses(pid)
check("MCP list_project_lenses works", lens_list["count"] >= 1)
usage = mcp_server.get_semantic_usage(pid, run_id)
check("MCP get_semantic_usage returns the ledger",
      usage["totals"]["requests"] >= 1)

# ---------------------------------------------------------------------------
# 5. Compatibility: artifact 1.1.0, Skill Bridge, local Ask, code/doc RAG
# ---------------------------------------------------------------------------
from openmind import artifacts  # noqa: E402
check("artifact schemaVersion is still 1.1.0",
      artifacts.SCHEMA_VERSION == "1.1.0")
artifact_src = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "fixtures", "sample-repo")
out_dir = tempfile.mkdtemp(prefix="om_art_")
summary = artifacts.generate_artifacts(artifact_src, out_dir)
manifest_path = None
for root, _dirs, files in os.walk(out_dir):
    if "manifest.json" in files:
        manifest_path = os.path.join(root, "manifest.json")
        break
manifest = json.load(open(manifest_path, encoding="utf-8"))
check("the dependency-free artifact export still runs at schema 1.1.0",
      summary["files"] and manifest["schemaVersion"].startswith("1.1."))
check("the exported artifact carries no semantic candidate or lens keys",
      "semanticCandidates" not in json.dumps(manifest)
      and "lenses" not in manifest)

import openmind.skill_bridge as skill_bridge  # noqa: E402
bridge_source = open(skill_bridge.__file__, encoding="utf-8").read()
check("the Skill Bridge still never imports the database or the semantic "
      "plane", "import db" not in bridge_source
      and "semantic" not in bridge_source)

from openmind import llm_client  # noqa: E402
check("local Ask stays loopback-only", llm_client.is_local_endpoint())
check("Ask's base URL is untouched by Phase 4 (loopback llama-server)",
      llm_client.base_url().startswith("http://127.0.0.1"))

results = runtime.documents.search(pid, "status query", limit=5)
check("document RAG still answers", "hits" in results)
knowledge = runtime.documents.search_knowledge(pid, "status query")
check("combined knowledge search still answers",
      "code" in knowledge and "documents" in knowledge)

# job payloads for the semantic run carried no content
from openmind import db  # noqa: E402
semantic_jobs = [j for j in db.list_jobs(project_ids=[pid])
                 if j["type"] == "semantic_analysis"]
payloads = [db.get_job_payload(j["job_id"]) for j in semantic_jobs]
check("semantic job payloads carry identifiers only (no content, no "
      "absolute path)", payloads and all(
          set(p) <= {"analysis_run_id", "workspace_id", "task_types",
                     "scope", "provider_profile", "model_tier",
                     "budget_overrides", "force"} for p in payloads))

finish()
