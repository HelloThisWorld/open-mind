"""Phase 5 adapter + compatibility gate — the 26 existing MCP tools remain,
EXACTLY nine read-only graph tools are added (35 total), knowledge REST
routes are additive and workspace-scoped, existing routes/commands survive,
and the Phase 3/4 surfaces are untouched."""
import json
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_confirmed_candidate,
                                make_minimal_workspace)

# ---------------------------------------------------------------------------
# 1. MCP: 26 unchanged + exactly 9 read-only graph tools = 35
# ---------------------------------------------------------------------------
from openmind import mcp_server  # noqa: E402

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
check("the seven semantic/lens tools are unchanged",
      mcp_server.SEMANTIC_TOOL_NAMES == (
          "list_semantic_runs", "get_semantic_run",
          "list_semantic_candidates", "get_semantic_candidate",
          "list_project_lenses", "get_project_lens",
          "get_semantic_usage"))
check("EXACTLY the nine graph tools are added",
      mcp_server.KNOWLEDGE_TOOL_NAMES == (
          "get_graph_stats", "search_graph", "get_graph_node",
          "expand_graph", "find_graph_path", "list_engineering_entities",
          "get_engineering_entity", "get_engineering_claim",
          "get_engineering_relation"))
total = (len(mcp_server.TOOLS) + len(mcp_server.ASSET_TOOLS)
         + len(mcp_server.DOCUMENT_TOOLS) + len(mcp_server.SEMANTIC_TOOLS)
         + len(mcp_server.KNOWLEDGE_TOOLS))
check("the Phase 1-5 MCP set is 35 tools", total == 35)
forbidden = {"promote_candidate", "promote_relation", "create_entity",
             "create_claim", "create_relation", "merge_entities",
             "split_entity", "set_authority", "seed_graph", "sync_graph",
             "export_bundle"}
all_names = set(mcp_server.TOOL_NAMES + mcp_server.ASSET_TOOL_NAMES
                + mcp_server.DOCUMENT_TOOL_NAMES
                + mcp_server.SEMANTIC_TOOL_NAMES
                + mcp_server.KNOWLEDGE_TOOL_NAMES)
check("no promote/create/merge/authority/seed/export tool exists on MCP",
      not (forbidden & all_names))

import asyncio  # noqa: E402
server = mcp_server.create_mcp_server()
registered = asyncio.new_event_loop().run_until_complete(server.list_tools())
# Phase 6 registers eight additive read-only trace/conflict tools beside
# these 35; verify_traceability_adapters accounts for each by name.
check("FastMCP registers the 35 Phase 1-5 tools (43 with the trace set)",
      len(registered) == 35 + len(mcp_server.TRACE_TOOLS)
      and len(mcp_server.TRACE_TOOLS) == 8)

# ---------------------------------------------------------------------------
# 2. REST: legacy + Phase 3/4 routes intact; knowledge routes additive
# ---------------------------------------------------------------------------
from openmind.main import app  # noqa: E402

paths = {route.path for route in app.routes}
legacy = {"/projects", "/projects/{project_id}",
          "/projects/{project_id}/assets",
          "/projects/{project_id}/documents", "/ingest", "/search", "/ask",
          "/glossary", "/jobs", "/jobs/{job_id}", "/api/health",
          "/projects/{project_id}/knowledge/search",
          "/projects/{project_id}/semantic/candidates",
          "/projects/{project_id}/lenses"}
check("every pre-Phase-5 route is still registered", legacy <= paths)
check("the API still says /projects, not /workspaces",
      not any("/workspaces" in p for p in paths))
knowledge_routes = {
    "/projects/{project_id}/knowledge/stats",
    "/projects/{project_id}/knowledge/revisions",
    "/projects/{project_id}/knowledge/revisions/{revision_number}",
    "/projects/{project_id}/knowledge/decisions",
    "/projects/{project_id}/knowledge/seed/plan",
    "/projects/{project_id}/knowledge/seed",
    "/projects/{project_id}/knowledge/sync",
    "/projects/{project_id}/knowledge/reconcile",
    "/projects/{project_id}/knowledge/graph-search",
    "/projects/{project_id}/knowledge/nodes/{node_id}",
    "/projects/{project_id}/knowledge/nodes/{node_id}/expand",
    "/projects/{project_id}/knowledge/path",
    "/projects/{project_id}/entities",
    "/projects/{project_id}/entities/{entity_id}",
    "/projects/{project_id}/entities/{entity_id}/aliases",
    "/projects/{project_id}/entities/{entity_id}/merge",
    "/projects/{project_id}/entities/{entity_id}/split",
    "/projects/{project_id}/entities/{entity_id}/authority",
    "/projects/{project_id}/claims",
    "/projects/{project_id}/claims/{claim_id}",
    "/projects/{project_id}/relations",
    "/projects/{project_id}/relations/{relation_id}",
    "/projects/{project_id}/promotions/plan",
    "/projects/{project_id}/promotions",
    "/projects/{project_id}/relation-promotions/plan",
    "/projects/{project_id}/relation-promotions",
}
check("all additive knowledge routes are registered",
      knowledge_routes <= paths)

# ---------------------------------------------------------------------------
# 3. REST end-to-end through the TestClient
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
pid = make_minimal_workspace(runtime, "kg-adapters")
evidence_id = find_evidence(pid, "REQ-NC-017")
candidate_id = make_confirmed_candidate(runtime, pid)
client = TestClient(app)

response = client.post(f"/projects/{pid}/promotions/plan",
                       json={"candidate_id": candidate_id})
check("REST promotion plan works",
      response.status_code == 200
      and response.json()["plan"]["eligible"])
response = client.post(f"/projects/{pid}/promotions",
                       json={"candidate_id": candidate_id,
                             "actor": "rest-rev", "note": "via REST"})
check("REST promotion succeeds",
      response.status_code == 200
      and response.json()["status"] == "promoted")
entity_id = response.json()["entity"]["id"]

response = client.get(f"/projects/{pid}/knowledge/stats")
check("REST knowledge stats", response.status_code == 200
      and response.json()["entities_active"] >= 1)
response = client.get(f"/projects/{pid}/entities")
check("REST entity listing", response.status_code == 200
      and response.json()["count"] >= 1)
response = client.get(f"/projects/{pid}/entities/{entity_id}")
check("REST entity detail includes claims",
      response.status_code == 200
      and response.json()["entity"]["claims"])
claim_id = response.json()["entity"]["claims"][0]["id"]
response = client.get(f"/projects/{pid}/claims/{claim_id}")
check("REST claim detail includes evidence",
      response.status_code == 200
      and response.json()["claim"]["evidence"])

response = client.post(f"/projects/{pid}/entities",
                       json={"entity_type": "business-rule",
                             "canonical_key": "business-rule:BR-REST",
                             "display_name": "BR-REST",
                             "evidence": [{"evidence_id": evidence_id}],
                             "actor": "rest", "note": "created"})
check("REST entity creation", response.status_code == 200)
second_id = response.json()["entity"]["id"]
response = client.post(f"/projects/{pid}/relations",
                       json={"source_entity_id": second_id,
                             "target_entity_id": entity_id,
                             "relation_type": "refines",
                             "relation_state": "confirmed",
                             "evidence": [{"evidence_id": evidence_id}],
                             "actor": "rest", "note": "rel"})
check("REST relation creation", response.status_code == 200)
relation_id = response.json()["relation"]["id"]
response = client.get(f"/projects/{pid}/relations/{relation_id}")
check("REST relation detail", response.status_code == 200)

response = client.post(f"/projects/{pid}/knowledge/graph-search",
                       json={"query": "REQ-NC-017"})
check("REST graph search finds the promoted entity",
      response.status_code == 200
      and response.json()["entities"][0]["canonical_key"]
      == "requirement:REQ-NC-017")
response = client.get(f"/projects/{pid}/knowledge/nodes/{entity_id}")
check("REST node lookup", response.status_code == 200
      and response.json()["node"]["nodeKind"] == "entity")
response = client.post(
    f"/projects/{pid}/knowledge/nodes/{entity_id}/expand",
    json={"depth": 2})
check("REST expansion", response.status_code == 200
      and "truncated" in response.json())
response = client.post(f"/projects/{pid}/knowledge/path",
                       json={"source": second_id, "target": entity_id})
check("REST path discovery", response.status_code == 200
      and response.json()["outcome"] == "found")
response = client.get(f"/projects/{pid}/knowledge/revisions")
check("REST revision ledger", response.status_code == 200
      and response.json()["revisions"])
response = client.get(f"/projects/{pid}/knowledge/decisions")
check("REST decision audit", response.status_code == 200
      and response.json()["decisions"])

# workspace scoping through REST: another workspace cannot see the entity
other = make_minimal_workspace(runtime, name="kg-adapters-other")
response = client.get(f"/projects/{other}/entities/{entity_id}")
check("REST blocks cross-workspace entity access",
      response.status_code == 404)
response = client.get(f"/projects/p_missing/knowledge/stats")
check("REST unknown workspace is a typed 404",
      response.status_code == 404
      and response.json()["error"]["code"] == "workspace_not_found")

# the Phase 3 combined knowledge search is untouched beside graph-search
response = client.post(f"/projects/{pid}/knowledge/search",
                       json={"query": "REQ-NC-017"})
check("Phase 3 POST /knowledge/search still answers with code+documents",
      response.status_code == 200
      and "documents" in response.json())

# ---------------------------------------------------------------------------
# 4. MCP graph tools end-to-end (read-only)
# ---------------------------------------------------------------------------
stats = mcp_server.get_graph_stats(pid)
check("MCP get_graph_stats", stats["entities_active"] >= 1)
found = mcp_server.search_graph(pid, "REQ-NC-017")
check("MCP search_graph returns separate entity/claim sections",
      found["entities"] and isinstance(found["claims"], list))
node = mcp_server.get_graph_node(pid, entity_id)
check("MCP get_graph_node", node["node"]["nodeKind"] == "entity")
expansion = mcp_server.expand_graph(pid, entity_id, depth=1)
check("MCP expand_graph bounded", "truncated" in expansion)
path = mcp_server.find_graph_path(pid, second_id, entity_id)
check("MCP find_graph_path", path["outcome"] == "found")
entities = mcp_server.list_engineering_entities(pid)
check("MCP list_engineering_entities", entities["count"] >= 1)
detail = mcp_server.get_engineering_entity(pid, entity_id)
check("MCP get_engineering_entity", detail["entity"]["id"] == entity_id)
claim_detail = mcp_server.get_engineering_claim(pid, claim_id)
check("MCP get_engineering_claim carries evidence",
      claim_detail["claim"]["evidence"])
relation_detail = mcp_server.get_engineering_relation(pid, relation_id)
check("MCP get_engineering_relation carries state",
      relation_detail["relation"]["relation_state"] == "confirmed")

# ---------------------------------------------------------------------------
# 5. Cross-surface invariants
# ---------------------------------------------------------------------------
from openmind import artifacts  # noqa: E402

check("artifact schema stays 1.1.0", artifacts.SCHEMA_VERSION == "1.1.0")
import openmind.skill_bridge as skill_bridge  # noqa: E402
import inspect  # noqa: E402

bridge_source = inspect.getsource(skill_bridge)
check("skill bridge remains database-independent",
      "import db" not in bridge_source
      and "from . import db" not in bridge_source
      and "knowledge" not in bridge_source)

# semantic review does not promote (adapter-level restatement)
from openmind.knowledge import store as kg_store  # noqa: E402
fresh = make_confirmed_candidate(runtime, other, confirm=False)
entities_before = kg_store.count_entities(other)
client.post(f"/projects/{other}/semantic/candidates/{fresh}/review",
            json={"decision": "confirm", "kind": "candidate"})
check("REST semantic review still creates no canonical entity",
      kg_store.count_entities(other) == entities_before)

finish()
