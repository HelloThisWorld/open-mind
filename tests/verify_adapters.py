"""Adapter compatibility — FastAPI routes, MCP construction, and the skill bridge.

Phase 1 moved orchestration out of the route bodies and into the service layer.
The point of this suite is that NOTHING externally visible changed: the same
paths, the same methods, the same status codes, the same response keys, the same
nine MCP tools, and the same skill-bridge protocol.
"""
import json
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from fastapi.testclient import TestClient  # noqa: E402

from openmind.main import app  # noqa: E402
from openmind.version import RUNTIME_VERSION  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "fixtures", "sample-repo")

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc
          + (f"  [{extra}]" if extra and not cond else ""))


# ---------------------------------------------------------------------------
# The published route table must not shrink or drift
# ---------------------------------------------------------------------------
ROUTES = {(m, r.path) for r in app.routes
          for m in getattr(r, "methods", set()) or set()}

EXPECTED = [
    ("GET", "/"), ("GET", "/api/health"), ("GET", "/netlog"),
    ("GET", "/system/load"),
    ("GET", "/projects"), ("POST", "/projects"),
    ("GET", "/projects/{project_id}"), ("DELETE", "/projects/{project_id}"),
    ("POST", "/projects/{project_id}/paths"),
    ("DELETE", "/projects/{project_id}/paths"),
    ("POST", "/projects/{project_id}/selection"),
    ("GET", "/projects/{project_id}/template"),
    ("POST", "/projects/{project_id}/template"),
    ("POST", "/projects/{project_id}/source-link"),
    ("POST", "/projects/{project_id}/terminate"),
    ("GET", "/templates"), ("GET", "/scope/{scope_id}"),
    ("GET", "/fs/tree"), ("GET", "/fs/list"), ("GET", "/fs/pick-folder"),
    ("GET", "/model-config"), ("POST", "/model-config"),
    ("POST", "/server/start"), ("POST", "/server/stop"),
    ("POST", "/server/restart"), ("GET", "/server/status"),
    ("POST", "/ingest"), ("POST", "/gendocs"),
    ("GET", "/jobs"), ("GET", "/jobs/{job_id}"),
    ("POST", "/jobs/{job_id}/pause"), ("POST", "/jobs/{job_id}/resume"),
    ("POST", "/jobs/{job_id}/cancel"), ("GET", "/jobs/{job_id}/stream"),
    ("GET", "/glossary"), ("POST", "/glossary/enrich"),
    ("POST", "/glossary/enrich/auto"),
    ("GET", "/source"), ("GET", "/structure"), ("GET", "/route"),
    ("GET", "/dispatch"), ("GET", "/graph"), ("GET", "/graph/children"),
    ("GET", "/graph/node"),
    ("POST", "/search"), ("POST", "/ocr"),
    ("POST", "/ask"), ("GET", "/ask/history"),
    ("GET", "/ask/exchange/{exchange_id}"), ("POST", "/ask/clear"),
    ("POST", "/ask/save-case"),
    ("POST", "/cases"), ("GET", "/cases"),
    ("GET", "/docs"), ("GET", "/docs/{page}"),
]
missing = [f"{m} {p}" for m, p in EXPECTED if (m, p) not in ROUTES]
check(f"all {len(EXPECTED)} pre-existing routes are still registered",
      not missing, "missing: " + ", ".join(missing))
check("/projects was NOT renamed to /workspaces in the public API",
      not any(p.startswith("/workspaces") for _m, p in ROUTES))

with TestClient(app) as c:
    # -----------------------------------------------------------------------
    # Health — additive only
    # -----------------------------------------------------------------------
    r = c.get("/api/health")
    health = r.json()
    check("GET /api/health is 200", r.status_code == 200)
    for key in ("ok", "embeddings_backend", "vectorstore_backend",
                "llm_base_url", "llm_local", "outbound_calls_logged", "server"):
        check(f"/api/health still returns the pre-existing key '{key}'",
              key in health)
    check("/api/health now also reports the runtime version",
          health.get("version") == RUNTIME_VERSION)
    check("/api/health now also reports the schema version",
          health.get("schema_version", 0) >= 2)
    check("/api/health stays cheap by default (no diagnostics probe)",
          "diagnostics" not in health)

    r = c.get("/api/health", params={"diagnostics": "1"})
    full = r.json()
    check("/api/health?diagnostics=1 exposes the doctor report",
          isinstance(full.get("diagnostics", {}).get("checks"), list))
    check("the HTTP diagnostics report matches the doctor contract",
          full["diagnostics"].get("version") == RUNTIME_VERSION
          and "ok" in full["diagnostics"])

    # -----------------------------------------------------------------------
    # Projects — created through the service, unchanged over HTTP
    # -----------------------------------------------------------------------
    r = c.post("/projects", json={"name": "adapter demo"})
    pid = r.json()["id"]
    check("POST /projects is 200 and returns a project record",
          r.status_code == 200 and pid.startswith("p_"))
    for key in ("id", "name", "state", "paths", "created_at", "updated_at", "meta"):
        check(f"POST /projects still returns '{key}'", key in r.json())

    r = c.post("/projects", json={"name": "with path", "path": FIXTURE,
                                  "exclude": ["target"]})
    check("POST /projects registers an initial path",
          r.status_code == 200 and len(r.json()["paths"]) == 1)
    check("POST /projects records the exclude set",
          r.json()["paths"][0]["exclude"] == ["target"])
    pid_with_path = r.json()["id"]

    r = c.get("/projects")
    check("GET /projects lists projects",
          r.status_code == 200 and isinstance(r.json()["projects"], list))

    r = c.get(f"/projects/{pid}")
    check("GET /projects/{id} is 200", r.status_code == 200)
    for key in ("code_chunks", "cases_count", "files_indexed"):
        check(f"GET /projects/{{id}} still decorates the record with '{key}'",
              key in r.json())

    r = c.get("/projects/p_does_not_exist")
    check("GET /projects/{id} 404s for an unknown project", r.status_code == 404)
    check("the 404 body still carries 'detail' (FastAPI's shape)",
          "detail" in r.json())
    check("the 404 body ALSO carries a machine-readable error code",
          r.json().get("error", {}).get("code") == "workspace_not_found")

    # -- paths
    r = c.post(f"/projects/{pid}/paths", json={"path": FIXTURE, "exclude": []})
    check("POST /projects/{id}/paths is 200", r.status_code == 200)
    check("POST /projects/{id}/paths returns the updated project",
          len(r.json()["paths"]) == 1)
    r = c.post("/projects/p_nope/paths", json={"path": FIXTURE, "exclude": []})
    check("POST /projects/{id}/paths 404s for an unknown project",
          r.status_code == 404)

    r = c.post(f"/projects/{pid}/selection",
               json={"path": FIXTURE, "exclude": ["build"]})
    check("POST /projects/{id}/selection still works", r.status_code == 200)
    check("selection updates the exclude set",
          r.json()["paths"][0]["exclude"] == ["build"])

    r = c.request("DELETE", f"/projects/{pid}/paths", params={"path": FIXTURE})
    check("DELETE /projects/{id}/paths is 200", r.status_code == 200)
    check("DELETE /projects/{id}/paths removes the path", r.json()["paths"] == [])
    # Before Phase 1 this returned None from a handler annotated -> Dict, which
    # FastAPI's response validation turned into a 500 ResponseValidationError.
    r = c.request("DELETE", "/projects/p_nope/paths", params={"path": FIXTURE})
    check("DELETE /projects/{id}/paths now 404s instead of raising a 500 "
          "(documented behaviour fix)", r.status_code == 404, str(r.status_code))

    # -- templates
    r = c.get("/templates")
    check("GET /templates lists profiles",
          r.status_code == 200 and "templates" in r.json() and "user_dir" in r.json())

    r = c.get(f"/projects/{pid}/template")
    check("GET /projects/{id}/template is 200", r.status_code == 200)
    for key in ("override", "override_error", "auto", "effective",
                "effective_source"):
        check(f"template selection still returns '{key}'", key in r.json())
    check("GET /projects/{id}/template 404s for an unknown project",
          c.get("/projects/p_nope/template").status_code == 404)

    r = c.post(f"/projects/{pid}/template", json={"name": "nope"})
    check("POST an unknown template name is still a 400", r.status_code == 400,
          str(r.status_code))
    check("the 400 body still carries 'detail'", "detail" in r.json())
    r = c.post(f"/projects/{pid}/template", json={"name": None})
    check("POST template name=null clears the override",
          r.status_code == 200 and r.json()["override"] is None)

    # -----------------------------------------------------------------------
    # Ingest + jobs
    # -----------------------------------------------------------------------
    r = c.post("/ingest", json={"project_id": pid_with_path})
    check("POST /ingest is 200", r.status_code == 200)
    check("POST /ingest still returns {job_id, job}",
          "job_id" in r.json() and "job" in r.json())
    job_id = r.json()["job_id"]
    check("the enqueued job carries the pre-existing job-record keys",
          all(k in r.json()["job"] for k in
              ("job_id", "project_id", "type", "status", "step", "progress",
               "log_tail", "control", "error", "created_at", "updated_at")))

    check("POST /ingest 404s for an unknown project",
          c.post("/ingest", json={"project_id": "p_nope"}).status_code == 404)
    check("POST /gendocs 404s for an unknown project",
          c.post("/gendocs", json={"project_id": "p_nope"}).status_code == 404)

    r = c.get("/jobs", params={"scope": pid_with_path})
    check("GET /jobs?scope= lists that project's jobs",
          r.status_code == 200 and any(j["job_id"] == job_id
                                       for j in r.json()["jobs"]))
    r = c.get("/jobs", params={"scope": "p_nope"})
    check("GET /jobs with an unresolvable scope lists nothing "
          "(does not fall back to every project)",
          r.status_code == 200 and r.json()["jobs"] == [])

    r = c.get(f"/jobs/{job_id}")
    check("GET /jobs/{id} is 200", r.status_code == 200)
    check("GET /jobs/{id} 404s for an unknown job",
          c.get("/jobs/job_nope").status_code == 404)
    check("POST /jobs/{id}/pause 404s for an unknown job",
          c.post("/jobs/job_nope/pause").status_code == 404)
    check("POST /jobs/{id}/resume 404s for an unknown job",
          c.post("/jobs/job_nope/resume").status_code == 404)
    check("POST /jobs/{id}/cancel 404s for an unknown job",
          c.post("/jobs/job_nope/cancel").status_code == 404)

    r = c.post(f"/jobs/{job_id}/cancel")
    check("POST /jobs/{id}/cancel is 200 for a real job", r.status_code == 200)

    # -----------------------------------------------------------------------
    # Terminate / delete
    # -----------------------------------------------------------------------
    check("POST /projects/{id}/terminate 404s for an unknown project",
          c.post("/projects/p_nope/terminate",
                 json={"clear_cases": False}).status_code == 404)
    check("DELETE /projects/{id} 404s for an unknown project",
          c.delete("/projects/p_nope").status_code == 404)

    r = c.delete(f"/projects/{pid}")
    check("DELETE /projects/{id} still returns {deleting} immediately",
          r.status_code == 200 and r.json().get("deleting") == pid)
    check("a deleted project is 404 at once",
          c.get(f"/projects/{pid}").status_code == 404)

# ---------------------------------------------------------------------------
# MCP construction
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

from openmind import mcp_server  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

REQUIRED_TOOLS = ("search", "route", "dispatch", "get_glossary",
                  "find_similar_cases", "save_case", "get_doc", "propose_fix",
                  "apply_fix")

check("the MCP module publishes its tool set", len(mcp_server.TOOLS) == 9)
check("every documented MCP tool name is present",
      set(REQUIRED_TOOLS) == set(mcp_server.TOOL_NAMES),
      str(mcp_server.TOOL_NAMES))

server = mcp_server.create_mcp_server(get_runtime())
check("create_mcp_server(runtime) builds a server", server is not None)
check("the server keeps the published name", server.name == "open-mind")

registered = sorted(t.name for t in asyncio.run(server.list_tools()))
# The nine core tools are a STABLE contract: they must remain registered,
# unchanged. Phase 2 adds read-only Asset tools ALONGSIDE them.
check("all nine core tools are still registered on the server",
      set(REQUIRED_TOOLS) <= set(registered), str(registered))

ASSET_TOOLS = ("list_assets", "get_asset", "get_asset_revisions", "get_evidence")
check("the four read-only Asset tools are registered additively",
      set(ASSET_TOOLS) <= set(registered), str(registered))
check("the core tool set is unchanged (no core tool renamed/removed)",
      set(mcp_server.TOOL_NAMES) == set(REQUIRED_TOOLS), str(mcp_server.TOOL_NAMES))
# v2 Phase 3 adds six read-only document tools alongside these. The invariant
# that matters is not "nothing else exists" — that would fail on every future
# phase — but that every registered tool is a KNOWN, deliberate addition, so a
# stray or accidentally-renamed tool is still caught.
DOCUMENT_TOOLS = ("list_documents", "get_document", "get_document_outline",
                  "search_documents", "search_knowledge",
                  "find_document_related_candidates")
check("the six read-only document tools are registered additively",
      set(DOCUMENT_TOOLS) <= set(registered), str(registered))
# v2 Phase 4 adds seven read-only semantic/lens tools the same way.
SEMANTIC_TOOLS = ("list_semantic_runs", "get_semantic_run",
                  "list_semantic_candidates", "get_semantic_candidate",
                  "list_project_lenses", "get_project_lens",
                  "get_semantic_usage")
check("the seven read-only semantic tools are registered additively",
      set(SEMANTIC_TOOLS) <= set(registered), str(registered))
# v2 Phase 5 adds nine read-only knowledge-graph tools the same way.
KNOWLEDGE_TOOLS = ("get_graph_stats", "search_graph", "get_graph_node",
                   "expand_graph", "find_graph_path",
                   "list_engineering_entities", "get_engineering_entity",
                   "get_engineering_claim", "get_engineering_relation")
check("the nine read-only graph tools are registered additively",
      set(KNOWLEDGE_TOOLS) <= set(registered), str(registered))
# v2 Phase 6 adds eight read-only traceability/conflict tools the same way.
TRACE_TOOLS = ("trace_requirement", "trace_code", "trace_test",
               "get_trace_path", "get_traceability_coverage",
               "list_traceability_gaps", "list_engineering_conflicts",
               "get_engineering_conflict")
check("the eight read-only trace/conflict tools are registered additively",
      set(TRACE_TOOLS) <= set(registered), str(registered))
check("every registered tool is an accounted-for addition",
      set(registered) == set(REQUIRED_TOOLS) | set(ASSET_TOOLS)
      | set(DOCUMENT_TOOLS) | set(SEMANTIC_TOOLS) | set(KNOWLEDGE_TOOLS)
      | set(TRACE_TOOLS),
      str(registered))

descriptions = {t.name: (t.description or "") for t in asyncio.run(server.list_tools())}
check("every tool still carries its docstring description",
      all(descriptions[n].strip() for n in REQUIRED_TOOLS + ASSET_TOOLS))

second = mcp_server.create_mcp_server(get_runtime())
check("create_mcp_server can be called repeatedly (independent instances)",
      second is not server)
check("the lazy module-level `mcp` object still resolves",
      mcp_server.mcp is not None)

# Importing the module must not have side effects on the database.
probe = subprocess.run(
    [sys.executable, "-c",
     "import os, tempfile;"
     "os.environ['OPENMIND_DATA_DIR']=tempfile.mkdtemp();"
     "os.environ['OPENMIND_MACHINE_DIR']=tempfile.mkdtemp();"
     "from openmind import config;"
     "import openmind.mcp_server;"
     "import pathlib;"
     "print(pathlib.Path(config.DB_PATH).exists())"],
    cwd=REPO, capture_output=True, text=True)
check("importing openmind.mcp_server no longer opens the database at import time",
      probe.stdout.strip() == "False", probe.stdout + probe.stderr)

# The documented command must still exist and be runnable.
probe = subprocess.run(
    [sys.executable, "-c",
     "import openmind.mcp_server as m; print(callable(m.main))"],
    cwd=REPO, capture_output=True, text=True)
check("python -m openmind.mcp_server still has a main() entry point",
      probe.stdout.strip() == "True", probe.stdout + probe.stderr)

# ---------------------------------------------------------------------------
# Skill bridge — must still run the REAL implementation over the JSON protocol
# ---------------------------------------------------------------------------
env = dict(os.environ)
env.update({"OPENMIND_EMBED_OFFLINE": "1", "OPENMIND_ENRICH_EGRESS": "0",
            "PYTHONIOENCODING": "utf-8"})
proc = subprocess.Popen(
    [sys.executable, "-m", "openmind.skill_bridge", "--root", FIXTURE],
    cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, bufsize=1)
try:
    ready = json.loads(proc.stdout.readline())
    check("skill bridge starts and prints a ready line", ready.get("ready") is True)
    check("skill bridge walked the corpus", ready.get("files", 0) > 0)
    check("skill bridge built the structure artifact",
          ready.get("definitions", 0) > 0)

    proc.stdin.write(json.dumps({"id": 1, "op": "route",
                                 "arg": "what does ISR mean"}) + "\n")
    proc.stdin.flush()
    reply = json.loads(proc.stdout.readline())
    check("skill bridge answers a route request",
          reply.get("id") == 1 and reply.get("ok") is True)
    check("the route reply carries the deterministic capability trace",
          all(k in reply["result"] for k in
              ("capability", "decided_by", "deterministic_fallback", "reason")))

    proc.stdin.write(json.dumps({"id": 2, "op": "nonsense", "arg": "x"}) + "\n")
    proc.stdin.flush()
    reply = json.loads(proc.stdout.readline())
    check("skill bridge reports an unknown op as an error, and keeps serving",
          reply.get("id") == 2 and reply.get("ok") is False and reply.get("error"))
finally:
    proc.stdin.close()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
