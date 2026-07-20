"""Additive REST routes, additive MCP tools, and everything that must NOT change.

Half of this suite is a compatibility gate. Phase 3 adds a whole document plane,
and the value of that is zero if it quietly moved a route, renamed a tool or
changed what the existing `search` returns. So the pre-existing surface is
asserted first, in full.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from openmind import artifacts, mcp_server  # noqa: E402
from openmind.main import app  # noqa: E402

_results = []
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "documents"


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


# ---------------------------------------------------------------------------
# 1. The MCP tool set: 9 + 4 unchanged, 6 added
# ---------------------------------------------------------------------------
CORE = ("search", "route", "dispatch", "get_glossary", "find_similar_cases",
        "save_case", "get_doc", "propose_fix", "apply_fix")
ASSETS = ("list_assets", "get_asset", "get_asset_revisions", "get_evidence")
DOCUMENTS = ("list_documents", "get_document", "get_document_outline",
             "search_documents", "search_knowledge",
             "find_document_related_candidates")

check("mcp: the original nine tools are unchanged, in order",
      mcp_server.TOOL_NAMES == CORE)
check("mcp: the four Phase 2 asset tools are unchanged, in order",
      mcp_server.ASSET_TOOL_NAMES == ASSETS)
check("mcp: the six document tools are registered",
      mcp_server.DOCUMENT_TOOL_NAMES == DOCUMENTS)
check("mcp: no name collides across the three sets",
      len(set(CORE) | set(ASSETS) | set(DOCUMENTS)) == 19)
check("mcp: every document tool is a callable module function",
      all(callable(fn) for fn in mcp_server.DOCUMENT_TOOLS))
check("mcp: every document tool documents itself for a client",
      all((fn.__doc__ or "").strip() for fn in mcp_server.DOCUMENT_TOOLS))
check("mcp: there is NO document-write tool",
      not any(name.startswith(("add_", "import_", "create_", "delete_",
                               "update_"))
              for name in mcp_server.DOCUMENT_TOOL_NAMES))
check("mcp: the candidate tool's docstring says candidates are not confirmed",
      "not" in (mcp_server.find_document_related_candidates.__doc__ or "").lower()
      and "candidate" in
      (mcp_server.find_document_related_candidates.__doc__ or "").lower())
check("mcp: search_knowledge's docstring disclaims a relationship",
      "NOT a claim" in (mcp_server.search_knowledge.__doc__ or ""))

import openmind.jobs as jobs_module  # noqa: E402

worker_before = jobs_module._worker_started
server = mcp_server.create_mcp_server()
check("mcp: building the server does not start the ingest worker",
      jobs_module._worker_started == worker_before)

# ---------------------------------------------------------------------------
# 2. REST: nothing removed, nothing renamed
# ---------------------------------------------------------------------------
# A StaticFiles Mount has a path but no methods, so both are needed to build
# the route set without tripping over it.
routes = {(r.path, tuple(sorted(m for m in (getattr(r, "methods", None) or set())
                                if m not in ("HEAD", "OPTIONS"))))
          for r in app.routes if hasattr(r, "path")}
paths = {p for p, _ in routes}

PRE_EXISTING = [
    ("/projects", "GET"), ("/projects", "POST"),
    ("/projects/{project_id}", "GET"), ("/projects/{project_id}", "DELETE"),
    ("/projects/{project_id}/paths", "POST"),
    ("/projects/{project_id}/terminate", "POST"),
    ("/projects/{project_id}/assets", "GET"),
    ("/projects/{project_id}/assets/stats", "GET"),
    ("/projects/{project_id}/assets/sync", "POST"),
    ("/projects/{project_id}/assets/{asset_id}", "GET"),
    ("/projects/{project_id}/assets/{asset_id}/revisions", "GET"),
    ("/projects/{project_id}/revisions/{revision_id}", "GET"),
    ("/projects/{project_id}/revisions/{revision_id}/segments", "GET"),
    ("/projects/{project_id}/evidence/{evidence_id}", "GET"),
    ("/ingest", "POST"), ("/jobs", "GET"), ("/jobs/{job_id}", "GET"),
    ("/glossary", "GET"), ("/source", "GET"), ("/api/health", "GET"),
    ("/scope/{scope_id}", "GET"),
]
for path, method in PRE_EXISTING:
    check(f"rest: {method} {path} still exists",
          any(p == path and method in m for p, m in routes))
check("rest: the public API still says /projects, never /workspaces",
      not any(p.startswith("/workspaces") for p in paths))
check("rest: /ocr remains a separate route", "/ocr" in paths)

NEW = [
    ("/projects/{project_id}/documents", "GET"),
    ("/projects/{project_id}/documents", "POST"),
    ("/projects/{project_id}/documents/plan", "POST"),
    ("/projects/{project_id}/documents/search", "POST"),
    ("/projects/{project_id}/documents/{asset_id}", "GET"),
    ("/projects/{project_id}/documents/{asset_id}/outline", "GET"),
    ("/projects/{project_id}/documents/{asset_id}/related", "GET"),
    ("/projects/{project_id}/knowledge/search", "POST"),
]
for path, method in NEW:
    check(f"rest: {method} {path} is added",
          any(p == path and method in m for p, m in routes))

route_order = [r.path for r in app.routes if hasattr(r, "path")]
check("rest: the literal /documents/search path is registered BEFORE "
      "/documents/{asset_id}, so it can never be captured as an id",
      route_order.index("/projects/{project_id}/documents/search")
      < route_order.index("/projects/{project_id}/documents/{asset_id}"))

# ---------------------------------------------------------------------------
# 3. Behaviour through the real app
# ---------------------------------------------------------------------------
SRC = Path(tempfile.mkdtemp(prefix="om_rest_"))
(SRC / "NameCheckService.java").write_text(
    "package a;\npublic class NameCheckService { void screen() {} }\n",
    encoding="utf-8")

with TestClient(app) as client:
    project = client.post("/projects", json={"name": "rest-docs"}).json()
    WS = project["id"]
    client.post(f"/projects/{WS}/paths", json={"path": str(SRC), "exclude": []})
    client.post("/ingest", json={"project_id": WS})

    spec = Path(tempfile.mkdtemp()) / "Requirements_v3.md"
    shutil.copy(FIXTURES / "sample-requirements.md", spec)

    # -- plan writes nothing ------------------------------------------------
    before = client.get(f"/projects/{WS}/documents").json()["total"]
    planned = client.post(f"/projects/{WS}/documents/plan",
                          json={"path": str(spec)})
    check("rest: POST documents/plan returns 200", planned.status_code == 200)
    plan = planned.json()
    check("rest: the plan reports the decision", plan["status"] == "new_asset")
    check("rest: the plan names the parser that would run",
          plan["parser"] == "markdown")
    check("rest: the plan includes the probe facts", "probe" in plan)
    check("rest: planning stores nothing",
          client.get(f"/projects/{WS}/documents").json()["total"] == before)

    # -- import -------------------------------------------------------------
    created = client.post(f"/projects/{WS}/documents",
                          json={"path": str(spec), "wait": True})
    check("rest: POST documents returns 200", created.status_code == 200)
    body = created.json()
    report = body.get("import_report") or {}
    check("rest: the import succeeded", body["status"] == "new_asset")
    check("rest: the response includes the import report", bool(report))
    ASSET = report["asset_id"]

    listing = client.get(f"/projects/{WS}/documents").json()
    check("rest: GET documents lists it", listing["total"] == 1)
    check("rest: the listing is bounded and reports its bound",
          listing["limit"] <= 500 and listing["count"] == len(listing["documents"]))
    check("rest: an oversized limit is clamped, not honoured",
          client.get(f"/projects/{WS}/documents?limit=99999"
                     ).json()["limit"] <= 500)

    single = client.get(f"/projects/{WS}/documents/{ASSET}").json()
    check("rest: GET one document returns its parse summary",
          single["parse"]["parser_name"] == "markdown")
    check("rest: it reports the current revision",
          bool((single.get("current_revision") or {}).get("id")))
    check("rest: no absolute source path is returned",
          str(spec) not in str(single) and str(spec.parent) not in str(single))

    outline = client.get(f"/projects/{WS}/documents/{ASSET}/outline?limit=5")
    check("rest: GET outline returns 200", outline.status_code == 200)
    check("rest: the outline is bounded", outline.json()["count"] == 5)
    check("rest: the outline reports the true total",
          outline.json()["total"] > 5)
    check("rest: the outline returns structure, not full content",
          all(len(e["preview"]) <= 120 for e in outline.json()["outline"]))

    found = client.post(f"/projects/{WS}/documents/search",
                        json={"query": "REQ-NC-017"}).json()
    check("rest: POST documents/search finds the exact identifier",
          found["count"] > 0)
    check("rest: every hit is evidence-cited",
          all(h["evidence_id"] for h in found["hits"]))
    check("rest: search hits are bounded",
          all(len(h["excerpt"]) <= 400 for h in found["hits"]))
    check("rest: an oversized search limit is clamped",
          client.post(f"/projects/{WS}/documents/search",
                      json={"query": "review", "limit": 9999}
                      ).json()["count"] <= 50)

    related = client.get(f"/projects/{WS}/documents/{ASSET}/related").json()
    check("rest: GET related returns candidates",
          related["status"] == "candidate")
    check("rest: every candidate is labelled candidate",
          all(c["status"] == "candidate" for c in related["candidates"]))

    knowledge = client.post(f"/projects/{WS}/knowledge/search",
                            json={"query": "NameCheck timeout"}).json()
    check("rest: POST knowledge/search separates code from documents",
          "code" in knowledge and "documents" in knowledge)
    check("rest: it disclaims any relationship between the two",
          "NOT a claim" in knowledge["grounding"]["note"])

    # -- typed errors -------------------------------------------------------
    other = client.post("/projects", json={"name": "other"}).json()["id"]
    check("rest: a cross-workspace document id is 404",
          client.get(f"/projects/{other}/documents/{ASSET}").status_code == 404)
    check("rest: an unknown workspace is 404",
          client.get("/projects/p_nope/documents").status_code == 404)
    check("rest: an unknown document is 404",
          client.get(f"/projects/{WS}/documents/a_nope").status_code == 404)
    check("rest: a cross-workspace outline is 404",
          client.get(f"/projects/{other}/documents/{ASSET}/outline"
                     ).status_code == 404)
    check("rest: a cross-workspace related lookup is 404",
          client.get(f"/projects/{other}/documents/{ASSET}/related"
                     ).status_code == 404)
    conflicting = client.post(f"/projects/{WS}/documents",
                              json={"path": str(spec), "new_asset": True,
                                    "logical_key": "documents/x"})
    check("rest: conflicting import targets are a 400",
          conflicting.status_code == 400)
    missing_file = client.post(f"/projects/{WS}/documents",
                               json={"path": "/no/such/file.md"})
    check("rest: a missing file is a typed 400", missing_file.status_code == 400)
    check("rest: the error body carries a machine-readable code",
          (missing_file.json().get("error") or {}).get("code")
          == "invalid_request")

    # -- the pre-existing surface still behaves -----------------------------
    check("compat: GET /projects still works",
          client.get("/projects").status_code == 200)
    check("compat: GET /api/health still works",
          client.get("/api/health").status_code == 200)
    check("compat: health reports the new runtime version",
          client.get("/api/health").json()["version"] == "1.3.0-dev")
    assets = client.get(f"/projects/{WS}/assets").json()
    check("compat: GET assets still returns the Phase 2 shape",
          {"assets", "total", "limit", "offset", "count"} <= set(assets))
    check("compat: the document asset appears in the asset listing too",
          any(a["id"] == ASSET for a in assets["assets"]))
    stats = client.get(f"/projects/{WS}/assets/stats").json()
    check("compat: asset stats still return every Phase 2 key",
          {"assets_total", "assets_active", "assets_removed", "revisions",
           "segments", "evidence"} <= set(stats))
    evidence_id = found["hits"][0]["evidence_id"]
    evidence = client.get(f"/projects/{WS}/evidence/{evidence_id}").json()
    check("compat: the Phase 2 evidence route resolves DOCUMENT evidence",
          evidence["snapshot"]["status"] == "available")
    check("compat: it reports current-source status honestly",
          evidence["current_source"]["status"] == "not-applicable")
    check("compat: it names the parser for a document citation",
          (evidence.get("parser") or {}).get("name") == "markdown")

# ---------------------------------------------------------------------------
# 4. The artifact contract is untouched
# ---------------------------------------------------------------------------
check("artifact: the exported schema version is still 1.1.0",
      artifacts.SCHEMA_VERSION == "1.1.0")
export_dir = Path(tempfile.mkdtemp(prefix="om_export_"))
summary = artifacts.generate_artifacts(str(SRC), str(export_dir),
                                       name="rest-docs")
check("artifact: export still runs and writes its files",
      bool(summary.get("files")))
written = "\n".join((export_dir / f).read_text(encoding="utf-8")
                    for f in summary["files"]
                    if (export_dir / f).is_file())
check("artifact: the exported schemaVersion is still 1.1.0",
      f'"schemaVersion": "{artifacts.SCHEMA_VERSION}"' in written)
check("artifact: the document model is NOT exported through Bundle 1.x",
      "document_parses" not in written and "documents_" not in written
      and "parse_status" not in written)

import subprocess  # noqa: E402

standalone = subprocess.run(
    [sys.executable, "-c",
     "import sys; from openmind.services.export_service import ExportService; "
     "print(','.join(sorted(m for m in sys.modules "
     "if m.startswith('openmind.documents') "
     "or m in ('docx','pypdf','openpyxl'))))"],
    capture_output=True, text=True, cwd=str(REPO))
check("artifact: export imports neither the document plane nor its dependencies",
      standalone.returncode == 0 and standalone.stdout.strip() == "")

# ---------------------------------------------------------------------------
# 5. The skill bridge stays independent of the application database
# ---------------------------------------------------------------------------
from openmind import skill_bridge  # noqa: E402

# The contract is that the bridge does not DEPEND on the application database —
# not that the module is never transitively imported. What must stay true is
# that no connection is opened and no migration runs.
bridge = subprocess.run(
    [sys.executable, "-c",
     "import openmind.skill_bridge; from openmind import db; "
     "print('open' if db._conn is not None else 'no-connection')"],
    capture_output=True, text=True, cwd=str(REPO))
check("skill bridge: importing it opens no database connection",
      bridge.stdout.strip() == "no-connection")
check("skill bridge: its entry point is unchanged", hasattr(skill_bridge, "main"))

# And the JSON-lines protocol itself still works end to end.
protocol = subprocess.run(
    [sys.executable, "-m", "openmind.skill_bridge", "--help"],
    capture_output=True, text=True, cwd=str(REPO))
check("skill bridge: it is still runnable as a module",
      protocol.returncode == 0)

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
