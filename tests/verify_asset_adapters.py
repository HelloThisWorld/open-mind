"""Asset model adapters — additive REST endpoints and read-only MCP tools,
with every existing route/tool still present and cross-workspace reads refused.
"""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from fastapi.testclient import TestClient  # noqa: E402

from openmind import main, mcp_server  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "fixtures", "sample-repo")

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc
          + (f"  [{extra}]" if extra and not cond else ""))


# ---------------------------------------------------------------------------
# Set-up: a real ingested workspace + an empty second workspace.
# ---------------------------------------------------------------------------
runtime = get_runtime()
runtime.ensure_worker()
WS = runtime.workspaces.create("adapters-assets", path=FIXTURE)["id"]
WS2 = runtime.workspaces.create("adapters-other")["id"]
runtime.ingest.start(WS, wait=True, timeout=180)

client = TestClient(main.app)

# ---------------------------------------------------------------------------
# Existing REST routes still operational (a representative sample).
# ---------------------------------------------------------------------------
for path in ("/projects", f"/projects/{WS}", "/api/health", "/templates"):
    r = client.get(path)
    check(f"existing REST route still works: GET {path}", r.status_code == 200)
check("GET /projects is still shaped {projects:[...]} (not renamed)",
      "projects" in client.get("/projects").json())

# ---------------------------------------------------------------------------
# Existing MCP tools still present, unchanged; new asset tools added.
# ---------------------------------------------------------------------------
server = mcp_server.create_mcp_server(runtime)
tool_names = {t.name for t in asyncio.run(server.list_tools())}
CORE = {"search", "route", "dispatch", "get_glossary", "find_similar_cases",
        "save_case", "get_doc", "propose_fix", "apply_fix"}
ASSET = {"list_assets", "get_asset", "get_asset_revisions", "get_evidence"}
# v2 Phase 3 adds six read-only document tools alongside these, and v2
# Phase 4 seven read-only semantic/lens tools.
DOCUMENT = {"list_documents", "get_document", "get_document_outline",
            "search_documents", "search_knowledge",
            "find_document_related_candidates"}
SEMANTIC = {"list_semantic_runs", "get_semantic_run",
            "list_semantic_candidates", "get_semantic_candidate",
            "list_project_lenses", "get_project_lens", "get_semantic_usage"}
check("all nine core MCP tools remain", CORE <= tool_names)
check("the four read-only asset MCP tools are added", ASSET <= tool_names)
check("every registered tool is an accounted-for addition",
      tool_names == CORE | ASSET | DOCUMENT | SEMANTIC)
check("the MCP server keeps its name", server.name == "open-mind")

# ---------------------------------------------------------------------------
# New REST asset endpoints.
# ---------------------------------------------------------------------------
r = client.get(f"/projects/{WS}/assets")
check("GET /assets exits 200", r.status_code == 200)
listing = r.json()
check("GET /assets returns a bounded page with a total",
      "assets" in listing and "total" in listing and listing["total"] >= 1)

r = client.get(f"/projects/{WS}/assets/stats")
check("GET /assets/stats exits 200 (literal path not shadowed by {asset_id})",
      r.status_code == 200 and "assets_total" in r.json())

ASSET_ID = listing["assets"][0]["id"]
r = client.get(f"/projects/{WS}/assets/{ASSET_ID}")
check("GET /assets/{id} exits 200 with a current revision",
      r.status_code == 200 and r.json().get("current_revision"))
REV_ID = r.json()["current_revision"]["id"]

r = client.get(f"/projects/{WS}/assets/{ASSET_ID}/revisions")
check("GET /assets/{id}/revisions exits 200", r.status_code == 200 and r.json()["count"] >= 1)

r = client.get(f"/projects/{WS}/revisions/{REV_ID}")
check("GET /revisions/{id} exits 200", r.status_code == 200)

r = client.get(f"/projects/{WS}/revisions/{REV_ID}/segments")
segs = r.json()
check("GET /revisions/{id}/segments exits 200 and is bounded",
      r.status_code == 200 and "limit" in segs and segs["total"] >= 1)
EV_ID = segs["segments"][0]["evidence_id"]

r = client.get(f"/projects/{WS}/evidence/{EV_ID}", params={"max_chars": 40})
ev = r.json()
check("GET /evidence/{id} exits 200", r.status_code == 200)
check("evidence content is bounded by max_chars", len(ev["content"]) <= 40)
check("evidence reports snapshot + current-source state",
      "snapshot" in ev and "current_source" in ev)
check("evidence locator is source-traceable (workspace-relative)",
      not str(ev["locator"].get("file", "")).startswith("/")
      and ev["locator"].get("startLine", 0) >= 1)

# ---------------------------------------------------------------------------
# Cross-workspace reads are refused (404), not leaked.
# ---------------------------------------------------------------------------
check("cross-workspace GET /assets/{id} -> 404",
      client.get(f"/projects/{WS2}/assets/{ASSET_ID}").status_code == 404)
check("cross-workspace GET /revisions/{id} -> 404",
      client.get(f"/projects/{WS2}/revisions/{REV_ID}").status_code == 404)
check("cross-workspace GET /evidence/{id} -> 404",
      client.get(f"/projects/{WS2}/evidence/{EV_ID}").status_code == 404)
check("unknown workspace GET /assets -> 404",
      client.get("/projects/p_nope00000000/assets").status_code == 404)

# ---------------------------------------------------------------------------
# New MCP asset tools work and are workspace-scoped.
# ---------------------------------------------------------------------------
mla = mcp_server.list_assets(WS, limit=5)
check("MCP list_assets returns a bounded set", mla["total"] >= 1 and mla["count"] <= 5)
mid = mla["assets"][0]["id"]
ma = mcp_server.get_asset(WS, mid)
check("MCP get_asset returns the asset", ma["id"] == mid)
mrevs = mcp_server.get_asset_revisions(WS, mid)
check("MCP get_asset_revisions returns history", mrevs["count"] >= 1)
mev = mcp_server.get_evidence(WS, EV_ID)
check("MCP get_evidence returns a workspace-relative locator",
      not str(mev["locator"].get("file", "")).startswith("/"))
check("MCP get_evidence states snapshot provenance",
      "snapshot" in mev and "current_source" in mev)

# cross-scope MCP reads are refused
from openmind.domain.errors import AssetNotFound, EvidenceNotFound  # noqa: E402
_scoped = False
try:
    mcp_server.get_asset(WS2, mid)
except AssetNotFound:
    _scoped = True
check("MCP get_asset refuses a cross-workspace id", _scoped)
_scoped_ev = False
try:
    mcp_server.get_evidence(WS2, EV_ID)
except EvidenceNotFound:
    _scoped_ev = True
check("MCP get_evidence refuses a cross-workspace id", _scoped_ev)

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
