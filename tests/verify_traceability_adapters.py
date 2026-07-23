"""Phase 6 adapters: additive REST routes, exactly eight read-only MCP
tools (the 43-tool compatibility gate), no write-capable trace/conflict
tool, zero provider calls during refresh/scan, Skill Bridge untouched."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind import mcp_server  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

# -- MCP: the 43-tool compatibility gate --------------------------------------
check("the nine core MCP tools are unchanged",
      mcp_server.TOOL_NAMES == ("search", "route", "dispatch",
                                "get_glossary", "find_similar_cases",
                                "save_case", "get_doc", "propose_fix",
                                "apply_fix"))
check("the four asset tools are unchanged",
      mcp_server.ASSET_TOOL_NAMES == ("list_assets", "get_asset",
                                      "get_asset_revisions",
                                      "get_evidence"))
check("the six document tools are unchanged",
      mcp_server.DOCUMENT_TOOL_NAMES == (
          "list_documents", "get_document", "get_document_outline",
          "search_documents", "search_knowledge",
          "find_document_related_candidates"))
check("the seven semantic tools are unchanged",
      mcp_server.SEMANTIC_TOOL_NAMES == (
          "list_semantic_runs", "get_semantic_run",
          "list_semantic_candidates", "get_semantic_candidate",
          "list_project_lenses", "get_project_lens",
          "get_semantic_usage"))
check("the nine knowledge tools are unchanged",
      mcp_server.KNOWLEDGE_TOOL_NAMES == (
          "get_graph_stats", "search_graph", "get_graph_node",
          "expand_graph", "find_graph_path", "list_engineering_entities",
          "get_engineering_entity", "get_engineering_claim",
          "get_engineering_relation"))
check("EXACTLY the eight trace/conflict tools are added",
      mcp_server.TRACE_TOOL_NAMES == (
          "trace_requirement", "trace_code", "trace_test",
          "get_trace_path", "get_traceability_coverage",
          "list_traceability_gaps", "list_engineering_conflicts",
          "get_engineering_conflict"))
total = (len(mcp_server.TOOLS) + len(mcp_server.ASSET_TOOLS)
         + len(mcp_server.DOCUMENT_TOOLS) + len(mcp_server.SEMANTIC_TOOLS)
         + len(mcp_server.KNOWLEDGE_TOOLS) + len(mcp_server.TRACE_TOOLS))
# The Phase 6 trace/conflict tools bring the set to 43; Phase 7 adds 8 more
# read-only git-overlay tools (accounted for below) for 51 in total.
check("the Phase 1-6 MCP set is 43 tools (35 + 8)", total == 43)
check("Phase 7 adds exactly 8 read-only overlay tools",
      len(mcp_server.OVERLAY_TOOLS) == 8)

forbidden = {"set_trace_policy", "refresh_traceability", "trace_refresh",
             "scan_conflicts", "conflict_scan", "promote_conflict",
             "promote_conflict_candidate", "resolve_conflict",
             "accept_conflict_risk", "dismiss_gap", "accept_gap",
             "resolve_gap"}
all_names = set(mcp_server.TOOL_NAMES + mcp_server.ASSET_TOOL_NAMES
                + mcp_server.DOCUMENT_TOOL_NAMES
                + mcp_server.SEMANTIC_TOOL_NAMES
                + mcp_server.KNOWLEDGE_TOOL_NAMES
                + mcp_server.TRACE_TOOL_NAMES
                + mcp_server.OVERLAY_TOOL_NAMES)
check("no policy-change/refresh/scan/promotion/resolution tool on MCP",
      not (forbidden & all_names))
check("every trace tool carries a docstring",
      all((fn.__doc__ or "").strip() for fn in mcp_server.TRACE_TOOLS))

import asyncio  # noqa: E402
server = mcp_server.create_mcp_server()
registered = sorted(t.name for t in
                    asyncio.new_event_loop().run_until_complete(
                        server.list_tools()))
check("FastMCP registers all 51 (43 + 8 additive overlay tools)",
      len(registered) == 51)
check("every registered tool is an accounted-for addition",
      set(registered) == all_names)

# -- fixture + MCP tool behavior ---------------------------------------------
runtime = get_runtime()
fx = make_fixture(runtime, "adapters-fix")
pid = fx.pid
trace_service = fx.trace
objects = fx.lifecycle()
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 2 seconds.")
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 5 seconds.")
trace_service.set_workspace_policy(pid, policy_name="api-service",
                                   actor="fx", note="api")
trace_service.refresh(pid)
trace_service.scan_conflicts(pid, actor="scanner")

result = mcp_server.trace_requirement(pid, objects["requirement"]["id"])
check("MCP trace_requirement returns the formal trace",
      result["paths"] and result["policy"]["name"] == "api-service")
result = mcp_server.trace_code(pid, objects["code"]["id"])
check("MCP trace_code resolves upstream",
      any(r["entity_id"] == objects["requirement"]["id"]
          for r in result["requirements"]))
result = mcp_server.trace_test(pid, objects["test"]["id"])
check("MCP trace_test resolves the requirement",
      bool(result["requirements"]))
coverage = mcp_server.get_traceability_coverage(pid)
check("MCP coverage returns the snapshot", coverage["snapshot"])
from openmind.traceability import store as trace_store  # noqa: E402
path_id = trace_store.list_paths(pid, limit=1)[0]["id"]
path = mcp_server.get_trace_path(pid, path_id)
check("MCP get_trace_path returns steps", path["steps"])
gaps = mcp_server.list_traceability_gaps(pid)
check("MCP gaps listing bounded", "gaps" in gaps)
conflicts = mcp_server.list_engineering_conflicts(pid, status="open")
check("MCP conflicts listing works", conflicts["count"] >= 1)
conflict = mcp_server.get_engineering_conflict(
    pid, conflicts["conflicts"][0]["id"])
check("MCP get_engineering_conflict carries joins",
      conflict["evidence"] and conflict["objects"])

# -- zero provider calls during refresh + scan --------------------------------
import openmind.semantic.providers as providers_pkg  # noqa: E402


def _boom(*_args, **_kwargs):
    raise AssertionError("a semantic provider was touched")


original_build = getattr(providers_pkg, "build_provider", None)
if original_build is not None:
    providers_pkg.build_provider = _boom
try:
    import urllib.request  # noqa: E402
    original_open = urllib.request.urlopen
    urllib.request.urlopen = _boom
    try:
        fx.claim(objects["requirement"], "constraint",
                 "The retry count is 3.")
        refresh_result = trace_service.refresh(pid)
        scan_result = trace_service.scan_conflicts(pid, actor="scanner")
    finally:
        urllib.request.urlopen = original_open
finally:
    if original_build is not None:
        providers_pkg.build_provider = original_build
check("trace refresh makes zero provider/network calls",
      refresh_result.get("no_op") or refresh_result["run"]["status"]
      == "done")
check("conflict scan makes zero provider/network calls",
      scan_result["status"] in ("done", "partial"))

# -- REST: legacy + additive routes ------------------------------------------
from openmind.main import app  # noqa: E402

paths = {route.path for route in app.routes}
legacy = {"/projects", "/projects/{project_id}",
          "/projects/{project_id}/assets",
          "/projects/{project_id}/documents", "/ingest", "/search", "/ask",
          "/glossary", "/jobs", "/jobs/{job_id}", "/api/health",
          "/projects/{project_id}/knowledge/stats",
          "/projects/{project_id}/semantic/candidates",
          "/projects/{project_id}/semantic/conflicts",
          "/projects/{project_id}/entities",
          "/projects/{project_id}/promotions"}
check("every pre-Phase-6 route is still registered", legacy <= paths)
check("the API still says /projects, not /workspaces",
      not any("/workspaces" in p for p in paths))
trace_routes = {
    "/projects/{project_id}/traceability/policies",
    "/projects/{project_id}/traceability/policy",
    "/projects/{project_id}/traceability/refresh-plan",
    "/projects/{project_id}/traceability/refresh",
    "/projects/{project_id}/traceability/runs",
    "/projects/{project_id}/traceability/runs/{run_id}",
    "/projects/{project_id}/traceability/requirements/{entity_id}",
    "/projects/{project_id}/traceability/code/{entity_id}",
    "/projects/{project_id}/traceability/tests/{entity_id}",
    "/projects/{project_id}/traceability/paths/{trace_id}",
    "/projects/{project_id}/traceability/coverage",
    "/projects/{project_id}/traceability/gaps",
    "/projects/{project_id}/traceability/gaps/{gap_id}",
    "/projects/{project_id}/traceability/gaps/{gap_id}/accept",
    "/projects/{project_id}/traceability/gaps/{gap_id}/dismiss",
    "/projects/{project_id}/traceability/gaps/{gap_id}/reopen",
    "/projects/{project_id}/traceability/orphans/requirements",
    "/projects/{project_id}/traceability/orphans/code",
    "/projects/{project_id}/traceability/orphans/tests",
    "/projects/{project_id}/traceability/orphans/documents",
    "/projects/{project_id}/conflicts/scan-plan",
    "/projects/{project_id}/conflicts/scan",
    "/projects/{project_id}/conflicts",
    "/projects/{project_id}/conflicts/{conflict_id}",
    "/projects/{project_id}/conflict-promotions/plan",
    "/projects/{project_id}/conflict-promotions",
    "/projects/{project_id}/conflicts/{conflict_id}/review",
    "/projects/{project_id}/conflicts/{conflict_id}/accept-risk",
    "/projects/{project_id}/conflicts/{conflict_id}/resolve",
    "/projects/{project_id}/conflicts/{conflict_id}/dismiss",
    "/projects/{project_id}/conflicts/{conflict_id}/reopen",
}
check("every Phase 6 route is registered additively",
      trace_routes <= paths)

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(app)
response = client.get(f"/projects/{pid}/traceability/coverage")
check("REST coverage works", response.status_code == 200
      and response.json()["snapshot"])
response = client.get(
    f"/projects/{pid}/traceability/requirements/"
    f"{objects['requirement']['id']}")
check("REST requirement trace works", response.status_code == 200)
response = client.get(f"/projects/{pid}/conflicts?status=open")
check("REST conflicts bounded listing works",
      response.status_code == 200)
response = client.get("/projects/p_missing/traceability/coverage")
check("REST unknown workspace is a 404-class error",
      response.status_code in (404, 400))
response = client.get(f"/projects/{pid}/semantic/conflicts")
check("the Phase 4 candidate conflicts route is untouched",
      response.status_code == 200)

# -- Skill Bridge untouched and database-independent -------------------------
import inspect  # noqa: E402

from openmind import skill_bridge  # noqa: E402
source = inspect.getsource(skill_bridge)
check("Skill Bridge imports no database or trace module",
      "traceability" not in source and "import db" not in source
      and "from . import db" not in source)

finish()
