"""FastAPI application — scope-aware REST + SSE + static single-page UI.

LOCAL-ONLY: the server binds to 127.0.0.1 by default; the only outbound HTTP is
the local LLM call (via netguard). See /netlog for the outbound audit trail.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (ask as askmod, cases, config, conversation, db, diagrams,
               docs as docsmod, embeddings, fs_tree, glossary, jobs, llm_client,
               machine, mapio, models, netguard, rag, router, scope, structure,
               sysload, vectorstore, walker)
from .model_server import server as model_server


def _warm_vectorstore() -> None:
    """Open the Chroma persistent client ONCE, off the request path. The client is
    lazily created on first use; if that first use is a user click (delete/search)
    its init cost lands *inside* that request and looks like a hang. Warming it in
    a daemon thread at startup pays the cost up front instead. (Init is fast on a
    healthy store; the pathological multi-minute stalls come from a SECOND process
    opening the same store concurrently — keep one server per data dir.)

    Also reclaims storage leaked by older delete paths: collections whose project
    no longer exists are dropped (shrinks the store -> faster ops)."""
    try:
        vectorstore.backend_name()
        orphans = vectorstore.drop_orphan_collections({p["id"] for p in db.list_projects()})
        if orphans:
            print(f"[startup] dropped {len(orphans)} orphaned vector collection(s): "
                  f"{', '.join(orphans)}", flush=True)
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # lifespan (not deprecated on_event) so the background job worker reliably
    # starts under every server/runtime — otherwise jobs would sit queued.
    config.ensure_dirs()
    db.init_db()
    jobs.start_worker()
    threading.Thread(target=_warm_vectorstore, name="vs-warmup", daemon=True).start()
    yield


# docs_url disabled: the spec reserves GET /docs and /docs/{page} for the
# generated knowledge-base docs (not Swagger UI). OpenAPI JSON stays available.
app = FastAPI(title="Open Mind", version="0.1.0",
              docs_url=None, redoc_url=None, lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _scope_pids(scope_id: Optional[str]) -> List[str]:
    pids = scope.resolve(scope_id)
    if not pids:
        raise HTTPException(400, f"unknown or empty scope: {scope_id!r}")
    return pids


# ---------------------------------------------------------------------------
# UI + app health
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(config.STATIC_DIR / "index.html"))


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "embeddings_backend": embeddings.backend_name(),
        "vectorstore_backend": vectorstore.backend_name(),
        "llm_base_url": llm_client.base_url(),
        "llm_local": llm_client.is_local_endpoint(),
        "outbound_calls_logged": len(netguard.get_log(1000)),
        "server": model_server.status()["status"],
    }


@app.get("/netlog")
def netlog(limit: int = 200) -> Dict[str, Any]:
    """Outbound network audit (Invariant 1)."""
    return {"calls": netguard.get_log(limit)}


@app.get("/system/load")
def system_load() -> Dict[str, Any]:
    """Live, on-machine load (CPU / RAM / GPU-VRAM) for the Learning page.
    All readings are local — no network egress (Invariant 1)."""
    return sysload.snapshot()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
@app.get("/projects")
def list_projects(state: Optional[str] = None) -> Dict[str, Any]:
    return {"projects": db.list_projects(state)}


@app.post("/projects")
def create_project(req: models.CreateProjectReq) -> Dict[str, Any]:
    p = db.create_project(req.name)
    if req.path:
        db.upsert_project_path(p["id"], req.path, req.exclude)
    return db.get_project(p["id"])


@app.get("/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, Any]:
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(404, "project not found")
    p["code_chunks"] = vectorstore.get_code_store(project_id).count()
    p["cases_count"] = vectorstore.get_cases_store(project_id).count()
    p["files_indexed"] = len(db.get_file_index(project_id))
    return p


@app.post("/projects/{project_id}/paths")
def add_path(project_id: str, req: models.AddPathReq) -> Dict[str, Any]:
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    db.upsert_project_path(project_id, req.path, req.exclude)
    return db.get_project(project_id)


@app.post("/projects/{project_id}/selection")
def save_selection(project_id: str, req: models.SaveSelectionReq) -> Dict[str, Any]:
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    db.upsert_project_path(project_id, req.path, req.exclude)
    return db.get_project(project_id)


@app.delete("/projects/{project_id}/paths")
def remove_path(project_id: str, path: str) -> Dict[str, Any]:
    db.remove_project_path(project_id, path)
    return db.get_project(project_id)


@app.post("/projects/{project_id}/source-link")
def set_source_link(project_id: str, req: models.SourceLinkReq) -> Dict[str, Any]:
    """Link a project to its source when no local copy resolves (see GET /source).

    kind=="local" re-points the machine-local source root at a folder; kind=="github"
    saves the repo so jump-to-source can fetch files on demand (audited). Both are
    stored MACHINE-LOCALLY (never in data/), keeping a copied data/ folder trace-free."""
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    kind = (req.kind or "").strip().lower()
    if kind == "local":
        path = (req.path or "").strip()
        if not path:
            raise HTTPException(400, "a local folder path is required")
        if not Path(path).is_dir():
            raise HTTPException(400, "folder not found on this machine: %s" % path)
        root = machine.set_local_root(project_id, path)
        return {"kind": "local", "root": root}
    if kind == "github":
        try:
            spec = machine.set_github(project_id, req.url or "", req.ref)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"kind": "github", "github": spec,
                "fetch_enabled": config.SOURCELINK_EGRESS}
    raise HTTPException(400, "kind must be 'local' or 'github'")


@app.post("/projects/{project_id}/terminate")
def terminate(project_id: str, req: models.TerminateReq) -> Dict[str, Any]:
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    return jobs.terminate_project(project_id, clear_cases=req.clear_cases)


@app.delete("/projects/{project_id}")
def delete_project(project_id: str) -> Dict[str, Any]:
    """Permanently remove the project. Returns IMMEDIATELY ({deleting}); the project
    vanishes from the listing at once and its storage is reclaimed in the background
    so the UI never freezes on a large project."""
    if not db.get_project(project_id):
        raise HTTPException(404, "project not found")
    return jobs.request_delete(project_id)


@app.get("/scope/{scope_id}")
def describe_scope(scope_id: str) -> Dict[str, Any]:
    return scope.describe(scope_id)


# ---------------------------------------------------------------------------
# Filesystem pickers
# ---------------------------------------------------------------------------
@app.get("/fs/tree")
def fs_tree_route(path: Optional[str] = None, depth: int = 1) -> Dict[str, Any]:
    return fs_tree.list_dir(path)


@app.get("/fs/list")
def fs_list_route(path: Optional[str] = None) -> Dict[str, Any]:
    """Model file picker — directories + .gguf files."""
    if path:
        db.kv_set("last_model_folder", path)
    return fs_tree.list_dir(path, file_ext={".gguf"})


@app.get("/fs/pick-folder")
def fs_pick_folder(initial: Optional[str] = None) -> Dict[str, Any]:
    """Open the OS-native folder dialog (server runs on the user's own machine,
    so this is the real system picker). Falls back gracefully if no display."""
    import os
    import threading
    initial = initial or db.kv_get("last_project_folder") or str(config.ROOT_DIR)
    out: Dict[str, Any] = {"path": None, "error": None}

    def _run():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(
                initialdir=initial, title="Select repository root folder",
                mustexist=True)
            root.update()
            root.destroy()
            out["path"] = chosen or None
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=300)
    if t.is_alive():
        out["error"] = "dialog timed out"
    if out["path"]:
        p = out["path"]
        if os.name == "nt":
            p = p.replace("/", "\\")
        out["path"] = p
        db.kv_set("last_project_folder", p)
    return out


# ---------------------------------------------------------------------------
# Model config + server lifecycle
# ---------------------------------------------------------------------------
@app.get("/model-config")
def get_model_config() -> Dict[str, Any]:
    cfg = db.get_model_config()
    cfg["_last_model_folder"] = db.kv_get("last_model_folder", config.DEFAULT_MODELS_FOLDER)
    cfg["_default_models_folder"] = config.DEFAULT_MODELS_FOLDER
    return cfg


@app.post("/model-config")
async def set_model_config(req: Request) -> Dict[str, Any]:
    body = await req.json()
    return db.set_model_config(body)


@app.post("/server/start")
def server_start() -> Dict[str, Any]:
    return model_server.start(db.get_model_config())


@app.post("/server/stop")
def server_stop() -> Dict[str, Any]:
    return model_server.stop()


@app.post("/server/restart")
def server_restart() -> Dict[str, Any]:
    return model_server.restart(db.get_model_config())


@app.get("/server/status")
def server_status() -> Dict[str, Any]:
    model_server.refresh()
    return model_server.status()


# ---------------------------------------------------------------------------
# Ingest / gendocs
# ---------------------------------------------------------------------------
@app.post("/ingest")
def ingest(req: models.IngestReq) -> Dict[str, Any]:
    if not db.get_project(req.project_id):
        raise HTTPException(404, "project not found")
    job = jobs.enqueue_ingest(req.project_id, req.path)
    return {"job_id": job["job_id"], "job": job}


@app.post("/gendocs")
def gendocs(req: models.GendocsReq) -> Dict[str, Any]:
    if not db.get_project(req.project_id):
        raise HTTPException(404, "project not found")
    job = jobs.enqueue_gendocs(req.project_id)
    return {"job_id": job["job_id"], "job": job}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.get("/jobs")
def list_jobs(scope_id: Optional[str] = Query(None, alias="scope")) -> Dict[str, Any]:
    pids = None
    if scope_id:
        pids = scope.resolve(scope_id) or ["__none__"]
    return {"jobs": db.list_jobs(project_ids=pids)}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.post("/jobs/{job_id}/pause")
def pause_job(job_id: str) -> Dict[str, Any]:
    try:
        return jobs.pause(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str) -> Dict[str, Any]:
    try:
        return jobs.resume(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    """Cancel a queued/running job (used by the Ask queue; Part 2)."""
    try:
        return jobs.cancel_job(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    async def gen():
        last = None
        idle = 0
        while True:
            job = db.get_job(job_id)
            if not job:
                yield f"event: error\ndata: {json.dumps({'error':'not found'})}\n\n"
                return
            snapshot = json.dumps(job, sort_keys=True)
            if snapshot != last:
                last = snapshot
                yield f"data: {json.dumps(job)}\n\n"
                idle = 0
            else:
                idle += 1
                if idle % 5 == 0:
                    yield ": heartbeat\n\n"
            if job["status"] in ("done", "failed"):
                yield f"event: end\ndata: {json.dumps(job)}\n\n"
                return
            await asyncio.sleep(0.8)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Glossary (deterministic term/acronym index) + jump-to-source
# ---------------------------------------------------------------------------
@app.get("/glossary")
def get_glossary(scope_id: str = Query(..., alias="scope"),
                 term: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic glossary lookup. With `term`: an exact-token resolve to
    {definition (verbatim), source_file, line_number, content_hash, source_kind}
    from the persisted map (no similarity, no model guess); an absent term returns
    found=False with an honest 'no authoritative definition found' message — never
    a fabricated definition. Without `term`: list all known terms (term ->
    definition). Acronym/term questions route here first.

    On a single-term hit we attach a deterministic, source-traceable USAGE PROFILE
    (where the term is defined, which files reference it, the modules it spans, and
    related co-located glossary terms) — all derived from the structure map, never
    generated — so an entry is a grounded knowledge unit, not a bare line."""
    pids = _scope_pids(scope_id)
    result = glossary.get_glossary(mapio.merged_glossary(pids), term)
    if term and result.get("found"):
        sdoc = mapio.merged_structure(pids)
        usage = structure.term_usage(sdoc, result["term"], result.get("source_file", ""))
        gbyfile = _glossary_by_file(pids)
        files = {d["file"] for d in usage["defined_at"]} | set(usage["used_in"])
        if result.get("source_file"):
            files.add(result["source_file"])
        related = {t for fp in files for t in gbyfile.get(fp, []) if t != result["term"]}
        usage["related_terms"] = sorted(related)[:20]
        result["usage"] = usage
    return result


@app.post("/glossary/enrich")
def enrich_glossary(req: models.EnrichGlossaryReq) -> Dict[str, Any]:
    """Attach EXTERNAL standard definitions (from the wikipedia-glossary search
    skill) to already-extracted glossary terms.

    LOCAL-ONLY: this app performs NO web request here — it only RECEIVES text the
    skill already fetched and merges it into the persisted glossary. The network
    actor is the skill (run by the agent), never Open Mind. Each definition is
    stored as a separate, attributed field; the verbatim in-project definition is
    untouched and NO new terms are created — an entry whose term is not already in
    the glossary is reported under `missing`, never invented."""
    pids = _scope_pids(req.scope)
    docs = {pid: mapio.load_glossary(pid) for pid in pids}
    retrieved = time.strftime("%Y-%m-%d", time.localtime())
    updated: List[str] = []
    missing: List[str] = []
    changed: set = set()
    for ent in req.entries:
        hit = False
        for pid in pids:
            if glossary.set_standard_definition(docs[pid], ent.term,
                                                definition=ent.definition,
                                                html=ent.html,
                                                url=ent.url, title=ent.title,
                                                retrieved=retrieved):
                changed.add(pid)
                hit = True
        (updated if hit else missing).append(ent.term)
    for pid in changed:
        mapio.save_glossary(pid, docs[pid])
    return {"updated": sorted(set(updated)), "missing": sorted(set(missing)),
            "saved_projects": sorted(changed), "retrieved": retrieved,
            "count": len(req.entries)}


@app.post("/glossary/enrich/auto")
def enrich_glossary_auto(req: models.EnrichAutoReq) -> Dict[str, Any]:
    """Queue the DETERMINISTIC in-app enrichment engine for the scope (the same
    engine the ingest pipeline runs). Optionally persists per-project settings
    (context hint, pinned term->article overrides, enable flag) into project meta
    first. Returns the queued job per project. The agent/UI uses this to trigger or
    re-run enrichment; the pipeline guarantees it runs on every train regardless."""
    pids = _scope_pids(req.scope)
    out: List[Dict[str, Any]] = []
    for pid in pids:
        p = db.get_project(pid)
        if not p:
            continue
        meta = dict(p.get("meta") or {})
        changed = False
        if req.context is not None:
            meta["enrich_context"] = req.context
            changed = True
        if req.pins:
            cur = dict(meta.get("enrich_pins") or {})
            cur.update({str(k): str(v) for k, v in req.pins.items() if k and v})
            meta["enrich_pins"] = cur
            changed = True
        if req.block:
            blk = sorted(set(meta.get("enrich_block") or []) | {str(b) for b in req.block if b})
            meta["enrich_block"] = blk
            changed = True
            # apply immediately: clear any (wrong) match and mark the blocked terms
            # 'none' so they are not re-looked-up — fixes a bad auto-match at once.
            gdoc = mapio.load_glossary(pid)
            touched = False
            for b in req.block:
                if glossary.mark_no_match(gdoc, str(b)):
                    touched = True
            if touched:
                mapio.save_glossary(pid, gdoc)
        if req.enabled is not None:
            meta["enrich_enabled"] = bool(req.enabled)
            changed = True
        if changed:
            db.update_project_meta(pid, meta)
        job = jobs.enqueue_enrich(pid)
        out.append({"project_id": pid, "job_id": (job or {}).get("job_id"),
                    "queued": bool(job)})
    return {"results": out}


def _source_window(rel_norm: str, text: str, line: int, context: int,
                   origin: str) -> Dict[str, Any]:
    """Slice a window of *text* around *line* into the viewer's response shape."""
    lines = text.split("\n")
    total = len(lines)
    if line and context:
        start = max(1, line - context)
        end = min(total, line + context)
    else:
        start, end = 1, min(total, 400)
    window = [{"n": i, "text": lines[i - 1]} for i in range(start, end + 1)]
    return {"status": "ok", "origin": origin, "file": rel_norm, "line": line,
            "start": start, "end": end, "total": total, "lines": window}


@app.get("/source")
def get_source(scope_id: str = Query(..., alias="scope"),
               file: str = Query(...), line: int = 0,
               context: int = 60) -> Dict[str, Any]:
    """Return a window of a source file for the jump-to-source view.

    LOCAL-FIRST + SAFE: `file` is a PROJECT-RELATIVE path and must be one of the
    scope's indexed sources (an allow-list built from the map's own provenance), so
    this cannot read arbitrary paths off the machine — nor fetch arbitrary files from
    GitHub. Resolution order:
      1. the project's machine-local source root, if set and the file exists;
      2. else the project's linked GitHub repo, fetched on demand (audited);
      3. else ``status="needs_link"`` so the UI can offer to link the source."""
    pids = _scope_pids(scope_id)
    allow = mapio.glossary_sources(pids) | mapio.structure_sources(pids)
    # find the project whose machine-local root resolves this relative source
    rel_norm = ""
    abs_path = ""
    owner_pid = ""
    for pid in pids:
        root = machine.project_root(pid)
        rel = machine.relativize(file, root)   # tolerate an absolute (legacy) input
        if rel not in allow:
            continue
        rel_norm = rel
        owner_pid = owner_pid or pid
        cand = machine.absolutize(rel, root)
        if root and Path(cand).exists():
            abs_path = cand
            owner_pid = pid
            break
    if not rel_norm:
        raise HTTPException(404, "file is not an indexed source for this scope")

    # 1) local file on this machine
    if abs_path:
        text = walker.read_text(abs_path)
        if not text:
            raise HTTPException(404, "source file is empty or unreadable")
        return _source_window(rel_norm, text, line, context, origin="local")

    # 2) linked GitHub repo — fetch on demand (audited via netguard)
    github = machine.get_github(owner_pid) if owner_pid else None
    if github:
        url = machine.github_raw_url(github, rel_norm)
        if not config.SOURCELINK_EGRESS:
            return {"status": "error", "file": rel_norm, "project_id": owner_pid,
                    "github": github, "url": url,
                    "reason": "GitHub source fetch is disabled "
                              "(OPENMIND_SOURCELINK_EGRESS=0)"}
        try:
            resp = netguard.guarded_sourcelink_request("GET", url)
        except netguard.ExfiltrationBlocked as e:
            return {"status": "error", "file": rel_norm, "project_id": owner_pid,
                    "github": github, "url": url, "reason": str(e)}
        except Exception as e:   # noqa: BLE001 - network/transport failure
            return {"status": "error", "file": rel_norm, "project_id": owner_pid,
                    "github": github, "url": url,
                    "reason": "could not reach GitHub: %s" % e}
        if resp.status_code == 404:
            return {"status": "error", "file": rel_norm, "project_id": owner_pid,
                    "github": github, "url": url,
                    "reason": "file not found in the linked repo at ref '%s' — "
                              "check the branch/tag" % github.get("ref", "HEAD")}
        if resp.status_code >= 400:
            return {"status": "error", "file": rel_norm, "project_id": owner_pid,
                    "github": github, "url": url,
                    "reason": "GitHub returned HTTP %d" % resp.status_code}
        out = _source_window(rel_norm, resp.text, line, context, origin="github")
        out["url"] = url
        out["github"] = github
        return out

    # 3) nothing resolves — invite the user to link the source
    return {"status": "needs_link", "file": rel_norm, "project_id": owner_pid}


# ---------------------------------------------------------------------------
# Structure / code graphs (deterministic projection of openmind.structure)
# ---------------------------------------------------------------------------
def _glossary_by_file(pids: List[str]) -> Dict[str, List[str]]:
    """source_file -> [glossary terms], for cross-linking graph nodes to terms."""
    out: Dict[str, List[str]] = {}
    for term, e in mapio.merged_glossary(pids).get("terms", {}).items():
        sf = e.get("source_file")
        if sf:
            out.setdefault(sf, []).append(term)
    return out


@app.get("/structure")
def get_structure(scope_id: str = Query(..., alias="scope")) -> Dict[str, Any]:
    """Deterministic structure overview for the scope (archetype + stats + entry
    points + top modules). The basis of the Graphs view."""
    doc = mapio.merged_structure(_scope_pids(scope_id))
    ov = structure.overview(doc)
    ov["archetype"] = diagrams._archetype(doc)
    ov["generated_at"] = doc.get("generated_at")
    ov["has_data"] = bool(doc.get("files"))
    return ov


@app.get("/route")
def route_query(query: str = Query(...)) -> Dict[str, Any]:
    """Agent-style capability routing with deterministic graceful degradation.

    Returns which capability (glossary / structure / search) would handle the query,
    WHO decided (a ready local model, validated against the capability set — else the
    deterministic if-else floor), and the deterministic fallback + reason. The model
    can never pick a capability outside the known set."""
    return router.route(query)


@app.get("/dispatch")
def dispatch_query(scope_id: str = Query(..., alias="scope"),
                   query: str = Query(...)) -> Dict[str, Any]:
    """Route the query to one capability and INVOKE it, returning the result plus the
    routing trace. Each capability is itself deterministic/grounded."""
    return router.dispatch(_scope_pids(scope_id), query)


@app.get("/graph")
def get_graph(scope_id: str = Query(..., alias="scope"),
              kind: str = "call") -> Dict[str, Any]:
    """Call mind-map roots (entry points / hubs), each expandable via
    /graph/children. All real names from the deterministic structure map."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    return diagrams.call_roots(doc, gbyfile=_glossary_by_file(pids))


@app.get("/graph/children")
def get_graph_children(scope_id: str = Query(..., alias="scope"),
                       id: str = Query(...), offset: int = 0, limit: int = 7) -> Dict[str, Any]:
    """Callees of one mind-map node, paginated (show `limit`, then '+more')."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    return diagrams.call_children(doc, id, offset=offset, limit=limit,
                                  gbyfile=_glossary_by_file(pids))


@app.get("/graph/node")
def get_graph_node(scope_id: str = Query(..., alias="scope"),
                   id: str = Query(...)) -> Dict[str, Any]:
    """Detail for a clicked node: source location, defs, call neighbors, glossary."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    return diagrams.node_detail(doc, id, gbyfile=_glossary_by_file(pids))


# ---------------------------------------------------------------------------
# Search (cases-first, then exact-token / conceptual RAG)
# ---------------------------------------------------------------------------
@app.post("/search")
def search(req: models.SearchReq) -> Dict[str, Any]:
    pids = _scope_pids(req.scope)
    case_hits = cases.search_cases(pids, req.query, k=5)
    close = [c for c in case_hits if c.get("similarity", 0) >= 0.65]
    result = rag.retrieve(pids, req.query, k=req.k, case_sensitive=req.case_sensitive,
                          subword=req.subword, exact=req.exact)
    return {
        "scope": scope.describe(req.scope),
        "case_hits": case_hits,
        "case_shortcircuit": bool(close),
        "code_chunks": result["code_chunks"],
        "tokens": result["tokens"],
        "query_mode": result["query_mode"],
        "exact_token": result["exact_token"],
        "grounding": result["grounding"],
        "backends": result["backends"],
    }


@app.post("/ocr")
async def ocr(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Local OCR for an image attachment (tesseract; graceful if unavailable).
    The raw image is never sent to the model — only any extracted text."""
    data = await file.read()
    return askmod.ocr_image(data, file.filename or "image")


@app.post("/ask")
def ask(req: models.AskReq) -> Dict[str, Any]:
    """ENQUEUE a grounded Ask onto the SAME single-task worker that governs the
    rest of the system (Part 2). Returns {job_id, exchange_id}; the answer streams
    via GET /jobs/{job_id}/stream and is persisted as a retained exchange (Part 1).
    Refuses (409) when the model server is not ready instead of queuing a doomed
    request (Invariant 7)."""
    pids = _scope_pids(req.scope)
    model_server.refresh()
    if not llm_client.is_ready():
        raise HTTPException(409, "model server not ready")
    desc = scope.describe(req.scope)
    return jobs.enqueue_ask(req.scope, pids, pids[0], desc,
                            req.question, req.attachments, req.k)


@app.get("/ask/history")
def ask_history(scope_id: str = Query(..., alias="scope")) -> Dict[str, Any]:
    """The retained Ask thread for this scope (last N exchanges; Part 1)."""
    _scope_pids(scope_id)   # validate scope exists
    return {"scope": scope.describe(scope_id),
            "exchanges": conversation.history(scope_id),
            "cap": db.ASK_HISTORY_CAP}


@app.get("/ask/exchange/{exchange_id}")
def ask_exchange(exchange_id: str) -> Dict[str, Any]:
    """A single retained exchange (used to fetch sources/meta + the final answer)."""
    ex = conversation.get(exchange_id)
    if not ex:
        raise HTTPException(404, "exchange not found")
    return conversation.public(ex)


@app.post("/ask/clear")
def ask_clear(req: models.ClearAskReq) -> Dict[str, Any]:
    """New conversation: clear the retained thread for this scope (Part 1, Invariant 4)."""
    _scope_pids(req.scope)
    conversation.clear(req.scope)
    return {"cleared": req.scope}


@app.post("/ask/save-case")
def ask_save_case(req: models.SaveAskCaseReq) -> Dict[str, Any]:
    """Promote a retained Ask exchange into the SEPARATE solved-cases RAG layer,
    embedded at save time (Part 3). Idempotent: a second save returns the existing
    case rather than silently duplicating it."""
    ex = conversation.get(req.exchange_id)
    if not ex:
        raise HTTPException(404, "exchange not found")
    if ex.get("saved_case_id"):
        return {"case_id": ex["saved_case_id"], "already_saved": True}
    pids = ex.get("pids") or scope.resolve(req.scope or ex.get("scope_id"))
    pids = [p for p in (pids or []) if db.get_project(p)]
    if not pids:
        raise HTTPException(400, "no valid project to attach the case to")
    pid = pids[0]
    # Atomically claim the save slot BEFORE minting/embedding the case, so two
    # concurrent saves (double-click / retry / MCP) can never create duplicates.
    case_id = db.new_id("case_")
    claim = db.ask_claim_case(req.exchange_id, case_id)
    if not claim:
        raise HTTPException(404, "exchange not found")
    if not claim["claimed"]:
        return {"case_id": claim["case_id"], "already_saved": True}
    case = conversation.case_from_exchange(ex)
    case["id"] = case_id
    saved = cases.save_case(pid, case)
    return {"case_id": saved["id"], "already_saved": False, "case": saved}


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
@app.post("/cases")
def save_case(req: models.SaveCaseReq) -> Dict[str, Any]:
    pid = req.project_id
    if not pid and req.scope:
        resolved = scope.resolve(req.scope)
        pid = resolved[0] if resolved else None
    if not pid or not db.get_project(pid):
        raise HTTPException(400, "a valid project_id (or single-project scope) is required")
    case = {
        "problem_text": req.problem_text,
        "resolution_summary": req.resolution_summary,
        "involved_services": req.involved_services,
        "involved_topics": req.involved_topics,
        "file_refs": req.file_refs,
        "tags": req.tags,
    }
    return cases.save_case(pid, case)


@app.get("/cases")
def list_cases(scope_id: str = Query(..., alias="scope"),
               query: Optional[str] = None) -> Dict[str, Any]:
    return {"cases": cases.list_cases(_scope_pids(scope_id), query)}


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------
@app.get("/docs")
def list_docs(scope_id: str = Query(..., alias="scope")) -> Dict[str, Any]:
    return {"docs": docsmod.list_docs(_scope_pids(scope_id))}


@app.get("/docs/{page}")
def get_doc(page: str, scope_id: str = Query(..., alias="scope")) -> Dict[str, Any]:
    doc = docsmod.get_doc(_scope_pids(scope_id), page)
    if not doc:
        raise HTTPException(404, "doc page not found")
    return doc


# mount static (for any future assets)
if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
