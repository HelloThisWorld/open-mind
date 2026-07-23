"""FastAPI application — scope-aware REST + SSE + static single-page UI.

LOCAL-ONLY: the server binds to 127.0.0.1 by default; the only outbound HTTP is
the local LLM call (via netguard). See /netlog for the outbound audit trail.

ONE ADAPTER AMONG SEVERAL
-------------------------
This module is no longer where application behaviour is defined. Workspace,
path, ingest, job, lifecycle and health use cases live in
:mod:`openmind.services` and are shared with the CLI and the MCP server; the
routes below validate HTTP input, call one service, and shape the response.

Routes that are already a thin call into a deterministic query module (glossary,
structure, graphs, source navigation, Ask) deliberately keep calling it
directly: routing a one-line query through a service method would add
indirection without adding a seam.
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
               sysload, templates, vectorstore, walker)
from .domain.errors import OpenMindError
from .model_server import server as model_server
from .runtime import get_runtime
from .version import RUNTIME_VERSION


def _sweep_orphan_project_dirs() -> List[str]:
    """Remove data/<p_...> dirs whose project row no longer exists — leftovers
    of a crash between the storage wipe and the row delete (or of older delete
    paths). Only exact project-id-shaped dirs are touched, and 'deleting'
    tombstones are NOT orphans (their cleanup is still running/resuming)."""
    import re
    import shutil
    known = {p["id"] for p in db.list_projects()} | \
            {p["id"] for p in db.list_projects("deleting")}
    gone: List[str] = []
    for d in config.DATA_DIR.iterdir():
        if (d.is_dir() and re.fullmatch(r"p_[0-9a-f]{12}", d.name)
                and d.name not in known):
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                gone.append(d.name)
    return gone


def _warm_vectorstore() -> None:
    """Open the Chroma persistent client ONCE, off the request path. The client is
    lazily created on first use; if that first use is a user click (delete/search)
    its init cost lands *inside* that request and looks like a hang. Warming it in
    a daemon thread at startup pays the cost up front instead. (Init is fast on a
    healthy store; the pathological multi-minute stalls come from a SECOND process
    opening the same store concurrently — keep one server per data dir.)

    Also the storage janitor: drops collections whose project no longer exists
    and removes orphaned project data dirs — best-effort, off the request path.
    (Leaked HNSW segment dirs are swept in lifespan BEFORE the client opens —
    see sweep_orphan_segment_dirs for why it cannot run here.)"""
    try:
        vectorstore.backend_name()
        # Same union as _sweep_orphan_project_dirs above, for the same reason: a
        # 'deleting' project's collections are NOT orphans — a cleanup thread
        # already owns them (jobs._recover_on_restart spawned it moments ago in
        # lifespan). db.list_projects() with no argument EXCLUDES 'deleting', so
        # passing it alone made the janitor race that cleanup: whichever thread
        # reached delete_collection first made the other's next get() raise, the
        # cleanup read that as "interrupted" and returned BEFORE removing the
        # data dir and the project row — so the tombstone came back every boot
        # and the project could never finish deleting.
        known = {p["id"] for p in db.list_projects()} | \
                {p["id"] for p in db.list_projects("deleting")}
        orphans = vectorstore.drop_orphan_collections(
            known, cancel=jobs.shutdown_requested)
        if orphans:
            print(f"[startup] dropped {len(orphans)} orphaned vector collection(s): "
                  f"{', '.join(orphans)}", flush=True)
        gone = _sweep_orphan_project_dirs()
        if gone:
            print(f"[startup] removed {len(gone)} orphaned project data dir(s): "
                  f"{', '.join(gone)}", flush=True)
    except Exception as exc:
        print(f"[startup] vectorstore warm-up failed (first use will retry lazily): {exc}",
              flush=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # lifespan (not deprecated on_event) so the background job worker reliably
    # starts under every server/runtime — otherwise jobs would sit queued.
    # bootstrap() is the SAME path the CLI and the MCP server take: ensure dirs,
    # open the database, migrate to head, build the services.
    runtime = get_runtime()
    # sweep leaked HNSW segment dirs FIRST — it reads chroma.sqlite3 with a raw
    # (read-only) connection, which is only safe while NO chroma client exists
    # in this process (start_worker may spawn delete-cleanup threads that open
    # one; racing the rust bindings here deadlocks GIL <-> sqlite lock). This
    # stays here rather than in bootstrap(): it is a long-lived-server concern,
    # and running it from a short-lived CLI process beside a live server would
    # be unsafe.
    swept = vectorstore.sweep_orphan_segment_dirs()
    if swept:
        print(f"[startup] removed {len(swept)} leaked vector segment dir(s).",
              flush=True)
    # Say out loud that storage reclaim is pending. A tombstone whose cleanup
    # keeps failing used to be completely silent: the project simply vanished
    # from the UI while every boot quietly re-ran a multi-minute drain, so the
    # app felt slow for reasons nothing on screen could explain.
    pending = db.list_projects("deleting")
    if pending:
        print(f"[startup] {len(pending)} project(s) pending storage reclaim: "
              f"{', '.join(p['id'] for p in pending)}. The app stays usable "
              f"while this runs; watch '[vectorstore] dropping ...' for progress.",
              flush=True)
    # The worker is opt-in per surface; the web app always needs one.
    runtime.ensure_worker()
    threading.Thread(target=_warm_vectorstore, name="vs-warmup", daemon=True).start()
    yield
    # shutdown: stop background delete-cleanup at its next batch so Ctrl+C
    # exits promptly; 'deleting' tombstones resume on the next start.
    runtime.shutdown()


# docs_url disabled: the spec reserves GET /docs and /docs/{page} for the
# generated knowledge-base docs (not Swagger UI). OpenAPI JSON stays available.
app = FastAPI(title="Open Mind", version=RUNTIME_VERSION,
              docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.exception_handler(OpenMindError)
async def _application_error(_request: Request, exc: OpenMindError) -> JSONResponse:
    """Map a service-layer error onto its HTTP status.

    Services raise typed errors rather than HTTPException so the CLI and MCP can
    map the same failure their own way. ``detail`` keeps the shape FastAPI's own
    HTTPException produces, so existing clients see no difference; ``error``
    carries the machine-readable code alongside it.
    """
    return JSONResponse(status_code=exc.http_status,
                        content={"detail": exc.message, "error": exc.as_dict()})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#: The shared application services. Same container the CLI and MCP server use.
_svc = get_runtime


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
def api_health(diagnostics: bool = False) -> Dict[str, Any]:
    """Liveness + backend summary.

    ADDITIVE ONLY: every key this returned before is unchanged. ``version`` and
    ``schema_version`` are new and cheap.

    The full ``doctor`` report is behind ``?diagnostics=1`` rather than always
    included: it probes the model endpoint with a timeout, stats the disk, and
    writes a temp file to prove the data dir is writable. That is right for an
    on-demand diagnostic and wrong for a liveness endpoint a monitor may poll.
    """
    runtime = _svc()
    payload = {
        "ok": True,
        "embeddings_backend": embeddings.backend_name(),
        "vectorstore_backend": vectorstore.backend_name(),
        "llm_base_url": llm_client.base_url(),
        "llm_local": llm_client.is_local_endpoint(),
        "outbound_calls_logged": len(netguard.get_log(1000)),
        "server": model_server.status()["status"],
        "version": RUNTIME_VERSION,
        "schema_version": runtime.info()["schema_version"],
    }
    if diagnostics:
        payload["diagnostics"] = runtime.health.summary()
    return payload


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
    return {"projects": _svc().workspaces.list(state)}


@app.post("/projects")
def create_project(req: models.CreateProjectReq) -> Dict[str, Any]:
    return _svc().workspaces.create(req.name, path=req.path, exclude=req.exclude)


@app.get("/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, Any]:
    return _svc().workspaces.describe(project_id)


@app.post("/projects/{project_id}/paths")
def add_path(project_id: str, req: models.AddPathReq) -> Dict[str, Any]:
    return _svc().workspaces.add_path(project_id, req.path, req.exclude)


@app.post("/projects/{project_id}/selection")
def save_selection(project_id: str, req: models.SaveSelectionReq) -> Dict[str, Any]:
    """Persist the folder selection (path + exclude-set). Same operation as
    /paths; kept as a separate route because the UI's selection panel posts
    here."""
    return _svc().workspaces.add_path(project_id, req.path, req.exclude)


@app.delete("/projects/{project_id}/paths")
def remove_path(project_id: str, path: str) -> Dict[str, Any]:
    """Remove one source path.

    BEHAVIOUR FIX: this used to call straight through to the database and return
    ``None`` for an unknown project. Since the handler is annotated
    ``-> Dict[str, Any]``, FastAPI's response validation turned that into a
    500 ResponseValidationError. It now 404s like every sibling route.
    """
    return _svc().workspaces.remove_path(project_id, path)


@app.get("/templates")
def list_templates() -> Dict[str, Any]:
    """Available template profiles: built-ins plus user files dropped into
    <data dir>/templates (same name -> the user file wins). Invalid files are
    listed WITH their validation errors instead of silently disappearing."""
    return {"templates": templates.list_templates(),
            "user_dir": str(config.USER_TEMPLATES_DIR)}


@app.get("/projects/{project_id}/template")
def get_project_template(project_id: str) -> Dict[str, Any]:
    """This project's template selection: the user override, the auto-selection
    recorded at learn time, and which one is effective."""
    return _svc().workspaces.template_selection(project_id)


@app.post("/projects/{project_id}/template")
def set_project_template(project_id: str, req: models.SetTemplateReq) -> Dict[str, Any]:
    """Set (or clear, with name=null) the project's template override. The name
    must resolve to a valid template at write time — a typo fails HERE, not
    silently at the next learn."""
    return _svc().workspaces.set_template(project_id, req.name)


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
    return _svc().workspaces.terminate(project_id, clear_cases=req.clear_cases)


@app.delete("/projects/{project_id}")
def delete_project(project_id: str) -> Dict[str, Any]:
    """Permanently remove the project. Returns IMMEDIATELY ({deleting}); the project
    vanishes from the listing at once and its storage is reclaimed in the background
    so the UI never freezes on a large project."""
    return _svc().workspaces.request_delete(project_id)


# ---------------------------------------------------------------------------
# Canonical Asset model (OpenMind v2 Phase 2) — ADDITIVE, read-only.
# Every route is workspace-scoped through AssetService; a cross-workspace id
# resolves to a typed not-found and is mapped to 404 by the exception handler.
# The `stats` literal path is registered before `{asset_id}` so it is never
# captured as an asset id.
# ---------------------------------------------------------------------------
@app.get("/projects/{project_id}/assets")
def list_assets(project_id: str, type: Optional[str] = None,
                state: Optional[str] = None, limit: int = 100,
                offset: int = 0) -> Dict[str, Any]:
    return _svc().assets.list_assets(project_id, asset_type=type, state=state,
                                     limit=limit, offset=offset)


@app.get("/projects/{project_id}/assets/stats")
def asset_stats(project_id: str) -> Dict[str, Any]:
    return _svc().assets.stats(project_id)


@app.post("/projects/{project_id}/assets/sync")
def sync_asset(project_id: str, req: models.AssetSyncReq) -> Dict[str, Any]:
    """Ingest a single existing file under a registered source root (Phase 2).
    Delegates to AssetService.sync_file; a directory or a file outside the root
    is a 400, an unsupported format is registered as `unsupported` (not parsed)."""
    return _svc().assets.sync_file(project_id, req.path, wait=req.wait,
                                   timeout=req.timeout)


@app.get("/projects/{project_id}/assets/{asset_id}")
def get_asset(project_id: str, asset_id: str) -> Dict[str, Any]:
    return _svc().assets.get_asset(project_id, asset_id)


@app.get("/projects/{project_id}/assets/{asset_id}/revisions")
def list_asset_revisions(project_id: str, asset_id: str,
                         limit: int = 50) -> Dict[str, Any]:
    return _svc().assets.list_revisions(project_id, asset_id, limit=limit)


@app.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str) -> Dict[str, Any]:
    return _svc().assets.get_revision(project_id, revision_id)


@app.get("/projects/{project_id}/revisions/{revision_id}/segments")
def list_revision_segments(project_id: str, revision_id: str, limit: int = 200,
                           offset: int = 0) -> Dict[str, Any]:
    return _svc().assets.list_segments(project_id, revision_id, limit=limit,
                                       offset=offset)


@app.get("/projects/{project_id}/evidence/{evidence_id}")
def get_evidence(project_id: str, evidence_id: str,
                 max_chars: int = 4000) -> Dict[str, Any]:
    return _svc().assets.get_evidence(project_id, evidence_id, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Documents (OpenMind v2 Phase 3) — ADDITIVE.
#
# Every route is workspace-scoped through DocumentService, so a cross-workspace
# id resolves to a typed not-found and is mapped to 404 by the exception
# handler. Responses are bounded; full document content is never returned (an
# outline is structure, a search hit is a bounded excerpt, and the exact stored
# text comes from the existing GET /projects/{id}/evidence/{evidence_id}).
#
# The literal `search` path is registered BEFORE `{asset_id}` so it can never be
# captured as an asset id. `/ocr` stays a separate route: a PDF import is never
# silently routed through OCR.
# ---------------------------------------------------------------------------
@app.get("/projects/{project_id}/documents")
def list_documents(project_id: str, status: Optional[str] = None,
                   parser: Optional[str] = None, state: str = "active",
                   limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().documents.list_documents(
        project_id, status=status, parser=parser, state=state, limit=limit,
        offset=offset)


@app.post("/projects/{project_id}/documents/plan")
def plan_document_import(project_id: str,
                         req: models.DocumentImportReq) -> Dict[str, Any]:
    """What importing this file WOULD do. Stores nothing at all."""
    _reject_conflicting_targets(req)
    return _svc().documents.plan_import(
        project_id, req.path, asset_id=req.asset or "",
        logical_key=req.logical_key or "", new_asset=bool(req.new_asset))


@app.post("/projects/{project_id}/documents/search")
def search_documents(project_id: str,
                     req: models.DocumentSearchReq) -> Dict[str, Any]:
    return _svc().documents.search(
        project_id, req.query, limit=req.limit, asset_type=req.asset_type,
        parser=req.parser, block_type=req.block_type,
        logical_key=req.logical_key, include_removed=req.include_removed)


@app.post("/projects/{project_id}/documents")
def add_document(project_id: str,
                 req: models.DocumentImportReq) -> Dict[str, Any]:
    """Append a document. A duplicate or a filename collision writes nothing and
    says so; only an unambiguous decision creates a job."""
    _reject_conflicting_targets(req)
    return _svc().documents.add_document(
        project_id, req.path, asset_id=req.asset or "",
        logical_key=req.logical_key or "", new_asset=bool(req.new_asset),
        version_label=req.version_label or "", wait=req.wait,
        timeout=req.timeout, dry_run=req.dry_run)


@app.get("/projects/{project_id}/documents/{asset_id}")
def get_document(project_id: str, asset_id: str) -> Dict[str, Any]:
    return _svc().documents.get_document(project_id, asset_id)


@app.get("/projects/{project_id}/documents/{asset_id}/outline")
def get_document_outline(project_id: str, asset_id: str,
                         limit: int = 500) -> Dict[str, Any]:
    """The document's structural outline, for its CURRENT revision."""
    document = _svc().documents.get_document(project_id, asset_id)
    revision = (document.get("current_revision") or {}).get("id", "")
    return _svc().documents.get_outline(project_id, revision, limit=limit)


@app.get("/projects/{project_id}/documents/{asset_id}/related")
def get_document_related(project_id: str, asset_id: str,
                         limit: int = 30) -> Dict[str, Any]:
    """Deterministic candidate associations. Every entry is an observed
    mention labelled `status: "candidate"`, never a confirmed relation."""
    return _svc().documents.find_related_candidates(project_id, asset_id,
                                                    limit=limit)


@app.post("/projects/{project_id}/knowledge/search")
def search_knowledge(project_id: str,
                     req: models.KnowledgeSearchReq) -> Dict[str, Any]:
    return _svc().documents.search_knowledge(
        project_id, req.query, code_limit=req.code_limit,
        document_limit=req.document_limit)


# ---------------------------------------------------------------------------
# Semantic plane (OpenMind v2 Phase 4) — ADDITIVE routes only.
#
# Everything below is workspace-scoped through the service layer, bounded,
# and secret-free: provider profiles are machine-local files, the API key
# VALUE never appears in a request or a response, and no route here can
# trigger a remote model call unless the workspace policy explicitly allows
# remote use. The existing `/projects` naming is kept on purpose.
# ---------------------------------------------------------------------------
@app.get("/providers")
def list_providers() -> Dict[str, Any]:
    """Machine-local provider profiles with their static validation. Read
    only — profiles are configured via the CLI on the machine they live on."""
    from .semantic.providers import profiles as _profiles
    records = _profiles.list_profiles()
    return {"providers": records, "count": len(records)}


@app.get("/providers/{name}")
def get_provider_profile(name: str) -> Dict[str, Any]:
    from .semantic.providers import profiles as _profiles
    profile = _profiles.require_profile(name)
    record = profile.as_dict()
    record["validation"] = _profiles.validate_profile(profile).as_dict()
    record["expected_host"] = _profiles.expected_host(profile)
    return {"provider": record}


@app.get("/projects/{project_id}/semantic/policy")
def get_semantic_policy(project_id: str) -> Dict[str, Any]:
    return {"policy": _svc().semantic.get_policy(project_id)}


@app.post("/projects/{project_id}/semantic/policy")
def set_semantic_policy(project_id: str,
                        req: models.SemanticPolicyReq) -> Dict[str, Any]:
    return {"policy": _svc().semantic.set_policy(
        project_id, data_classification=req.data_classification,
        allow_remote=req.allow_remote,
        provider_profile=req.provider_profile,
        local_cache_enabled=req.local_cache_enabled,
        task_models=req.task_models, budgets=req.budgets)}


@app.post("/projects/{project_id}/semantic/plan")
def semantic_plan(project_id: str,
                  req: models.SemanticAnalysisReq) -> Dict[str, Any]:
    """Deterministic dry-run: no provider call, nothing written."""
    return {"plan": _svc().semantic.plan_analysis(
        project_id, task_types=req.tasks or None, scope=req.scope,
        provider_profile=req.provider, model_tier=req.model_tier,
        budgets=req.budgets, force=req.force)}


@app.post("/projects/{project_id}/semantic/analyses")
def semantic_start(project_id: str,
                   req: models.SemanticAnalysisReq) -> Dict[str, Any]:
    return _svc().semantic.start_analysis(
        project_id, task_types=req.tasks or None, scope=req.scope,
        provider_profile=req.provider, model_tier=req.model_tier,
        budgets=req.budgets, force=req.force, wait=req.wait,
        timeout=req.timeout)


@app.post("/projects/{project_id}/semantic/analyses/{run_id}/resume")
def semantic_resume(project_id: str, run_id: str,
                    req: models.SemanticResumeReq) -> Dict[str, Any]:
    return _svc().semantic.resume_analysis(project_id, run_id,
                                           wait=req.wait,
                                           timeout=req.timeout)


@app.get("/projects/{project_id}/semantic/analyses")
def semantic_runs(project_id: str, limit: int = 50, offset: int = 0,
                  status: Optional[str] = None) -> Dict[str, Any]:
    return _svc().semantic.list_runs(project_id, limit=limit, offset=offset,
                                     status=status)


@app.get("/projects/{project_id}/semantic/analyses/{run_id}")
def semantic_run(project_id: str, run_id: str) -> Dict[str, Any]:
    return {"run": _svc().semantic.get_run(project_id, run_id)}


@app.get("/projects/{project_id}/semantic/analyses/{run_id}/usage")
def semantic_usage(project_id: str, run_id: str) -> Dict[str, Any]:
    return _svc().semantic.get_usage(project_id, run_id)


@app.get("/projects/{project_id}/semantic/candidates")
def semantic_candidates(project_id: str, kind: Optional[str] = None,
                        type: Optional[str] = None,
                        review_status: Optional[str] = None,
                        lifecycle_status: Optional[str] = None,
                        run: Optional[str] = None, limit: int = 100,
                        offset: int = 0) -> Dict[str, Any]:
    return _svc().semantic.list_candidates(
        project_id, candidate_kind=kind, candidate_type=type,
        review_status=review_status, lifecycle_status=lifecycle_status,
        run_id=run, limit=limit, offset=offset)


@app.get("/projects/{project_id}/semantic/candidates/{candidate_id}")
def semantic_candidate(project_id: str, candidate_id: str) -> Dict[str, Any]:
    return {"candidate": _svc().semantic.get_candidate(project_id,
                                                       candidate_id)}


@app.post("/projects/{project_id}/semantic/candidates/{candidate_id}/review")
def semantic_review(project_id: str, candidate_id: str,
                    req: models.SemanticReviewReq) -> Dict[str, Any]:
    """Review changes candidate METADATA only; confirming never creates a
    canonical entity or relation."""
    semantic = _svc().semantic
    review = {"candidate": semantic.review_candidate,
              "relation": semantic.review_relation_candidate,
              "conflict": semantic.review_conflict_candidate}.get(req.kind)
    if review is None:
        raise HTTPException(400, f"unknown candidate kind: {req.kind!r}")
    return {"candidate": review(project_id, candidate_id,
                                decision=req.decision, note=req.note,
                                reviewer=req.reviewer)}


@app.get("/projects/{project_id}/semantic/relations")
def semantic_relations(project_id: str, type: Optional[str] = None,
                       review_status: Optional[str] = None,
                       lifecycle_status: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().semantic.list_relation_candidates(
        project_id, relation_type=type, review_status=review_status,
        lifecycle_status=lifecycle_status, limit=limit, offset=offset)


@app.get("/projects/{project_id}/semantic/conflicts")
def semantic_conflicts(project_id: str, category: Optional[str] = None,
                       review_status: Optional[str] = None,
                       lifecycle_status: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().semantic.list_conflict_candidates(
        project_id, category=category, review_status=review_status,
        lifecycle_status=lifecycle_status, limit=limit, offset=offset)


@app.get("/projects/{project_id}/lenses")
def list_lenses(project_id: str,
                source: Optional[str] = None) -> Dict[str, Any]:
    return _svc().lenses.list_lenses(project_id, source=source)


@app.get("/projects/{project_id}/lenses/{lens_id}")
def get_lens(project_id: str, lens_id: str) -> Dict[str, Any]:
    return {"lens": _svc().lenses.get_lens(project_id, lens_id)}


@app.post("/projects/{project_id}/lenses/induction-plan")
def lens_induction_plan(project_id: str,
                        req: models.LensInductionReq) -> Dict[str, Any]:
    """Deterministic sample plan + policy verdict. No provider call."""
    return {"plan": _svc().lenses.plan_induction(
        project_id, provider_profile=req.provider)}


@app.post("/projects/{project_id}/lenses")
def lens_induce(project_id: str,
                req: models.LensInductionReq) -> Dict[str, Any]:
    """Start a lens induction (one bounded strong-tier request; the result
    is stored PROVISIONAL and needs explicit approval + activation)."""
    return _svc().lenses.start_induction(
        project_id, provider_profile=req.provider, wait=req.wait,
        timeout=req.timeout)


@app.post("/projects/{project_id}/lenses/import")
def lens_import(project_id: str,
                req: models.LensImportReq) -> Dict[str, Any]:
    return {"lens": _svc().lenses.import_organization_lens(project_id,
                                                           req.name)}


@app.post("/projects/{project_id}/lenses/{lens_id}/validate")
def lens_validate(project_id: str, lens_id: str) -> Dict[str, Any]:
    return {"lens": _svc().lenses.validate(project_id, lens_id)}


@app.post("/projects/{project_id}/lenses/{lens_id}/approve")
def lens_approve(project_id: str, lens_id: str) -> Dict[str, Any]:
    return {"lens": _svc().lenses.approve(project_id, lens_id)}


@app.post("/projects/{project_id}/lenses/{lens_id}/reject")
def lens_reject(project_id: str, lens_id: str,
                req: models.LensRejectReq) -> Dict[str, Any]:
    return {"lens": _svc().lenses.reject(project_id, lens_id,
                                         reason=req.reason)}


@app.post("/projects/{project_id}/lenses/{lens_id}/activate")
def lens_activate(project_id: str, lens_id: str) -> Dict[str, Any]:
    return {"lens": _svc().lenses.activate(project_id, lens_id)}


@app.post("/projects/{project_id}/lenses/{lens_id}/deactivate")
def lens_deactivate(project_id: str, lens_id: str) -> Dict[str, Any]:
    return {"lens": _svc().lenses.deactivate(project_id, lens_id)}


# ---------------------------------------------------------------------------
# Canonical Knowledge Graph (OpenMind v2 Phase 5) — ADDITIVE routes only.
#
# Workspace-scoped through the service layer, bounded, typed: there is
# deliberately NO generic graph-mutation endpoint — every write is one
# explicit operation that records a Human Decision and one Knowledge
# Revision. `/projects` naming is kept; the Phase 3 combined search route
# POST /projects/{id}/knowledge/search is UNTOUCHED (graph search lives at
# /knowledge/graph-search).
# ---------------------------------------------------------------------------
@app.get("/projects/{project_id}/knowledge/stats")
def knowledge_stats(project_id: str) -> Dict[str, Any]:
    return _svc().knowledge.get_stats(project_id)


@app.get("/projects/{project_id}/knowledge/revisions")
def knowledge_revisions(project_id: str, limit: int = 50,
                        offset: int = 0) -> Dict[str, Any]:
    return _svc().knowledge.list_knowledge_revisions(
        project_id, limit=limit, offset=offset)


@app.get("/projects/{project_id}/knowledge/revisions/{revision_number}")
def knowledge_revision(project_id: str,
                       revision_number: int) -> Dict[str, Any]:
    return {"revision": _svc().knowledge.get_knowledge_revision(
        project_id, revision_number)}


@app.get("/projects/{project_id}/knowledge/decisions")
def knowledge_decisions(project_id: str, target_kind: Optional[str] = None,
                        target_id: Optional[str] = None,
                        decision_type: Optional[str] = None,
                        limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().knowledge.list_decisions(
        project_id, target_kind=target_kind, target_id=target_id,
        decision_type=decision_type, limit=limit, offset=offset)


@app.post("/projects/{project_id}/knowledge/seed/plan")
def knowledge_seed_plan(project_id: str) -> Dict[str, Any]:
    return {"plan": _svc().knowledge.plan_seed(project_id)}


@app.post("/projects/{project_id}/knowledge/seed")
def knowledge_seed(project_id: str,
                   req: models.GraphActorReq) -> Dict[str, Any]:
    return _svc().knowledge.seed(project_id, actor=req.actor)


@app.post("/projects/{project_id}/knowledge/sync")
def knowledge_sync(project_id: str,
                   req: models.GraphActorReq) -> Dict[str, Any]:
    return _svc().knowledge.sync(project_id, actor=req.actor)


@app.post("/projects/{project_id}/knowledge/reconcile")
def knowledge_reconcile(project_id: str,
                        req: models.GraphActorReq) -> Dict[str, Any]:
    return _svc().knowledge.reconcile_staleness(project_id, actor=req.actor)


@app.post("/projects/{project_id}/knowledge/graph-search")
def knowledge_graph_search(project_id: str,
                           req: models.GraphSearchReq) -> Dict[str, Any]:
    """Search canonical Entities and Claims (exact > lexical > vector). The
    Phase 3 code+document search stays at POST /knowledge/search."""
    return _svc().knowledge.search_entities(
        project_id, req.query, limit=req.limit,
        include_stale=req.include_stale)


@app.get("/projects/{project_id}/knowledge/nodes/{node_id}")
def knowledge_node(project_id: str, node_id: str) -> Dict[str, Any]:
    return {"node": _svc().knowledge.get_node(project_id, node_id)}


@app.post("/projects/{project_id}/knowledge/nodes/{node_id}/expand")
def knowledge_expand(project_id: str, node_id: str,
                     req: models.GraphExpandReq) -> Dict[str, Any]:
    return _svc().knowledge.expand_node(
        project_id, node_id, direction=req.direction,
        relation_types=req.relation_types or None, depth=req.depth,
        node_limit=req.node_limit, edge_limit=req.edge_limit,
        include_stale=req.include_stale)


@app.post("/projects/{project_id}/knowledge/path")
def knowledge_path(project_id: str,
                   req: models.GraphPathReq) -> Dict[str, Any]:
    """Bounded shortest-path discovery. Generic reachability — NOT the
    Phase 6 formal Requirement Traceability."""
    return _svc().knowledge.find_path(
        project_id, req.source, req.target,
        relation_types=req.relation_types or None, direction=req.direction,
        max_depth=req.max_depth, include_stale=req.include_stale)


@app.get("/projects/{project_id}/entities")
def list_entities(project_id: str, entity_type: Optional[str] = None,
                  lifecycle_status: Optional[str] = "active",
                  origin: Optional[str] = None, limit: int = 100,
                  offset: int = 0) -> Dict[str, Any]:
    return _svc().knowledge.list_entities(
        project_id, entity_type=entity_type,
        lifecycle_status=lifecycle_status, origin=origin, limit=limit,
        offset=offset)


@app.get("/projects/{project_id}/entities/{entity_id}")
def get_entity(project_id: str, entity_id: str) -> Dict[str, Any]:
    return {"entity": _svc().knowledge.get_entity(project_id, entity_id)}


@app.post("/projects/{project_id}/entities")
def create_entity(project_id: str,
                  req: models.EntityCreateReq) -> Dict[str, Any]:
    return _svc().knowledge.create_entity(
        project_id, entity_type=req.entity_type,
        canonical_key=req.canonical_key, display_name=req.display_name,
        description=req.description, evidence=req.evidence,
        actor=req.actor, note=req.note, source_command="rest:POST /entities")


@app.post("/projects/{project_id}/entities/{entity_id}/aliases")
def add_entity_alias(project_id: str, entity_id: str,
                     req: models.AliasAddReq) -> Dict[str, Any]:
    return _svc().knowledge.add_alias(
        project_id, entity_id=entity_id, alias=req.alias,
        alias_type=req.alias_type, evidence_id=req.evidence_id,
        actor=req.actor, note=req.note,
        source_command="rest:POST /entities/{id}/aliases")


@app.post("/projects/{project_id}/entities/{entity_id}/merge")
def merge_entity(project_id: str, entity_id: str,
                 req: models.EntityMergeReq) -> Dict[str, Any]:
    return _svc().knowledge.merge_entities(
        project_id, source_entity_id=entity_id,
        target_entity_id=req.target_entity_id, actor=req.actor,
        note=req.note, source_command="rest:POST /entities/{id}/merge")


@app.post("/projects/{project_id}/entities/{entity_id}/split")
def split_entity(project_id: str, entity_id: str,
                 req: models.EntitySplitReq) -> Dict[str, Any]:
    return _svc().knowledge.split_entity(
        project_id, source_entity_id=entity_id,
        new_entity_type=req.new_entity_type,
        new_canonical_key=req.new_canonical_key,
        new_display_name=req.new_display_name, claim_ids=req.claim_ids,
        binding_ids=req.binding_ids,
        relation_rewrites=req.relation_rewrites, actor=req.actor,
        note=req.note, source_command="rest:POST /entities/{id}/split")


@app.post("/projects/{project_id}/entities/{entity_id}/authority")
def entity_authority(project_id: str, entity_id: str,
                     req: models.AuthorityReq) -> Dict[str, Any]:
    return _svc().knowledge.set_authority(
        project_id, kind="entity", object_id=entity_id,
        authority=req.authority, actor=req.actor, note=req.note,
        source_command="rest:POST /entities/{id}/authority")


@app.get("/projects/{project_id}/claims")
def list_claims(project_id: str, entity_id: Optional[str] = None,
                claim_type: Optional[str] = None,
                lifecycle_status: Optional[str] = "active",
                limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().knowledge.list_claims(
        project_id, entity_id=entity_id, claim_type=claim_type,
        lifecycle_status=lifecycle_status, limit=limit, offset=offset)


@app.get("/projects/{project_id}/claims/{claim_id}")
def get_claim(project_id: str, claim_id: str) -> Dict[str, Any]:
    return {"claim": _svc().knowledge.get_claim(project_id, claim_id)}


@app.post("/projects/{project_id}/claims")
def create_claim(project_id: str,
                 req: models.ClaimCreateReq) -> Dict[str, Any]:
    return _svc().knowledge.create_claim(
        project_id, entity_id=req.entity_id, claim_type=req.claim_type,
        statement=req.statement, evidence=req.evidence, actor=req.actor,
        note=req.note, source_command="rest:POST /claims")


@app.get("/projects/{project_id}/relations")
def list_relations(project_id: str, entity_id: Optional[str] = None,
                   relation_type: Optional[str] = None,
                   relation_state: Optional[str] = None,
                   lifecycle_status: Optional[str] = "active",
                   limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    return _svc().knowledge.list_relations(
        project_id, entity_id=entity_id, relation_type=relation_type,
        relation_state=relation_state, lifecycle_status=lifecycle_status,
        limit=limit, offset=offset)


@app.get("/projects/{project_id}/relations/{relation_id}")
def get_relation(project_id: str, relation_id: str) -> Dict[str, Any]:
    return {"relation": _svc().knowledge.get_relation(project_id,
                                                      relation_id)}


@app.post("/projects/{project_id}/relations")
def create_relation(project_id: str,
                    req: models.RelationCreateReq) -> Dict[str, Any]:
    return _svc().knowledge.create_relation(
        project_id, source_entity_id=req.source_entity_id,
        target_entity_id=req.target_entity_id,
        relation_type=req.relation_type,
        relation_state=req.relation_state, confidence=req.confidence,
        evidence=req.evidence, actor=req.actor, note=req.note,
        source_command="rest:POST /relations")


@app.post("/projects/{project_id}/promotions/plan")
def promotion_plan(project_id: str,
                   req: models.PromotionReq) -> Dict[str, Any]:
    return {"plan": _svc().knowledge.plan_candidate_promotion(
        project_id, req.candidate_id)}


@app.post("/projects/{project_id}/promotions")
def promotion_promote(project_id: str,
                      req: models.PromotionReq) -> Dict[str, Any]:
    return _svc().knowledge.promote_candidate(
        project_id, req.candidate_id, actor=req.actor, note=req.note,
        source_command="rest:POST /promotions")


@app.post("/projects/{project_id}/relation-promotions/plan")
def relation_promotion_plan(project_id: str,
                            req: models.RelationPromotionReq
                            ) -> Dict[str, Any]:
    return {"plan": _svc().knowledge.plan_relation_promotion(
        project_id, req.relation_candidate_id)}


@app.post("/projects/{project_id}/relation-promotions")
def relation_promotion_promote(project_id: str,
                               req: models.RelationPromotionReq
                               ) -> Dict[str, Any]:
    return _svc().knowledge.promote_relation(
        project_id, req.relation_candidate_id, actor=req.actor,
        note=req.note, source_command="rest:POST /relation-promotions")


# ---------------------------------------------------------------------------
# Formal Traceability + governed Conflicts (OpenMind v2 Phase 6) — ADDITIVE
# routes only.
#
# Workspace-scoped through the service layer, bounded, typed: there is
# deliberately NO generic trace or conflict mutation endpoint — every write
# is one explicit operation with a caller-supplied actor. Nothing here can
# reach a semantic provider. `/projects` naming is kept.
# ---------------------------------------------------------------------------
@app.get("/projects/{project_id}/traceability/policies")
def trace_policies(project_id: str) -> Dict[str, Any]:
    return _svc().traceability.list_policies(project_id)


@app.get("/projects/{project_id}/traceability/policy")
def trace_policy(project_id: str,
                 name: Optional[str] = None) -> Dict[str, Any]:
    return _svc().traceability.get_policy(project_id, name)


@app.post("/projects/{project_id}/traceability/policy")
def trace_policy_set(project_id: str,
                     req: models.TracePolicySetReq) -> Dict[str, Any]:
    return _svc().traceability.set_workspace_policy(
        project_id, policy_name=req.policy_name, actor=req.actor,
        note=req.note, source_command="rest:POST /traceability/policy")


@app.post("/projects/{project_id}/traceability/policy/validate")
def trace_policy_validate(project_id: str,
                          req: models.TracePolicyValidateReq
                          ) -> Dict[str, Any]:
    return _svc().traceability.validate_policy(project_id, req.document)


@app.post("/projects/{project_id}/traceability/refresh-plan")
def trace_refresh_plan(project_id: str,
                       req: models.TraceRefreshReq) -> Dict[str, Any]:
    return _svc().traceability.plan_refresh(
        project_id, scope=req.scope or None, force=req.force)


@app.post("/projects/{project_id}/traceability/refresh")
def trace_refresh(project_id: str,
                  req: models.TraceRefreshReq) -> Dict[str, Any]:
    return _svc().traceability.refresh(
        project_id, scope=req.scope or None, force=req.force,
        wait=req.wait)


@app.get("/projects/{project_id}/traceability/runs")
def trace_runs(project_id: str, status: Optional[str] = None,
               limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    return _svc().traceability.list_runs(project_id, status=status,
                                         limit=limit, offset=offset)


@app.get("/projects/{project_id}/traceability/runs/{run_id}")
def trace_run(project_id: str, run_id: str) -> Dict[str, Any]:
    return {"run": _svc().traceability.get_run(project_id, run_id)}


@app.get("/projects/{project_id}/traceability/requirements/{entity_id}")
def trace_requirement_route(project_id: str, entity_id: str,
                            include_stale: bool = False,
                            max_paths: int = 10) -> Dict[str, Any]:
    return _svc().traceability.trace_requirement(
        project_id, entity_id, include_stale=include_stale,
        max_paths=max_paths)


@app.get("/projects/{project_id}/traceability/code/{entity_id}")
def trace_code_route(project_id: str, entity_id: str,
                     include_stale: bool = False) -> Dict[str, Any]:
    return _svc().traceability.trace_code(project_id, entity_id,
                                          include_stale=include_stale)


@app.get("/projects/{project_id}/traceability/tests/{entity_id}")
def trace_test_route(project_id: str, entity_id: str,
                     include_stale: bool = False) -> Dict[str, Any]:
    return _svc().traceability.trace_test(project_id, entity_id,
                                          include_stale=include_stale)


@app.get("/projects/{project_id}/traceability/paths/{trace_id}")
def trace_path_route(project_id: str, trace_id: str) -> Dict[str, Any]:
    return {"path": _svc().traceability.get_trace_path(project_id,
                                                       trace_id)}


@app.get("/projects/{project_id}/traceability/coverage")
def trace_coverage(project_id: str) -> Dict[str, Any]:
    return _svc().traceability.get_coverage(project_id)


@app.get("/projects/{project_id}/traceability/gaps")
def trace_gaps(project_id: str, gap_type: Optional[str] = None,
               status: Optional[str] = None,
               root_entity_id: Optional[str] = None,
               severity: Optional[str] = None,
               limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    return _svc().traceability.list_gaps(
        project_id, gap_type=gap_type, status=status,
        root_entity_id=root_entity_id, severity=severity, limit=limit,
        offset=offset)


@app.get("/projects/{project_id}/traceability/gaps/{gap_id}")
def trace_gap(project_id: str, gap_id: str) -> Dict[str, Any]:
    return {"gap": _svc().traceability.get_gap(project_id, gap_id)}


@app.post("/projects/{project_id}/traceability/gaps/{gap_id}/resolve")
def trace_gap_resolve(project_id: str, gap_id: str,
                      req: models.GapGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.resolve_gap(
        project_id, gap_id, actor=req.actor, note=req.note,
        engine_exception=req.engine_exception,
        source_command="rest:POST /traceability/gaps/resolve")


@app.post("/projects/{project_id}/traceability/gaps/{gap_id}/accept")
def trace_gap_accept(project_id: str, gap_id: str,
                     req: models.GapGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.accept_gap(
        project_id, gap_id, actor=req.actor, note=req.note,
        expires_at=req.expires_at,
        source_command="rest:POST /traceability/gaps/accept")


@app.post("/projects/{project_id}/traceability/gaps/{gap_id}/dismiss")
def trace_gap_dismiss(project_id: str, gap_id: str,
                      req: models.GapGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.dismiss_gap(
        project_id, gap_id, actor=req.actor, note=req.note,
        source_command="rest:POST /traceability/gaps/dismiss")


@app.post("/projects/{project_id}/traceability/gaps/{gap_id}/reopen")
def trace_gap_reopen(project_id: str, gap_id: str,
                     req: models.GapGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.reopen_gap(
        project_id, gap_id, actor=req.actor, note=req.note,
        source_command="rest:POST /traceability/gaps/reopen")


@app.get("/projects/{project_id}/traceability/orphans/requirements")
def trace_orphan_requirements(project_id: str,
                              limit: int = 500) -> Dict[str, Any]:
    return _svc().traceability.find_orphan_requirements(project_id,
                                                        limit=limit)


@app.get("/projects/{project_id}/traceability/orphans/code")
def trace_orphan_code(project_id: str, limit: int = 500) -> Dict[str, Any]:
    return _svc().traceability.find_orphan_code(project_id, limit=limit)


@app.get("/projects/{project_id}/traceability/orphans/tests")
def trace_orphan_tests(project_id: str,
                       limit: int = 500) -> Dict[str, Any]:
    return _svc().traceability.find_orphan_tests(project_id, limit=limit)


@app.get("/projects/{project_id}/traceability/orphans/documents")
def trace_orphan_documents(project_id: str,
                           limit: int = 500) -> Dict[str, Any]:
    return _svc().traceability.find_orphan_documents(project_id,
                                                     limit=limit)


@app.post("/projects/{project_id}/conflicts/scan-plan")
def conflict_scan_plan(project_id: str) -> Dict[str, Any]:
    return _svc().traceability.plan_conflict_scan(project_id)


@app.post("/projects/{project_id}/conflicts/scan")
def conflict_scan(project_id: str,
                  req: models.ConflictScanReq) -> Dict[str, Any]:
    return _svc().traceability.scan_conflicts(project_id, actor=req.actor,
                                              wait=req.wait)


@app.get("/projects/{project_id}/conflicts")
def conflicts_list(project_id: str, status: Optional[str] = None,
                   category: Optional[str] = None,
                   origin: Optional[str] = None,
                   severity: Optional[str] = None,
                   limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    """Canonical governed conflicts. The Phase 4 candidate listing stays at
    GET /projects/{id}/semantic/conflicts, untouched."""
    return _svc().traceability.list_conflicts(
        project_id, status=status, category=category, origin=origin,
        severity=severity, limit=limit, offset=offset)


@app.get("/projects/{project_id}/conflicts/{conflict_id}")
def conflict_show(project_id: str, conflict_id: str) -> Dict[str, Any]:
    return {"conflict": _svc().traceability.get_conflict(project_id,
                                                         conflict_id)}


@app.post("/projects/{project_id}/conflict-promotions/plan")
def conflict_promotion_plan(project_id: str,
                            req: models.ConflictPromotionPlanReq
                            ) -> Dict[str, Any]:
    return {"plan": _svc().traceability.plan_conflict_promotion(
        project_id, req.candidate_id)}


@app.post("/projects/{project_id}/conflict-promotions")
def conflict_promotion(project_id: str,
                       req: models.ConflictPromotionReq) -> Dict[str, Any]:
    return _svc().traceability.promote_conflict_candidate(
        project_id, req.candidate_id, actor=req.actor, note=req.note,
        source_command="rest:POST /conflict-promotions")


@app.post("/projects/{project_id}/conflicts/{conflict_id}/review")
def conflict_review(project_id: str, conflict_id: str,
                    req: models.ConflictGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.start_conflict_review(
        project_id, conflict_id, actor=req.actor, note=req.note,
        source_command="rest:POST /conflicts/review")


@app.post("/projects/{project_id}/conflicts/{conflict_id}/accept-risk")
def conflict_accept_risk(project_id: str, conflict_id: str,
                         req: models.ConflictGovernanceReq
                         ) -> Dict[str, Any]:
    return _svc().traceability.accept_conflict_risk(
        project_id, conflict_id, actor=req.actor, note=req.note,
        expires_at=req.expires_at, follow_up=req.follow_up,
        source_command="rest:POST /conflicts/accept-risk")


@app.post("/projects/{project_id}/conflicts/{conflict_id}/resolve")
def conflict_resolve(project_id: str, conflict_id: str,
                     req: models.ConflictGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.resolve_conflict(
        project_id, conflict_id, actor=req.actor, note=req.note,
        resolution_type=req.resolution_type, evidence=req.evidence,
        source_command="rest:POST /conflicts/resolve")


@app.post("/projects/{project_id}/conflicts/{conflict_id}/dismiss")
def conflict_dismiss(project_id: str, conflict_id: str,
                     req: models.ConflictGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.dismiss_conflict(
        project_id, conflict_id, actor=req.actor, note=req.note,
        source_command="rest:POST /conflicts/dismiss")


@app.post("/projects/{project_id}/conflicts/{conflict_id}/reopen")
def conflict_reopen(project_id: str, conflict_id: str,
                    req: models.ConflictGovernanceReq) -> Dict[str, Any]:
    return _svc().traceability.reopen_conflict(
        project_id, conflict_id, actor=req.actor, note=req.note,
        source_command="rest:POST /conflicts/reopen")


# ---------------------------------------------------------------------------
# Git change intelligence + overlays (v2 Phase 7) — additive, read-only wrt Git
# and canonical data. No generic git endpoint; no endpoint accepts arbitrary
# git arguments; every list is bounded.
# ---------------------------------------------------------------------------
@app.get("/projects/{project_id}/git/repositories")
def git_repositories(project_id: str, discover: bool = False) -> Dict[str, Any]:
    if discover:
        return _svc().git.discover_repositories(project_id)
    return _svc().git.list_repositories(project_id)


@app.get("/projects/{project_id}/git/repositories/{repository_id}")
def git_repository(project_id: str, repository_id: str) -> Dict[str, Any]:
    return _svc().git.get_repository(project_id, repository_id)


@app.get("/projects/{project_id}/git/status")
def git_status(project_id: str, repository: str) -> Dict[str, Any]:
    return _svc().git.get_repository_status(project_id, repository)


@app.post("/projects/{project_id}/git/baselines/plan")
def git_baseline_plan(project_id: str,
                      body: Dict[str, Any] = None) -> Dict[str, Any]:
    body = body or {}
    return _svc().git.plan_baseline(project_id, body.get("repository"))


@app.post("/projects/{project_id}/git/baselines")
def git_baseline_capture(project_id: str,
                         body: Dict[str, Any] = None) -> Dict[str, Any]:
    body = body or {}
    return _svc().git.capture_baseline(project_id, body.get("repository"),
                                       actor=body.get("actor", ""))


@app.get("/projects/{project_id}/git/baselines")
def git_baselines(project_id: str,
                  repository: Optional[str] = None) -> Dict[str, Any]:
    return _svc().git.list_baselines(project_id, repository)


@app.get("/projects/{project_id}/git/baselines/{baseline_id}")
def git_baseline(project_id: str, baseline_id: str) -> Dict[str, Any]:
    return _svc().git.get_baseline(project_id, baseline_id)


@app.post("/projects/{project_id}/overlays/plan")
def overlays_plan(project_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _svc().overlays.plan_overlay(
        project_id, kind=body.get("kind", ""),
        repositories=body.get("repositories", []))


@app.post("/projects/{project_id}/overlays")
def overlays_create(project_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _svc().overlays.create_overlay(
        project_id, kind=body.get("kind", ""),
        repositories=body.get("repositories", []),
        name=body.get("name", ""), options=body.get("options"),
        allow_partial_repositories=bool(body.get("allow_partial_repositories")))


@app.get("/projects/{project_id}/overlays")
def overlays_list(project_id: str,
                  state: Optional[str] = None) -> Dict[str, Any]:
    return _svc().overlays.list_overlays(project_id, state=state)


@app.get("/projects/{project_id}/overlays/{overlay_id}")
def overlay_show(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.get_overlay(project_id, overlay_id)


@app.post("/projects/{project_id}/overlays/{overlay_id}/refresh")
def overlay_refresh(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.refresh_overlay(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/files")
def overlay_files(project_id: str, overlay_id: str,
                  change_type: Optional[str] = None) -> Dict[str, Any]:
    return _svc().overlays.list_overlay_files(project_id, overlay_id,
                                              change_type=change_type)


@app.get("/projects/{project_id}/overlays/{overlay_id}/files/{file_id}")
def overlay_file(project_id: str, overlay_id: str,
                 file_id: str) -> Dict[str, Any]:
    return _svc().overlays.get_overlay_file(project_id, overlay_id, file_id)


@app.post("/projects/{project_id}/overlays/{overlay_id}/search")
def overlay_search(project_id: str, overlay_id: str,
                   body: Dict[str, Any]) -> Dict[str, Any]:
    return _svc().overlays.search_overlay(
        project_id, overlay_id, body.get("query", ""),
        limit=int(body.get("limit", 10)))


@app.get("/projects/{project_id}/overlays/{overlay_id}/evidence/{evidence_id}")
def overlay_evidence(project_id: str, overlay_id: str,
                     evidence_id: str) -> Dict[str, Any]:
    return _svc().overlays.get_overlay_evidence(project_id, overlay_id,
                                                evidence_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/impact")
def overlay_impact(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.get_impact_report(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/requirements")
def overlay_requirements(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.list_impacted_requirements(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/tests")
def overlay_tests(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.list_impacted_tests(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/traces")
def overlay_traces(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.list_trace_impacts(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/gaps")
def overlay_gaps(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.list_gap_impacts(project_id, overlay_id)


@app.get("/projects/{project_id}/overlays/{overlay_id}/conflicts")
def overlay_conflicts(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.list_conflict_impacts(project_id, overlay_id)


@app.post("/projects/{project_id}/overlays/{overlay_id}/close")
def overlay_close(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.close_overlay(project_id, overlay_id)


@app.post("/projects/{project_id}/overlays/{overlay_id}/abandon")
def overlay_abandon(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.abandon_overlay(project_id, overlay_id)


@app.delete("/projects/{project_id}/overlays/{overlay_id}")
def overlay_delete(project_id: str, overlay_id: str) -> Dict[str, Any]:
    return _svc().overlays.delete_overlay(project_id, overlay_id)


@app.post("/projects/{project_id}/overlays/{overlay_id}/reconcile")
def overlay_reconcile(project_id: str, overlay_id: str,
                      body: Dict[str, Any] = None) -> Dict[str, Any]:
    body = body or {}
    return _svc().overlays.reconcile_overlay(
        project_id, overlay_id, actor=body.get("actor", ""),
        note=body.get("note", ""))


def _reject_conflicting_targets(req: models.DocumentImportReq) -> None:
    """``asset`` / ``logical_key`` / ``new_asset`` name three different targets
    for one set of bytes; combining them has no single correct meaning, so it is
    a 400 rather than a silent precedence rule."""
    chosen = [name for name, value in (("asset", req.asset),
                                       ("logical_key", req.logical_key),
                                       ("new_asset", req.new_asset)) if value]
    if len(chosen) > 1:
        raise HTTPException(
            400, f"asset, logical_key and new_asset are mutually exclusive "
                 f"(got: {', '.join(chosen)})")


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
    # Enqueue-only, exactly as before: the HTTP client polls /jobs or the SSE
    # stream. The service's path-registration guard is deliberately NOT applied
    # here — it would turn a currently-accepted request into a 400.
    runtime = _svc()
    runtime.workspaces.get(req.project_id)          # 404 for an unknown project
    job = runtime.jobs.enqueue_ingest(req.project_id, req.path)
    return {"job_id": job["job_id"], "job": job}


@app.post("/gendocs")
def gendocs(req: models.GendocsReq) -> Dict[str, Any]:
    runtime = _svc()
    runtime.workspaces.get(req.project_id)
    job = runtime.jobs.enqueue_gendocs(req.project_id)
    return {"job_id": job["job_id"], "job": job}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.get("/jobs")
def list_jobs(scope_id: Optional[str] = Query(None, alias="scope")) -> Dict[str, Any]:
    pids = None
    if scope_id:
        # '__none__' is a sentinel that matches no project, so an unresolvable
        # scope lists nothing instead of silently listing EVERY project's jobs.
        pids = scope.resolve(scope_id) or ["__none__"]
    return {"jobs": _svc().jobs.list(pids)}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    return _svc().jobs.get(job_id)


@app.post("/jobs/{job_id}/pause")
def pause_job(job_id: str) -> Dict[str, Any]:
    return _svc().jobs.pause(job_id)


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str) -> Dict[str, Any]:
    return _svc().jobs.resume(job_id)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    """Cancel a queued/running job (used by the Ask queue; Part 2)."""
    return _svc().jobs.cancel(job_id)


@app.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    async def gen():
        last = None
        idle = 0
        while True:
            # off the event loop: a slow SQLite read must not stall other
            # clients' streams (or any other async endpoint) for its duration
            job = await asyncio.to_thread(db.get_job, job_id)
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


def _attach_roles(nodes: Optional[List[Dict[str, Any]]],
                  roles: Dict[str, Dict[str, Any]]) -> None:
    """Tag graph nodes with the template role of their file (node ids are the
    repo-relative paths the facts map is keyed by). No template facts -> no-op,
    nodes keep their pre-template shape."""
    for n in nodes or []:
        hit = roles.get(n.get("id") or "")
        if hit:
            n["role"] = hit.get("role")


@app.get("/structure")
def get_structure(scope_id: str = Query(..., alias="scope")) -> Dict[str, Any]:
    """Deterministic structure overview for the scope (archetype + stats + entry
    points + top modules). The basis of the Graphs view."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    ov = structure.overview(doc)
    ov["archetype"] = diagrams._archetype(doc)
    ov["generated_at"] = doc.get("generated_at")
    ov["has_data"] = bool(doc.get("files"))
    fdoc = mapio.merged_facts(pids)
    ov["template"] = None
    if fdoc.get("template"):
        ov["template"] = {"name": (fdoc["template"] or {}).get("name"),
                          "layers": fdoc.get("role_defs") or [],
                          "facets": fdoc.get("facets") or []}
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
    res = diagrams.call_roots(doc, gbyfile=_glossary_by_file(pids))
    _attach_roles(res.get("roots"), mapio.merged_facts(pids).get("roles") or {})
    return res


@app.get("/graph/children")
def get_graph_children(scope_id: str = Query(..., alias="scope"),
                       id: str = Query(...), offset: int = 0, limit: int = 7) -> Dict[str, Any]:
    """Callees of one mind-map node, paginated (show `limit`, then '+more')."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    res = diagrams.call_children(doc, id, offset=offset, limit=limit,
                                 gbyfile=_glossary_by_file(pids))
    _attach_roles(res.get("children"), mapio.merged_facts(pids).get("roles") or {})
    return res


@app.get("/graph/node")
def get_graph_node(scope_id: str = Query(..., alias="scope"),
                   id: str = Query(...)) -> Dict[str, Any]:
    """Detail for a clicked node: source location, defs, call neighbors, glossary."""
    pids = _scope_pids(scope_id)
    doc = mapio.merged_structure(pids)
    res = diagrams.node_detail(doc, id, gbyfile=_glossary_by_file(pids))
    fdoc = mapio.merged_facts(pids)
    hit = (fdoc.get("roles") or {}).get(id)
    if hit:
        res["role"] = hit          # {"role", "via", "line"} — the full evidence
    facts = [f for f in (fdoc.get("facts") or []) if f.get("file") == id]
    if facts:
        res["facets"] = facts[:20]
    return res


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
    buf = bytearray()
    while chunk := await file.read(1 << 20):
        buf += chunk
        if len(buf) > config.OCR_MAX_UPLOAD_BYTES:
            raise HTTPException(413, "image too large (limit "
                                     f"{config.OCR_MAX_UPLOAD_BYTES // 1_000_000} MB)")
    return askmod.ocr_image(bytes(buf), file.filename or "image")


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
