"""Background job engine — persistent, resumable, reconnectable (Invariant 6).

A single server-side worker thread processes one job at a time. Every tick is
persisted to SQLite, so progress survives tab-close and is reconnectable; SSE
streams by polling that persisted state (so it reconnects trivially). On server
restart, running jobs are marked ``interrupted`` and are one-click resumable.

Ingest is incremental + idempotent (Invariant 12), pauses only at file boundaries with
the per-file hash set as the checkpoint (Invariant 4), and Terminate performs a true
full wipe to init (Invariant 5).
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (ask as askmod, cases, config, content_store, conversation, db,
               docs as docsmod, embeddings, glossary, llm_client, machine, mapio,
               rag, resources, segmentation, structure, vectorstore, walker,
               wikienrich)
from .domain.types import AssetType
from .model_server import server as model_server

_wake = threading.Event()
_worker_started = False
_current_job_id: Optional[str] = None
_deleting_lock = threading.Lock()
_deleting: set = set()     # project_ids with an in-flight background delete-cleanup
# set by the app's lifespan on shutdown: background cleanup stops at its next
# small batch so Ctrl+C exits promptly; 'deleting' tombstones resume next start.
_shutdown = threading.Event()
# if set, the model server was stopped to free the GPU for embedding and must be
# restarted once the current job ends (handled in the worker loop's finally).
_resume_model_cfg: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------
def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    _shutdown.clear()
    _recover_on_restart()
    reconcile_enrichment()   # completeness backstop after any restart/crash
    # Semantic-staleness backstop (spec §22): a crash between a revision
    # commit and its reconciliation would otherwise leave candidates falsely
    # active until the next ingest. Incremental + indexed, so this is cheap.
    for _p in db.list_projects():
        reconcile_semantic_staleness(_p["id"])
    t = threading.Thread(target=_worker_loop, name="openmind-job-worker", daemon=True)
    t.start()


def shutdown_requested() -> bool:
    """Public predicate for the shutdown flag, so other modules can pass a
    ``cancel`` callback into long drains without importing a private Event."""
    return _shutdown.is_set()


def begin_shutdown() -> None:
    """Called from the app lifespan on shutdown. Interrupts any background
    delete-cleanup at its next batch (the 'deleting' tombstone makes it resume
    on the next start), so process exit is prompt instead of waiting behind a
    long storage drain."""
    _shutdown.set()
    _wake.set()


def _recover_on_restart() -> None:
    """Mark previously-running jobs interrupted + resumable (Invariant 6)."""
    for job in db.list_jobs(status="running"):
        # an in-flight Ask can't be resumed (its stream is gone) — fail it cleanly
        # so the persisted exchange doesn't sit "running" forever after a restart.
        if job["type"] == "ask":
            db.update_job(job["job_id"], status="failed",
                          error="interrupted by server restart", finished_at=db.now())
            if job.get("path"):
                db.ask_update(job["path"], status="error",
                              payload={"error": "interrupted by server restart"})
            continue
        db.update_job(job["job_id"], status="interrupted",
                      error="server restarted mid-job",
                      log_tail=(job["log_tail"] + ["[restart] marked interrupted; resumable"])[-200:])
        p = db.get_project(job["project_id"])
        if p and p["state"] == "learning":
            db.set_project_state(job["project_id"], "paused")
    # resume any async delete that was interrupted mid-cleanup by the restart
    for p in db.list_projects(state="deleting"):
        _spawn_cleanup(p["id"])


def _worker_loop() -> None:
    global _current_job_id
    while True:
        job = _next_queued()
        if not job:
            _wake.wait(timeout=1.0)
            _wake.clear()
            continue
        _current_job_id = job["job_id"]
        try:
            db.update_job(job["job_id"], status="running", started_at=db.now(),
                          control={"requested": None}, error="")
            if job["type"] == "ingest":
                _run_ingest(job["job_id"])
            elif job["type"] == "document_ingest":
                _run_document_ingest(job["job_id"])
            elif job["type"] == "gendocs":
                _run_gendocs(job["job_id"])
            elif job["type"] == "ask":
                _run_ask(job["job_id"])
            elif job["type"] == "enrich":
                _run_enrich(job["job_id"])
            elif job["type"] == "semantic_analysis":
                _run_semantic_analysis(job["job_id"])
            elif job["type"] == "lens_induction":
                _run_lens_induction(job["job_id"])
            else:
                db.update_job(job["job_id"], status="failed",
                              error=f"unknown job type {job['type']}")
        except _PauseSignal:
            pass  # handled inside
        except _TerminateSignal:
            pass
        except Exception as exc:  # noqa
            tb = traceback.format_exc()
            _log(job["job_id"], f"[error] {exc}")
            db.update_job(job["job_id"], status="failed", error=str(exc),
                          finished_at=db.now())
            p = db.get_project(job["project_id"])
            if p and p["state"] == "learning":
                db.set_project_state(job["project_id"], "paused")
            print(tb)
        finally:
            _current_job_id = None
            _maybe_resume_model()   # restart the model if ingest freed the GPU


def _maybe_resume_model() -> None:
    """Restart the model server if it was stopped to free the GPU for embedding.
    Runs after EVERY job but is a no-op unless an ingest set the resume flag — so
    the model comes back on success, terminate, pause, or error alike."""
    global _resume_model_cfg
    cfg = _resume_model_cfg
    if cfg is None:
        return
    _resume_model_cfg = None
    try:
        model_server.start(cfg)
    except Exception:
        pass


def _free_gpu_for_embed(job_id: str) -> bool:
    """Ingest never uses the LLM, so if OUR managed model server is resident on the
    GPU (holding VRAM, idle), stop it for the embed phase so DirectML can use the
    GPU (~5× faster than CPU); the worker loop restarts it afterwards. Returns True
    if we stopped it. Never stops an ATTACHED external server, and respects an
    explicit OPENMIND_EMBED_DEVICE=cpu."""
    global _resume_model_cfg
    if not config.INGEST_FREE_GPU:
        return False
    if os.environ.get("OPENMIND_EMBED_DEVICE", "smart").strip().lower() == "cpu":
        return False
    st = model_server.status()
    if st.get("attached") or st.get("status") not in ("ready", "loading", "starting"):
        return False
    _log(job_id, "[embeddings] model server is resident on the GPU but idle — stopping "
                 "it to free VRAM for fast GPU embedding (auto-restarts after learning).")
    _resume_model_cfg = db.get_model_config()
    model_server.stop()
    deadline = time.time() + 12
    while (time.time() < deadline and
           model_server.status().get("status") not in ("stopped", "not_started")):
        time.sleep(0.3)
    return True


def _next_queued() -> Optional[Dict[str, Any]]:
    q = [j for j in db.list_jobs(status="queued")]
    q.sort(key=lambda j: j["created_at"])
    return q[0] if q else None


# ---------------------------------------------------------------------------
# Control signals
# ---------------------------------------------------------------------------
class _PauseSignal(Exception):
    pass


class _TerminateSignal(Exception):
    pass


def _control(job_id: str) -> Optional[str]:
    j = db.get_job(job_id)
    return (j or {}).get("control", {}).get("requested")


def _checkpoint(job_id: str) -> None:
    """Raise at a file boundary if pause/terminate was requested."""
    req = _control(job_id)
    if req == "terminate":
        # transition out of 'running' immediately so terminate_project's wait
        # loop observes the stop within one poll instead of blocking ~20s.
        db.update_job(job_id, status="failed", error="terminated by user",
                      control={"requested": None}, finished_at=db.now())
        raise _TerminateSignal()
    if req == "pause":
        job = db.get_job(job_id)
        db.update_job(job_id, status="paused",
                      control={"requested": None})
        if job:
            db.set_project_state(job["project_id"], "paused")
            _log(job_id, "[pause] checkpoint reached; partial data kept; resumable.")
        raise _PauseSignal()


def _log(job_id: str, msg: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    tail = (job["log_tail"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]
    db.update_job(job_id, log_tail=tail)


def _progress(job_id: str, **counts: Any) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    prog = dict(job["progress"])
    prog.update(counts)
    db.update_job(job_id, progress=prog)


# ---------------------------------------------------------------------------
# Enqueue API
# ---------------------------------------------------------------------------
def enqueue_ingest(project_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    # Dedup: a single worker can only run one ingest at a time, so a second
    # learn-click while one is already queued/running just piled jobs up behind it
    # ("waiting for worker" forever). Reuse the in-flight ingest instead.
    for j in db.list_jobs(project_ids=[project_id]):
        if j["type"] == "ingest" and j["status"] in ("queued", "running"):
            db.set_project_state(project_id, "learning")
            _wake.set()
            return j
    # store the optional single-path filter RELATIVE so the (portable) jobs table
    # carries no absolute machine path; _project_path_specs re-matches relatively.
    rel_path = machine.to_rel(project_id, path) if path else path
    job = db.create_job(project_id, "ingest", rel_path)
    db.set_project_state(project_id, "learning")
    _wake.set()
    return job


def enqueue_gendocs(project_id: str) -> Dict[str, Any]:
    job = db.create_job(project_id, "gendocs", None)
    _wake.set()
    return job


#: Keys a document-import payload may carry. Anything else is dropped rather
#: than persisted: the payload lives in the PORTABLE database, so an accidental
#: absolute path added by a future caller must not silently travel with the data.
_DOCUMENT_PAYLOAD_KEYS = frozenset({
    "staged_blob_hash", "original_filename", "requested_asset_id",
    "requested_logical_key", "import_mode", "source_kind", "version_label",
    "parser_options",
})


def enqueue_document_ingest(project_id: str,
                            payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue the import of one already-staged document (v2 Phase 3).

    Unlike ``enqueue_ingest`` this does NOT dedupe by workspace: two different
    documents appended in quick succession are two separate pieces of work, and
    collapsing them would silently drop one.
    """
    safe = {k: v for k, v in (payload or {}).items()
            if k in _DOCUMENT_PAYLOAD_KEYS}
    if not safe.get("staged_blob_hash"):
        raise ValueError("document_ingest payload needs a staged_blob_hash")
    filename = str(safe.get("original_filename") or "")
    if "/" in filename or "\\" in filename or (len(filename) > 1
                                               and filename[1] == ":"):
        # A bare NAME is fine to store; a PATH is a machine trace and must never
        # reach the portable database.
        raise ValueError("document_ingest payload must not contain a path")
    job = db.create_job(project_id, "document_ingest", None)
    db.set_job_payload(job["job_id"], safe)
    _wake.set()
    return db.get_job(job["job_id"])


# ---------------------------------------------------------------------------
# Wikipedia enrichment (deterministic pipeline guarantee + crash recovery)
# ---------------------------------------------------------------------------
def _enrich_enabled(project: Optional[Dict[str, Any]]) -> bool:
    return bool((project or {}).get("meta", {}).get("enrich_enabled", True))


def _enrich_context(project: Optional[Dict[str, Any]]) -> str:
    meta = (project or {}).get("meta", {})
    return (meta.get("enrich_context") or wikienrich.infer_context(project) or "").strip()


def _enrich_pins(project: Optional[Dict[str, Any]]) -> Dict[str, str]:
    pins = (project or {}).get("meta", {}).get("enrich_pins") or {}
    return {str(k): str(v) for k, v in pins.items() if k and v}


def _enrich_block(project: Optional[Dict[str, Any]]) -> List[str]:
    return [str(b) for b in ((project or {}).get("meta", {}).get("enrich_block") or []) if b]


def enqueue_enrich(project_id: str) -> Optional[Dict[str, Any]]:
    """Queue a Wikipedia-enrichment job for a project, unless disabled or one is
    already pending/running. Returns the job (or the existing one), or None."""
    p = db.get_project(project_id)
    if not p or not _enrich_enabled(p):
        return None
    for j in db.list_jobs(status="queued") + db.list_jobs(status="running"):
        if j["project_id"] == project_id and j["type"] == "enrich":
            return j
    job = db.create_job(project_id, "enrich", None)
    _wake.set()
    return job


def reconcile_enrichment() -> int:
    """Completeness backstop (crash / network failure / server shutdown recovery):
    for every READY, enrich-enabled project whose glossary still has un-attempted
    terms, (re)queue an enrichment job. Because enrichment is incremental and marks
    each term attempted, this resumes exactly the work that did not finish — and is
    a no-op once a project is fully enriched. Called on startup and after ingest."""
    queued = 0
    try:
        projects = db.list_projects()
    except Exception:
        return 0
    for p in projects:
        if p.get("state") != "ready" or not _enrich_enabled(p):
            continue
        try:
            doc = mapio.load_glossary(p["id"])
        except Exception:
            continue
        if glossary.unattempted_terms(doc):
            if enqueue_enrich(p["id"]):
                queued += 1
    return queued


#: Keys a semantic-analysis payload may carry — identifiers and options ONLY
#: (spec §24). Source or document content in a job row would put project text
#: into the portable database; anything outside this list is dropped.
_SEMANTIC_PAYLOAD_KEYS = frozenset({
    "analysis_run_id", "workspace_id", "task_types", "scope",
    "provider_profile", "model_tier", "budget_overrides", "force",
})


def enqueue_semantic_analysis(project_id: str,
                              payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue one semantic analysis run (v2 Phase 4). The run row (created by
    the service) is the source of truth; the payload carries only its id plus
    the safe option echo."""
    safe = {k: v for k, v in (payload or {}).items()
            if k in _SEMANTIC_PAYLOAD_KEYS}
    if not safe.get("analysis_run_id"):
        raise ValueError("semantic_analysis payload needs an analysis_run_id")
    job = db.create_job(project_id, "semantic_analysis", None)
    db.set_job_payload(job["job_id"], safe)
    _wake.set()
    return db.get_job(job["job_id"])


def _run_semantic_analysis(job_id: str) -> None:
    """Execute one semantic analysis run inside the single worker.

    The heavy lifting lives in :mod:`openmind.semantic.runner`; this wrapper
    wires the job engine's pause/terminate checkpoints and progress
    persistence into it. Imported lazily so a build with the semantic plane
    somehow unavailable degrades only this job type.
    """
    from .semantic import runner as semantic_runner
    from .semantic import store as semantic_store
    from .semantic.models import SemanticRunStatus

    job = db.get_job(job_id)
    payload = db.get_job_payload(job_id)
    run_id = str(payload.get("analysis_run_id") or "")
    workspace_id = str(payload.get("workspace_id") or job["project_id"])
    db.update_job(job_id, step="running-analysis")

    def _tick(**counters: Any) -> None:
        _progress(job_id, **counters)

    try:
        summary = semantic_runner.execute_run(
            run_id, workspace_id,
            checkpoint=lambda: _checkpoint(job_id),
            log=lambda message: _log(job_id, message),
            progress=_tick)
    except _TerminateSignal:
        semantic_store.update_run(run_id,
                                  status=SemanticRunStatus.CANCELLED,
                                  error="terminated by user",
                                  finished_at=db.now())
        raise
    except _PauseSignal:
        # Targets are checkpointed; the run row stays 'running' and resumes.
        raise
    db.update_job(job_id, status="done", step="done", finished_at=db.now(),
                  progress={**(db.get_job(job_id) or {}).get("progress", {}),
                            "summary_status": summary.get("status", "")})


def enqueue_lens_induction(project_id: str,
                           payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue one Project-Lens induction (v2 Phase 4): a single bounded
    strong-tier request whose result is stored PROVISIONAL. Payload carries
    identifiers only, like every Phase 4 job."""
    safe = {k: v for k, v in (payload or {}).items()
            if k in _SEMANTIC_PAYLOAD_KEYS}
    if not safe.get("analysis_run_id"):
        raise ValueError("lens_induction payload needs an analysis_run_id")
    job = db.create_job(project_id, "lens_induction", None)
    db.set_job_payload(job["job_id"], safe)
    _wake.set()
    return db.get_job(job["job_id"])


def _run_lens_induction(job_id: str) -> None:
    from .semantic.lenses import induction
    from .semantic import store as semantic_store
    from .semantic.models import SemanticRunStatus

    job = db.get_job(job_id)
    payload = db.get_job_payload(job_id)
    run_id = str(payload.get("analysis_run_id") or "")
    workspace_id = str(payload.get("workspace_id") or job["project_id"])
    db.update_job(job_id, step="inducing-lens")
    try:
        result = induction.execute_induction(run_id=run_id,
                                             workspace_id=workspace_id)
    except _TerminateSignal:
        semantic_store.update_run(run_id,
                                  status=SemanticRunStatus.CANCELLED,
                                  error="terminated by user",
                                  finished_at=db.now())
        raise
    if result.get("status") == SemanticRunStatus.DONE:
        db.update_job(job_id, status="done", step="done",
                      finished_at=db.now(),
                      progress={"lens_id": result.get("lens_id", ""),
                                "validation_result":
                                    result.get("validation_result", "")})
    else:
        db.update_job(job_id, status="failed", step="done",
                      error=str(result.get("errors") or
                                result.get("error") or "induction failed")[:500],
                      finished_at=db.now())


def reconcile_semantic_staleness(project_id: str) -> None:
    """Post-commit staleness hook (spec §22): mark semantic candidates whose
    source revisions moved on as stale. Best-effort — a reconciliation
    failure must never fail the ingest that triggered it."""
    try:
        from .semantic import store as semantic_store
        result = semantic_store.reconcile_staleness(project_id)
        if any(result.values()):
            print(f"[semantic] staleness reconciled for {project_id}: "
                  f"{result}", flush=True)
    except Exception as exc:
        print(f"[semantic] staleness reconciliation failed for "
              f"{project_id}: {exc}", flush=True)


def enqueue_ask(scope_id: str, pids: List[str], project_id: str,
                scope_desc: Dict[str, Any], question: str,
                attachments: Optional[List[Dict[str, Any]]] = None,
                k: int = 12) -> Dict[str, Any]:
    """Submit an Ask through the SAME single-task worker (Part 2). It PENDS if any
    job is already running; the exchange is persisted (Part 1) before it runs."""
    ex = conversation.create(scope_id, project_id, pids, scope_desc,
                             question, attachments, k)
    # the job's free 'path' column carries the exchange id the worker will run
    job = db.create_job(project_id, "ask", ex["id"])
    db.ask_update(ex["id"], job_id=job["job_id"])
    _wake.set()
    return {"job_id": job["job_id"], "exchange_id": ex["id"], "status": "queued"}


def cancel_job(job_id: str) -> Dict[str, Any]:
    """Cancel a job. A QUEUED job is removed from the queue immediately; a RUNNING
    job is signalled to stop at its next checkpoint / streamed token (Part 2)."""
    job = db.get_job(job_id)
    if not job:
        raise KeyError("job not found")
    if job["status"] == "queued":
        db.update_job(job_id, status="failed", error="cancelled by user",
                      finished_at=db.now())
        if job["type"] == "ask" and job.get("path"):
            db.ask_update(job["path"], status="cancelled", payload={"answer": ""})
    elif job["status"] == "running":
        db.update_job(job_id, control={"requested": "terminate"})
    return db.get_job(job_id)


def pause(job_id: str) -> Dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise KeyError("job not found")
    if job["status"] == "queued":
        db.update_job(job_id, status="paused")
        db.set_project_state(job["project_id"], "paused")
    elif job["status"] == "running":
        db.update_job(job_id, control={"requested": "pause"})
    return db.get_job(job_id)


def resume(job_id: str) -> Dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise KeyError("job not found")
    if job["status"] in ("paused", "interrupted", "failed"):
        db.update_job(job_id, status="queued", control={"requested": None}, error="")
        db.set_project_state(job["project_id"], "learning")
        _wake.set()
    return db.get_job(job_id)


def _signal_stop_jobs(project_id: str) -> None:
    """FAST part of stopping a project's jobs: signal a RUNNING job to stop at its
    next checkpoint and IMMEDIATELY fail queued/paused/interrupted ones. NO wait —
    the (bounded) wait for a running job to actually stop happens off the request
    path (in _wait_jobs_stopped / the cleanup thread). Cancels the linked Ask too."""
    for job in db.list_jobs(project_ids=[project_id]):
        if job["status"] == "running":
            db.update_job(job["job_id"], control={"requested": "terminate"})
        elif job["status"] in ("queued", "paused", "interrupted"):
            db.update_job(job["job_id"], status="failed", error="terminated by user",
                          control={"requested": "terminate"}, finished_at=db.now())
            if job["type"] == "ask" and job.get("path"):
                db.ask_update(job["path"], status="cancelled", payload={"answer": ""})
    _wake.set()


def _wait_jobs_stopped(project_id: str, timeout: float = 20.0) -> None:
    """Bounded wait for a running job to leave 'running' (its checkpoints ack within
    one file / one embed batch), then force-fail any straggler. Used off the request
    path so a slow in-flight embed never freezes the UI."""
    deadline = time.time() + timeout
    while time.time() < deadline and not _shutdown.is_set():
        if not [j for j in db.list_jobs(project_ids=[project_id])
                if j["status"] == "running"]:
            break
        time.sleep(0.1)
    for job in db.list_jobs(project_ids=[project_id]):
        if job["status"] in ("running", "queued", "paused", "interrupted"):
            db.update_job(job["job_id"], status="failed", error="terminated by user",
                          control={"requested": "terminate"}, finished_at=db.now())
            if job["type"] == "ask" and job.get("path"):
                db.ask_update(job["path"], status="cancelled")


def _stop_project_jobs(project_id: str) -> None:
    """Signal + bounded wait (used by terminate, which must finish the wipe before
    the project can be re-learned)."""
    _signal_stop_jobs(project_id)
    _wait_jobs_stopped(project_id)


def terminate_project(project_id: str, clear_cases: bool = False) -> Dict[str, Any]:
    """Stop any running job + FULL wipe to init (Invariant 5)."""
    _stop_project_jobs(project_id)

    # 1) drop the code AND document collections (delete-only; the next learn
    # recreates them lazily — recreating an empty collection now is wasted work
    # on a large project). cancel= matters even though this one is synchronous:
    # without it a Ctrl+C during a large Terminate waits out the entire drain,
    # which is exactly the dead-Ctrl+C symptom the batched drain was introduced
    # to remove.
    vectorstore.get_code_store(project_id).drop(cancel=_shutdown.is_set)
    vectorstore.drop_collection(vectorstore.documents_collection_name(project_id),
                                cancel=_shutdown.is_set)
    # 2) delete map/* and docs/*
    for sub in ("map", "docs"):
        d = config.project_dir(project_id) / sub
        if d.exists():
            for f in d.glob("*"):
                try:
                    f.unlink()
                except Exception as exc:
                    print(f"[terminate] could not delete {f}: {exc}", flush=True)
    # 3) clear per-file hashes (forces a full re-embed on the next learn). KEEP the
    # path selection: a Terminate wipes LEARNED DATA but should leave the project
    # ready to re-learn in one click — clearing the repo paths here would make
    # "Start / Re-learn" fail with "add a path first" (use Delete to drop a project).
    db.clear_file_index(project_id)
    # 3b) drop the canonical Asset model (assets cascade to revisions/segments/
    # evidence) and its immutable content blobs. Terminate is a full wipe of
    # LEARNED data back to init, so Asset history goes too — unlike an ordinary
    # source-file removal, which only marks an Asset removed and keeps its history.
    db.clear_workspace_assets(project_id)
    content_store.clear_workspace(project_id)
    # cases: keep but flag stale, unless clearing
    if clear_cases:
        cases.clear_cases(project_id)
    else:
        cases.mark_all_stale(project_id)
    # 4) state = init
    db.set_project_state(project_id, "init")
    config.ensure_project_dirs(project_id)
    return db.get_project(project_id)


def _cleanup_deleted(project_id: str) -> None:
    """Background pipeline that frees a deleted project's storage: wait (bounded) for
    any running job to stop — never wipe under a live ingest — then drop the vector
    collections, delete the data dir, and finally drop the entity row (+ jobs +
    file_index + ask history + machine-local path config). Idempotent; also used for
    restart recovery. The project is already a 'deleting' tombstone (hidden from the
    UI, un-revivable), so this races nothing the user can see.

    Interruptible + resumable: the collection drop drains in short batches
    (never one giant GIL-holding delete_collection — that froze the whole app
    AND blocked Ctrl+C for its duration); on shutdown or a batch failure we
    return with the tombstone kept, and the next start re-spawns this cleanup
    to finish the remainder. The entity row is removed only once storage is
    actually gone, so nothing is ever leaked silently."""
    import shutil
    try:
        _wait_jobs_stopped(project_id, timeout=30.0)
        if _shutdown.is_set():
            return                     # still 'deleting' -> resumed on next start
        # Every collection the project owns, from ONE list — so the delete path
        # can never forget a collection kind the terminate path knows about.
        for name in vectorstore.project_collection_names(project_id):
            if not vectorstore.drop_collection(name, cancel=_shutdown.is_set):
                return                 # interrupted/failed -> retry on next start
        try:
            d = config.project_dir(project_id)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                if d.exists():
                    print(f"[delete] data dir not fully removed for {project_id}: {d}",
                          flush=True)
        except Exception as exc:
            print(f"[delete] data dir removal failed for {project_id}: {exc}", flush=True)
        db.delete_project(project_id)          # row finally disappears
        print(f"[delete] storage reclaimed for {project_id}", flush=True)
    finally:
        with _deleting_lock:
            _deleting.discard(project_id)


def _spawn_cleanup(project_id: str) -> bool:
    """Start the background cleanup for a project unless one is already running."""
    with _deleting_lock:
        if project_id in _deleting:
            return False
        _deleting.add(project_id)
    threading.Thread(target=_cleanup_deleted, args=(project_id,),
                     name=f"reclaim-{project_id[:8]}", daemon=True).start()
    return True


def request_delete(project_id: str) -> Dict[str, Any]:
    """ASYNC delete (the HTTP path). Tombstones the project so it vanishes from the
    listing AT ONCE ('deleting' is one-way — a job handler unwinding from the
    terminate can't revive it), signals its jobs to stop WITHOUT waiting, and runs
    the heavy storage cleanup on a background thread. Returns immediately so the UI
    is snappy and never freezes; a refresh will not show the project again."""
    db.set_project_state(project_id, "deleting")
    _signal_stop_jobs(project_id)
    _spawn_cleanup(project_id)
    return {"deleting": project_id}


def delete_project(project_id: str) -> Dict[str, Any]:
    """Synchronous full removal (blocks until storage + entity are gone). Kept for
    inline callers/tests; the HTTP path uses request_delete."""
    db.set_project_state(project_id, "deleting")
    _signal_stop_jobs(project_id)
    _cleanup_deleted(project_id)
    return {"deleted": project_id}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def _project_path_specs(project_id: str, only_path: Optional[str]) -> List[Dict[str, Any]]:
    specs = db.get_project_paths(project_id)
    if only_path:
        # only_path is stored relative (see enqueue_ingest); match spec paths
        # relatively too. relativize() is idempotent, so an absolute input works.
        root = machine.project_root(project_id)
        op = machine.relativize(walker.norm(only_path), root)
        match = [s for s in specs if machine.relativize(walker.norm(s["path"]), root) == op]
        if match:
            return match
        return [{"path": machine.absolutize(only_path, root), "exclude": []}]
    return specs


# ---------------------------------------------------------------------------
# Asset model: local Git provenance + the per-file commit into the canonical
# Asset/Revision/Segment/Evidence store (see openmind/segmentation.py and
# db.commit_revision). All local, deterministic, model-free.
# ---------------------------------------------------------------------------
def _find_git_root(start_dir: str) -> str:
    """Nearest ancestor of *start_dir* that contains a ``.git`` entry, or ''."""
    cur = Path(start_dir)
    for _ in range(64):                     # bounded: never climb forever
        if (cur / ".git").exists():
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return ""


def _read_git_head(git_root: str) -> str:
    """Resolve a repo's current HEAD commit SHA from the filesystem only — no
    network, no ``git`` subprocess. Handles a ``.git`` directory, a ``.git``
    file (worktree/submodule), a symbolic ``ref:`` HEAD, a detached SHA, and the
    ``packed-refs`` fallback. Any failure returns '' rather than raising."""
    try:
        dot_git = Path(git_root) / ".git"
        if dot_git.is_file():               # worktree/submodule: 'gitdir: <path>'
            content = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if content.startswith("gitdir:"):
                gd = content.split(":", 1)[1].strip()
                dot_git = Path(gd) if os.path.isabs(gd) else (Path(git_root) / gd)
        head = (dot_git / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_file = dot_git / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8", errors="ignore").strip()
            packed = dot_git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8",
                                             errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "^")):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == ref:
                        return parts[0].strip()
            return ""
        return head if head else ""          # detached HEAD (a raw SHA)
    except Exception:
        return ""


def _git_commit_for(file_abs: str, dir_cache: Dict[str, str],
                    sha_cache: Dict[str, str]) -> str:
    """The HEAD SHA of the repo containing *file_abs*, cached per repository for
    one ingestion. Records only the real HEAD — a dirty working tree is never
    turned into an invented SHA (dirtiness is not asserted here)."""
    d = os.path.dirname(file_abs)
    root = dir_cache.get(d)
    if root is None:
        root = _find_git_root(d)
        dir_cache[d] = root
    if not root:
        return ""
    sha = sha_cache.get(root)
    if sha is None:
        sha = _read_git_head(root)
        sha_cache[root] = sha
    return sha


def _commit_file_asset(project_id: str, rel_key: str, text: str,
                       source_commit: str, ac: Dict[str, int]) -> Dict[str, Any]:
    """Snapshot one file's content + segments into the canonical Asset store and
    tally the additive progress counters in *ac*. Writes NO vector data — the RAG
    projection is owned by the ingest's embed batch (or reused unchanged for a
    legacy backfill), so this never re-embeds."""
    data = text.encode("utf-8", "replace")
    blob_hash = content_store.hash_bytes(data)
    if content_store.exists(project_id, blob_hash):
        ac["content_blobs_reused"] += 1
    else:
        ac["content_blobs_created"] += 1
    content_store.put(project_id, data)                     # immutable snapshot
    segments = segmentation.segment_source(rel_key, text)
    title = rel_key.replace("\\", "/").rsplit("/", 1)[-1]
    res = db.commit_revision(
        project_id, rel_key, asset_type=AssetType.classify(rel_key), title=title,
        source_path=rel_key, content_hash=blob_hash, content_size=len(data),
        content_blob_hash=blob_hash, segments=segments, source_commit=source_commit,
        revision_metadata=({"git_head": source_commit} if source_commit else {}))
    if res["asset_created"]:
        ac["assets_created"] += 1
    elif res["reactivated"]:
        ac["assets_reactivated"] += 1
    else:
        ac["assets_reused"] += 1
    if res["revision_created"]:
        ac["revisions_created"] += 1
        ac["segments_created"] += res["segments_created"]
        ac["evidence_created"] += res["evidence_created"]
    else:
        ac["revisions_reused"] += 1
    return res


_ASSET_COUNTER_KEYS = (
    "assets_created", "assets_reused", "assets_reactivated", "assets_removed",
    "revisions_created", "revisions_reused", "segments_created",
    "evidence_created", "content_blobs_created", "content_blobs_reused",
)


def _run_ingest(job_id: str) -> None:
    job = db.get_job(job_id)
    project_id = job["project_id"]
    db.set_project_state(project_id, "learning")
    # A FILTERED ingest (a single file or subtree, e.g. `asset add`) must not
    # rebuild the whole-project artifacts (glossary/structure/templates/facets)
    # from a partial file set, and must not prune files outside the filter.
    filtered = bool(job.get("path"))
    specs = _project_path_specs(project_id, job.get("path"))
    if not specs:
        _log(job_id, "[ingest] no paths to ingest.")
        db.update_job(job_id, status="failed", error="no ingest paths", finished_at=db.now())
        db.set_project_state(project_id, "init")
        return

    # ---- step 1: scan (honors selection / gitignore / built-ins; Invariant 3) ----
    db.update_job(job_id, step="scanning")
    current_files: List[str] = []
    file_meta: Dict[str, Dict[str, str]] = {}
    project_root_abs = machine.project_root(project_id)
    for spec in specs:
        root = walker.norm(spec["path"])
        # A single-file spec (a filtered `asset add`) is not a directory tree, so
        # os.walk would yield nothing; feed the one file through directly, still
        # honouring the indexable-extension / size / binary gates. Repo/service
        # attribution anchors to the project root, not the lone file.
        if os.path.isfile(root):
            ext = os.path.splitext(root)[1].lower()
            walk_root = project_root_abs or os.path.dirname(root)
            files_iter = ([root] if (ext in config.INDEX_EXTENSIONS
                                     and ext not in config.BINARY_EXTENSIONS)
                          else [])
        else:
            walk_root = root
            files_iter = walker.iter_files(root, spec.get("exclude", []))
        for f in files_iter:
            if f in file_meta:
                # overlapping path selections (e.g. a root and its subfolder):
                # index each file ONCE — duplicates in one embed batch would
                # collide on identical chunk ids. First selection wins.
                continue
            repo = walker.find_repo_root(f, walk_root)
            svc = walker.service_name(repo)
            current_files.append(f)
            file_meta[f] = {"repo": repo, "service": svc}
            # Honor pause/terminate WHILE walking a large tree — not only once the
            # whole scan finishes — so Terminate stops the worker promptly.
            if len(current_files) % 64 == 0:
                _checkpoint(job_id)
    # Single machine-local anchor. Every learned artifact below stores paths
    # RELATIVE to this root, so the data is portable + trace-free; files are
    # still READ from disk by their absolute path (cache_text/cache_hash keys).
    root = machine.project_root(project_id)
    def rel_of(f: str) -> str:
        return machine.relativize(f, root)
    current_set = {rel_of(f) for f in current_files}
    _progress(job_id, files_total=len(current_files), files_done=0)
    _log(job_id, f"[scan] {len(current_files)} indexable files selected.")
    _log(job_id, f"[resources] {resources.status_line()}")
    _checkpoint(job_id)

    # ---- step 2: read + content-hash every file (the change-sync key) ----
    # The per-file content hash is the SHARED key reused by both the deterministic
    # glossary (below) and the incremental RAG index (step 4): a file whose hash is
    # unchanged is never re-scanned or re-embedded.
    db.update_job(job_id, step="analyzing")
    cache_text: Dict[str, str] = {}
    cache_hash: Dict[str, str] = {}
    for i, f in enumerate(current_files):
        text = walker.read_text(f)
        cache_text[f] = text
        cache_hash[f] = walker.hash_text(text)
        if i % 25 == 0:
            _progress(job_id, analyzed=i + 1)
            _checkpoint(job_id)
            resources.guard_or_abort(lambda m: _log(job_id, m))  # keep RAM headroom

    # The glossary, structure map, template selection and facet captures are
    # WHOLE-PROJECT artifacts, reconciled against the full walked file set. A
    # filtered ingest (a single file / subtree — e.g. `asset add`) sees only a
    # slice of that set, so rebuilding them here would drop every term, symbol
    # and fact that lives outside the slice. Skip them for a filtered run; a full
    # ingest refreshes them. The Asset model + RAG projection below still update
    # for exactly the filtered file(s).
    if not filtered:
        # ---- template auto-selection (recorded for later consumers, not yet applied) ----
        # Deterministic stack detection scores the available template profiles; the
        # winner (if any) is RECORDED in project meta as template_auto together with
        # its score. A user override (meta["template"]) always wins at resolve time
        # (openmind.templates.resolve_for_project). Guarded end to end: template
        # selection must never be able to break a learn.
        try:
            from . import detect as detectmod, templates as templatesmod
            detection = detectmod.detect(
                [(rel_of(f), cache_text[f]) for f in current_files])
            sel = templatesmod.auto_select(detection, current_set)
            new_auto = (sel or {}).get("name")
            p = db.get_project(project_id) or {}
            meta = dict(p.get("meta") or {})
            if (meta.get("template_auto") != new_auto
                    or meta.get("template_auto_score") != (sel or {}).get("score")):
                meta["template_auto"] = new_auto
                meta["template_auto_score"] = (sel or {}).get("score")
                db.update_project_meta(project_id, meta)
            if new_auto:
                _log(job_id, f"[templates] auto-selected profile '{new_auto}' "
                             f"(score {sel['score']}); a project override wins if set.")
        except Exception as exc:
            _log(job_id, f"[templates] auto-selection skipped: {exc}")

        # ---- template facts (roles + facet captures for the RESOLVED profile) ----
        # Consumes the selection above (override > auto) and persists the facts map
        # like the other learn artifacts: deterministic, file:line evidence. A learn
        # that resolves NO template removes any stale facts file so readers never
        # serve facts from a profile that is no longer selected. Guarded the same
        # way: a facet error logs and skips, never fails the learn.
        try:
            from . import facets as facetsmod, templates as templatesmod
            tpl = templatesmod.resolve_for_project(db.get_project(project_id))
            if tpl and (tpl.roles or tpl.facets):
                fdoc = facetsmod.build_facts(
                    [(rel_of(f), cache_text[f], cache_hash[f]) for f in current_files], tpl)
                mapio.save_facts(project_id, fdoc)
                _log(job_id, f"[templates] '{tpl.name}' facts: "
                             f"{fdoc['stats']['files_classified']} file(s) classified, "
                             f"{fdoc['stats']['fact_count']} fact(s) captured.")
            else:
                mapio.delete_facts(project_id)
        except Exception as exc:
            _log(job_id, f"[templates] facet build skipped: {exc}")

        # ---- step 3: deterministic glossary (term/acronym -> VERBATIM definition) ----
        # The primary build-time artifact: term definitions are extracted ONCE here
        # (pure pattern-matching, no LLM, verbatim from authoritative sources) and
        # thereafter QUERIED (never re-parsed). Incremental via the same per-file
        # content hash — an unchanged definition source is carried over, not re-scanned.
        db.update_job(job_id, step="glossary")
        try:
            gfiles = [(rel_of(f), cache_text[f], cache_hash[f]) for f in current_files]
            # cancel-aware: a Terminate landing mid-build stops the pass promptly
            # rather than scanning the whole corpus first. Skip persisting a partial
            # artifact in that case — the wipe removes map/* anyway.
            gdoc = glossary.build_glossary(
                gfiles, prior=mapio.load_glossary(project_id),
                cancel=lambda: _control(job_id) == "terminate")
            if _control(job_id) != "terminate":
                mapio.save_glossary(project_id, gdoc)
                _log(job_id, f"[glossary] {gdoc['stats']['term_count']} term(s) from "
                             f"{gdoc['stats']['source_count']} source(s) "
                             f"({gdoc['stats']['sources_reused']} reused).")
        except Exception as exc:
            _log(job_id, f"[glossary] WARNING: build failed ({exc}) — the glossary "
                         f"was NOT updated for this run; prior artifact (if any) kept.")
        _checkpoint(job_id)   # raises if a pause/terminate arrived during the build

        # ---- step 3b: deterministic STRUCTURE map (modules / defs / dependency /
        # call / entry-point-flow graphs) — the basis of the Graphs view. Same
        # incremental, hash-keyed, never-fabricated contract as the glossary. ----
        db.update_job(job_id, step="structure")
        try:
            sfiles = [(rel_of(f), cache_text[f], cache_hash[f]) for f in current_files]
            # paths are already relative -> store an empty root (no absolute trace)
            sdoc = structure.build_structure(sfiles, root="",
                                              prior=mapio.load_structure(project_id))
            if _control(job_id) != "terminate":
                mapio.save_structure(project_id, sdoc)
                st = sdoc.get("stats", {})
                _log(job_id, f"[structure] {st.get('definition_count', 0)} defs · "
                             f"{st.get('module_count', 0)} modules · "
                             f"{st.get('call_edges', 0)} call edges · "
                             f"{st.get('entry_point_count', 0)} entry points "
                             f"({st.get('sources_reused', 0)} reused).")
        except Exception as exc:
            _log(job_id, f"[structure] WARNING: build failed ({exc}) — the structure "
                         f"map was NOT updated for this run; prior artifact (if any) kept.")
        _checkpoint(job_id)
    else:
        _log(job_id, "[filtered] single-file/subtree ingest: glossary, structure "
                     "and template artifacts are left unchanged (a full ingest "
                     "rebuilds them).")

    # ---- step 4: RAG indexing (incremental, idempotent; Invariant 12) ----
    # Embeddings are BATCHED ACROSS files (BATCH_CHUNKS at a time), not one embed
    # call per file — that is what keeps a GPU / throughput backend fully utilised
    # instead of starving it with ~10-chunk calls. A file is recorded in the
    # file_index only AFTER its chunks are embedded + upserted, so a crash/pause
    # mid-batch just re-indexes those few files on resume (stable ids -> idempotent).
    db.update_job(job_id, step="indexing")
    # Smart embedding device: bulk-embed on the GPU only when the card is FREE. If
    # the local LLM is resident on the GPU, embed on CPU so DirectML can't saturate
    # VRAM and crash the display driver (a whole-machine freeze). Decided once here,
    # at the embed step (the only GPU-heavy phase). Explicit OPENMIND_EMBED_DEVICE
    # overrides this and is respected as-is.
    store = vectorstore.get_code_store(project_id)
    index = db.get_file_index(project_id)
    # Canonical Asset model: load the whole workspace's asset index ONCE (like the
    # file index) so the per-file loop can decide, without a query per file, whether
    # an Asset already exists and whether its current revision already matches — the
    # basis of the unchanged-legacy backfill that must NOT re-embed.
    asset_index = db.list_asset_index(project_id)
    ac: Dict[str, int] = {k: 0 for k in _ASSET_COUNTER_KEYS}
    _git_dir_cache: Dict[str, str] = {}     # file dir  -> git root (per ingestion)
    _git_sha_cache: Dict[str, str] = {}     # git root  -> HEAD sha (per ingestion)
    # only the changed/new files actually embed (incremental) — so don't stop the
    # model to free the GPU when an incremental re-ingest has nothing to embed.
    _needs_embed = any(index.get(rel_of(f), {}).get("file_hash") != cache_hash[f]
                       for f in current_files)
    # Auto-free the GPU: stop our idle MANAGED model so embedding can use the GPU
    # (ingest never uses the LLM); the worker loop restarts it after this job.
    _freed = _free_gpu_for_embed(job_id) if _needs_embed else False
    _mstat = model_server.status().get("status")
    _gpu_free = _freed or _mstat in ("not_started", "stopped", "error", None)
    _dev = embeddings.prepare_for_bulk(_gpu_free)
    _log(job_id, f"[embeddings] {_dev} (model server: {_mstat or 'unknown'})")
    if _needs_embed and not _gpu_free and "CPU" in _dev:
        _log(job_id, "[embeddings] an ATTACHED model server holds the GPU, so embedding "
                     "runs on CPU (slower, no VRAM crash). Stop it before learning for "
                     "~5x faster GPU embedding (OPENMIND_INGEST_FREE_GPU only frees our "
                     "own managed server).")
    done = skipped = indexed = changed = 0
    chunk_total = sum(len(v.get("chunk_ids", [])) for v in index.values())
    # Adaptive batch: large on GPU (throughput); small on CPU so pause/terminate/
    # delete are honored within ~one short embed instead of a long one.
    BATCH_CHUNKS = 256 if _gpu_free else 64
    pending: List[Dict[str, Any]] = []
    pending_chunks = 0

    def _flush_batch() -> None:
        nonlocal pending, pending_chunks, chunk_total
        if not pending:
            return
        # Vector projection first (idempotent, stable ids), then per file the
        # compatibility file-index row and the canonical Asset revision. If the DB
        # write fails after the upsert, the next ingest safely repeats the stable
        # upsert and reconciles the Asset, and no partial revision becomes current
        # (commit_revision is a single transaction).
        rag.embed_and_upsert(store, [c for rec in pending for c in rec["chunks"]])
        for rec in pending:
            ids = [c["id"] for c in rec["chunks"]]
            # Commit the canonical Asset revision BEFORE the compatibility
            # file_index row. file_index is the "unchanged" gate the skipped
            # branch trusts, so it must be written LAST: if the process dies
            # between these two writes, file_index still shows the OLD hash, the
            # file is re-indexed next run (changed branch), and commit_revision
            # simply reuses the already-committed revision. Writing file_index
            # first would instead strand a stale Asset that the skip branch would
            # trust forever.
            _commit_file_asset(project_id, rec["f"], rec["text"], rec["commit"], ac)
            db.upsert_file_index(project_id, rec["f"], rec["h"], ids,
                                 rec["service"], rec["topics"])
            chunk_total += len(ids)
        pending = []
        pending_chunks = 0

    for i, f in enumerate(current_files):
        _checkpoint(job_id)  # pause/terminate only at a file boundary (Invariant 4)
        if i % 8 == 0:
            resources.guard_or_abort(lambda m: _log(job_id, m))  # keep RAM headroom
        r = rel_of(f)        # portable identity key (file is read from disk via f)
        h = cache_hash[f]
        prior = index.get(r)
        meta = file_meta[f]
        repo_rel = machine.relativize(meta["repo"], root)
        topics: List[str] = []     # chunk-header topic tags (architecture-agnostic: none)
        if prior and prior.get("file_hash") == h:
            skipped += 1
            # Legacy backfill: this file is unchanged in the RAG index, but a
            # Phase 1 workspace has no Asset for it yet. Snapshot it into the
            # canonical store WITHOUT re-embedding (its Chroma chunks are reused).
            # A file whose Asset is already in sync just tallies reuse.
            ai = asset_index.get(r)
            if ai and ai.get("content_hash") and ai.get("state") == "active":
                ac["assets_reused"] += 1
                ac["revisions_reused"] += 1
            else:
                _commit_file_asset(
                    project_id, r, cache_text[f],
                    _git_commit_for(f, _git_dir_cache, _git_sha_cache), ac)
        else:
            if prior and prior.get("chunk_ids"):
                rag.delete_file_chunks(store, r, prior["chunk_ids"])
                chunk_total -= len(prior["chunk_ids"])
                changed += 1
            chunks = rag.chunk_file(project_id, r, cache_text[f],
                                    meta["service"], repo_rel, topics, h)
            # Carry the file text + resolved Git HEAD so _flush_batch can snapshot
            # the blob and commit the Asset revision transactionally, once the
            # chunks for this batch are embedded.
            pending.append({"f": r, "h": h, "service": meta["service"],
                            "topics": topics, "chunks": chunks,
                            "text": cache_text[f],
                            "commit": _git_commit_for(f, _git_dir_cache,
                                                      _git_sha_cache)})
            pending_chunks += len(chunks)
            indexed += 1
            if pending_chunks >= BATCH_CHUNKS:
                _checkpoint(job_id)   # honor pause/terminate BEFORE a blocking embed
                _flush_batch()
        done += 1
        if i % 5 == 0 or done == len(current_files):
            _progress(job_id, files_done=done, files_skipped=skipped,
                      files_indexed=indexed, files_changed=changed,
                      chunks=chunk_total + pending_chunks, **ac)
    _checkpoint(job_id)   # honor a late pause/terminate before the final embed
    _flush_batch()   # embed + store the final partial batch

    # ---- step 5: prune removed / now-excluded files ----
    # A FILTERED ingest saw only part of the workspace, so it must prune ONLY
    # within the filter root(s) — otherwise a single-file `asset add` would treat
    # every other indexed file as "gone". A full ingest prunes the whole index.
    if filtered:
        scope_roots = [rel_of(walker.norm(s["path"])) for s in specs]

        def _in_scope(fpath: str) -> bool:
            for rt in scope_roots:
                if not rt or fpath == rt or fpath.startswith(rt.rstrip("/") + "/"):
                    return True
            return False
    else:
        def _in_scope(_fpath: str) -> bool:
            return True

    removed = 0
    for fpath, rec in list(index.items()):
        if _in_scope(fpath) and fpath not in current_set:
            # Remove the active retrieval projection + the compatibility index
            # row, but PRESERVE the Asset's history: mark it removed, keeping every
            # revision, segment, evidence and content blob (source deletion is not
            # history erasure — that is what terminate/delete are for).
            rag.delete_file_chunks(store, fpath, rec.get("chunk_ids"))
            db.delete_file_index_entry(project_id, fpath)
            if db.mark_asset_removed(project_id, fpath):
                ac["assets_removed"] += 1
            removed += 1
    _progress(job_id, files_removed=removed, chunks=store.count(), **ac)
    _log(job_id, f"[index] indexed={indexed} changed={changed} skipped={skipped} "
                 f"removed={removed} chunks={store.count()}")
    _log(job_id, f"[assets] +{ac['assets_created']} new "
                 f"~{ac['assets_reused']} reused ^{ac['assets_reactivated']} reactivated "
                 f"-{ac['assets_removed']} removed | revisions +{ac['revisions_created']} "
                 f"={ac['revisions_reused']} reused | segments {ac['segments_created']} "
                 f"evidence {ac['evidence_created']} | blobs +{ac['content_blobs_created']} "
                 f"={ac['content_blobs_reused']} reused")

    # ---- step 6: documents (v2 Phase 3) ----
    # A separate discovery policy and a separate pipeline: document extensions
    # are deliberately NOT in INDEX_EXTENSIONS, so a .pdf can never reach
    # walker.read_text, and one file can never become both a code Asset and a
    # document Asset. A filtered code ingest must not prune documents, so
    # document pruning only happens on a full run.
    _run_document_sweep(job_id, project_id, specs, root, filtered, ac)

    # ---- done ----
    _checkpoint(job_id)  # if terminate raced in after the last file, abort before finalize
    # New current revisions may have superseded the sources of semantic
    # candidates; reconcile BEFORE the job flips to done. Placement matters:
    # doing this between update_job(done) and enqueue_gendocs would widen the
    # zero-active-jobs window that "wait until idle" pollers observe, and a
    # poll landing inside it would conclude the pipeline finished before the
    # docs job was even enqueued.
    reconcile_semantic_staleness(project_id)
    db.update_job(job_id, status="done", step="done", finished_at=db.now())
    db.set_project_state(project_id, "ready")
    _log(job_id, "[done] ingest complete; triggering docs sync + wiki enrichment.")
    enqueue_gendocs(project_id)
    enqueue_enrich(project_id)   # timing (b): index is READY first; enrich follows


# ---------------------------------------------------------------------------
# Document ingestion (OpenMind v2 Phase 3)
#
# One document at a time, from an ALREADY-STAGED immutable blob. The job row
# carries only a blob hash and a filename, so a restart can resume it without an
# absolute machine path ever having been persisted — and a re-run is safe,
# because the blob is content-addressed and the vector chunk ids are derived
# from the content.
# ---------------------------------------------------------------------------
def _document_step(job_id: str, step: str) -> None:
    db.update_job(job_id, step=step)


def _run_document_sweep(job_id: str, project_id: str,
                        specs: List[Dict[str, Any]], root: str,
                        filtered: bool, ac: Dict[str, int]) -> None:
    """Discover, ingest and prune the workspace's DOCUMENT assets.

    Incremental in exactly the way the code pipeline is: a document whose bytes
    are unchanged produces no revision and no embedding, because
    ``commit_document`` compares the content hash before doing anything.

    Guarded end to end. A malformed document must fail its own ingest and be
    recorded honestly, never take down a whole learn that has already indexed the
    code correctly.
    """
    db.update_job(job_id, step="documents")
    discovered: Dict[str, str] = {}          # logical key -> absolute path
    for spec in specs:
        spec_root = walker.norm(spec["path"])
        if os.path.isfile(spec_root):
            ext = os.path.splitext(spec_root)[1].lower()
            files = ([spec_root] if ext in config.DOCUMENT_DISCOVERY_EXTENSIONS
                     else [])
        else:
            files = walker.iter_document_files(spec_root, spec.get("exclude", []))
        for path in files:
            discovered.setdefault(machine.relativize(path, root), path)

    if not discovered and filtered:
        return

    index = db.list_document_index(project_id)
    imported = unchanged = failed = removed = 0
    store = vectorstore.get_documents_store(project_id) if discovered else None

    for logical_key, abspath in sorted(discovered.items()):
        _checkpoint(job_id)
        try:
            with open(abspath, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            failed += 1
            _log(job_id, f"[documents] could not read {logical_key}: {exc}")
            continue
        try:
            report = ingest_one_document(
                project_id, job_id, data=data, logical_key=logical_key,
                filename=os.path.basename(abspath), source_kind="file",
                source_commit=_git_commit_for(abspath, {}, {}), store=store)
        except Exception as exc:
            # One bad document fails ITSELF. The worker survives, every other
            # document still lands, and the failure is on the job log.
            failed += 1
            _log(job_id, f"[documents] {logical_key} failed to ingest: "
                         f"{type(exc).__name__}: {exc}")
            continue
        if report.get("revision_created"):
            imported += 1
        elif report.get("state") == "unsupported":
            failed += 1
        else:
            unchanged += 1

    # Pruning is FULL-INGEST ONLY. A filtered code ingest saw a slice of the
    # workspace and must never conclude that every document outside it is gone.
    if not filtered:
        for asset_id, record in index.items():
            asset = db.get_asset(project_id, asset_id)
            if not asset or asset.get("state") != "active":
                continue
            if asset.get("source_kind") == "attachment":
                # An attachment's origin path is deliberately not tracked, so it
                # is never "missing from the workspace". Its snapshot IS the
                # canonical source.
                continue
            if asset["logical_key"] in discovered:
                continue
            chunk_ids = record.get("chunk_ids") or []
            if chunk_ids and store is not None:
                store.delete(ids=chunk_ids)
            db.delete_document_index(project_id, asset_id)
            if db.mark_asset_removed(project_id, asset["logical_key"]):
                ac["assets_removed"] += 1
            removed += 1

    if discovered or removed:
        _log(job_id, f"[documents] discovered={len(discovered)} "
                     f"imported={imported} unchanged={unchanged} "
                     f"unsupported/failed={failed} removed={removed}")
    _progress(job_id, documents_total=len(discovered),
              documents_imported=imported, documents_unchanged=unchanged,
              documents_failed=failed, documents_removed=removed)


def ingest_one_document(project_id: str, job_id: str, *, data: bytes,
                        logical_key: str, filename: str, source_kind: str,
                        version_label: str = "", source_commit: str = "",
                        parser_options: Optional[Dict[str, Any]] = None,
                        store: Any = None) -> Dict[str, Any]:
    """Parse and commit ONE document. Shared by the attach job and the workspace
    document sweep, so both paths produce identical Assets and projections.

    Returns an import report. A parse that produced no usable content
    (``unsupported`` / ``failed`` / ``needs-ocr`` / ``encrypted``) does NOT
    become a content revision — the Asset is registered honestly as
    ``unsupported`` instead, so "we could not read this" never looks like "this
    document is empty".
    """
    from . import documents as documents_pkg
    from .documents import pipeline

    _document_step(job_id, "probing-format")
    parsed = documents_pkg.parse_bytes(
        data, logical_key=logical_key, filename=filename,
        workspace_id=project_id, parser_options=parser_options or {})

    report: Dict[str, Any] = {
        "logical_key": logical_key,
        "parser": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "parse_status": parsed.status,
        "title": parsed.title,
        "blocks": len(parsed.blocks),
        "warnings": [w.as_dict() for w in parsed.warnings],
        "unsupported_content": [u.as_dict() for u in parsed.unsupported_content],
        "coverage": dict(parsed.coverage),
    }

    if not parsed.usable:
        asset = db.upsert_asset(
            project_id, logical_key,
            asset_type=AssetType.classify(logical_key) or AssetType.DOCUMENT,
            title=parsed.title or filename, source_path=logical_key,
            state="unsupported", media_type=parsed.media_type,
            source_kind=source_kind,
            metadata={"parse_status": parsed.status,
                      "parse_reason": parsed.reason})
        report.update({"asset_id": asset["id"], "revision_created": False,
                       "state": "unsupported", "reason": parsed.reason,
                       "blocks_indexed": 0})
        _log(job_id, f"[document] {logical_key}: {parsed.status} "
                     f"({parsed.reason or 'not parseable'}); registered as an "
                     f"unsupported asset, NOT parsed.")
        return report

    _document_step(job_id, "storing-segments")
    content_store.put(project_id, data)          # immutable revision snapshot
    blob_hash = content_store.hash_bytes(data)

    _document_step(job_id, "embedding-documents")
    result = pipeline.commit_document(
        project_id, logical_key, parsed=parsed, content=data,
        blob_hash=blob_hash, title=parsed.title or filename,
        source_kind=source_kind, source_path=logical_key,
        version_label=version_label, source_commit=source_commit,
        store=store if store is not None
        else vectorstore.get_documents_store(project_id))
    report.update({
        "asset_id": result["asset_id"],
        "revision_id": (result.get("revision") or {}).get("id", ""),
        "revision_sequence": (result.get("revision") or {}).get("sequence"),
        "revision_created": result.get("revision_created", False),
        "asset_created": result.get("asset_created", False),
        "reactivated": result.get("reactivated", False),
        "segments_created": result.get("segments_created", 0),
        "evidence_created": result.get("evidence_created", 0),
        "blocks_indexed": result.get("blocks_indexed", 0),
        "replaced_chunks": len(result.get("replaced_chunk_ids") or []),
        "version_label": result.get("version_label", ""),
        "version_label_source": result.get("version_label_source", ""),
        "state": "active",
    })
    return report


def _run_document_ingest(job_id: str) -> None:
    """Import one staged document, then look for deterministic candidates."""
    job = db.get_job(job_id)
    project_id = job["project_id"]
    payload = db.get_job_payload(job_id)
    blob_hash = str(payload.get("staged_blob_hash") or "")
    filename = str(payload.get("original_filename") or "document")
    logical_key = str(payload.get("requested_logical_key") or "")

    if not blob_hash or not logical_key:
        db.update_job(job_id, status="failed", finished_at=db.now(),
                      error="document_ingest payload is incomplete "
                            "(staged_blob_hash and requested_logical_key are "
                            "required)")
        return

    _document_step(job_id, "reading-snapshot")
    try:
        data = content_store.get(project_id, blob_hash)
    except Exception as exc:
        db.update_job(job_id, status="failed", finished_at=db.now(),
                      error=f"staged document blob unavailable: {exc}")
        return

    _checkpoint(job_id)
    _document_step(job_id, "parsing")
    report = ingest_one_document(
        project_id, job_id, data=data, logical_key=logical_key,
        filename=filename,
        source_kind=str(payload.get("source_kind") or "attachment"),
        version_label=str(payload.get("version_label") or ""),
        parser_options=payload.get("parser_options") or {})

    _checkpoint(job_id)
    report["related_candidates"] = _document_candidates(
        job_id, project_id, report.get("asset_id", ""))

    _document_step(job_id, "committing")
    _log(job_id, f"[document] {logical_key}: {report['parse_status']} via "
                 f"{report['parser']} — {report.get('segments_created', 0)} "
                 f"segment(s), {report.get('blocks_indexed', 0)} indexed, "
                 f"{len(report.get('warnings') or [])} warning(s).")
    progress = dict(job.get("progress") or {})
    progress["import_report"] = report
    # A new document revision stales semantic candidates tied to its
    # predecessor; reconcile BEFORE the job flips to done (same ordering
    # rationale as the code-ingest path), so a caller that waited on the job
    # never observes a done import with staleness still pending.
    reconcile_semantic_staleness(project_id)
    db.update_job(job_id, status="done", step="done", finished_at=db.now(),
                  progress=progress)


def _document_candidates(job_id: str, project_id: str,
                         asset_id: str) -> Dict[str, Any]:
    """Deterministic candidate association for a freshly imported document.

    Guarded end to end: candidate association is a convenience, and a failure in
    it must never fail an import whose content is already committed correctly.
    """
    if not asset_id:
        return {"count": 0, "top": []}
    _document_step(job_id, "finding-related-candidates")
    try:
        from .documents import candidates as candidates_module
        found = candidates_module.find_candidates(project_id, asset_id, limit=20)
    except Exception as exc:
        _log(job_id, f"[document] candidate association skipped: {exc}")
        return {"count": 0, "top": [], "error": str(exc)}
    top = found.get("candidates", [])[:5]
    if found.get("count"):
        _log(job_id, f"[document] {found['count']} related CANDIDATE(s) found "
                     f"(observed mentions, not confirmed relations).")
    return {"count": found.get("count", 0), "total_found":
            found.get("total_found", 0), "signals": found.get("signals", {}),
            "top": top, "status": found.get("status", "candidate")}


# ---------------------------------------------------------------------------
# Wikipedia enrichment job (the deterministic guarantee; resumable + recoverable)
# ---------------------------------------------------------------------------
def _run_enrich(job_id: str) -> None:
    job = db.get_job(job_id)
    project_id = job["project_id"]
    p = db.get_project(project_id)
    if not p or not _enrich_enabled(p):
        db.update_job(job_id, status="done", step="done", finished_at=db.now())
        return
    doc = mapio.load_glossary(project_id)
    pending = glossary.unattempted_terms(doc)
    if not pending:
        _log(job_id, "[enrich] up to date — every term already attempted.")
        db.update_job(job_id, status="done", step="done", finished_at=db.now())
        return
    ctx = _enrich_context(p)
    pins = _enrich_pins(p)
    block = _enrich_block(p)
    db.update_job(job_id, step="enriching")
    _progress(job_id, enrich_total=len(pending), enrich_done=0)
    _log(job_id, f"[enrich] {len(pending)} term(s) to look up on Wikipedia "
                 f"(context: '{ctx}').")

    def _cancel() -> bool:
        return _control(job_id) in ("terminate", "pause")

    def _prog(done: int, total: int, term: str, result: Optional[str]) -> None:
        _progress(job_id, enrich_done=done, enrich_total=total)
        _log(job_id, f"[enrich] {term} -> {result}" if result
             else f"[enrich] {term} -> (no confident match; kept local)")

    res = wikienrich.enrich_project(project_id, context=ctx, pins=pins, block=block,
                                    skip_enriched=True, cancel=_cancel,
                                    on_progress=_prog)
    # honor a pause/terminate that arrived mid-run (un-attempted terms remain, so
    # resume / the startup reconciler completes them).
    _checkpoint(job_id)
    db.update_job(job_id, status="done", step="done", finished_at=db.now())
    _log(job_id, f"[enrich] done: matched={res['matched']} "
                 f"no-match={res['none']} of {res['total']}.")


# ---------------------------------------------------------------------------
# Gendocs
# ---------------------------------------------------------------------------
def _run_gendocs(job_id: str) -> None:
    job = db.get_job(job_id)
    project_id = job["project_id"]
    db.update_job(job_id, step="generating docs")

    def prog(done, total, page):
        _progress(job_id, pages_done=done, pages_total=total)
        if _control(job_id) == "terminate":
            raise _TerminateSignal()

    result = docsmod.generate(project_id, progress=prog)
    _log(job_id, f"[docs] generated {result['count']} pages.")
    db.update_job(job_id, status="done", step="done", finished_at=db.now(),
                  progress={**job["progress"], "pages": result["count"]})


# ---------------------------------------------------------------------------
# Ask (grounded, streamed answer — runs under the single-task lock; Part 2)
# ---------------------------------------------------------------------------
def _run_ask(job_id: str) -> None:
    """Build a grounded prompt + stream the local model's answer into the
    persisted exchange. Because this runs in the single worker, no two
    inferences (Ask-vs-Ask, or Ask-vs-ingest summaries) ever run concurrently
    against the constrained llama-server (--parallel 1)."""
    job = db.get_job(job_id)
    ex_id = job.get("path")
    ex = db.ask_get(ex_id) if ex_id else None
    if not ex:
        db.update_job(job_id, status="failed", error="ask exchange missing",
                      finished_at=db.now())
        return

    # server-ready gate, re-checked at the moment the lock frees (Invariant 7)
    try:
        model_server.refresh()
    except Exception:
        pass
    if not llm_client.is_ready():
        db.ask_update(ex_id, status="error",
                      payload={"error": "model server not ready"})
        db.update_job(job_id, status="failed", error="model server not ready",
                      finished_at=db.now())
        return

    # cancel can land while the job sat queued (cancel_job set the exchange
    # 'cancelled' but the worker may have already dequeued it) — abort cleanly.
    if ex.get("status") == "cancelled":
        db.update_job(job_id, status="failed", error="cancelled by user",
                      control={"requested": None}, finished_at=db.now())
        return

    def _aborted() -> bool:
        """A cancel (cancel_job) or a terminate (terminate_project) may flip the
        job to terminal / set control=terminate at any moment from another
        thread; treat either as 'stop now' (no second/clobbering inference)."""
        jn = db.get_job(job_id)
        if not jn:
            return True
        return (jn.get("control", {}).get("requested") == "terminate"
                or jn.get("status") in ("failed", "cancelled"))

    def _finish_cancelled(answer: str) -> None:
        db.ask_update(ex_id, status="cancelled", payload={"answer": answer})
        db.update_job(job_id, status="failed", error="cancelled by user",
                      control={"requested": None}, finished_at=db.now())

    pids = ex.get("pids") or []
    question = ex.get("question") or ""
    attachments = ex.get("attachments") or []
    k = int(ex.get("k", 12) or 12)
    pairs = conversation.context_pairs(ex["scope_id"], ex_id)   # multi-turn context

    try:
        built = askmod.build_grounded(pids, question, attachments, k=k, history=pairs)
    except Exception as exc:  # noqa
        db.ask_update(ex_id, status="error",
                      payload={"error": f"retrieval failed: {exc}"})
        db.update_job(job_id, status="failed", error=str(exc),
                      control={"requested": None}, finished_at=db.now())
        return

    if _aborted():                       # cancelled/terminated during retrieval
        _finish_cancelled("")
        return

    # persist the grounding (sources/meta) immediately so the UI can show it
    # while the (prefill-bound) first token is still pending.
    db.ask_update(ex_id, status="running", payload={"meta": {
        "sources": built["sources"], "raw": built["raw"], "meta": built["meta"]}})
    db.update_job(job_id, step="answering", progress={"phase": "answering", "answer": ""})

    acc = ""
    last = 0.0
    try:
        for delta in llm_client.chat_stream(built["messages"], max_tokens=1024):
            if _aborted():               # checked EVERY delta (not only on flush)
                _finish_cancelled(acc)
                return
            acc += delta
            t = time.monotonic()
            if t - last >= 0.25:          # throttle DB writes (SSE polls ~0.8s)
                db.update_job(job_id, progress={"phase": "answering", "answer": acc})
                last = t
    except Exception as exc:  # surface transport/model errors
        db.ask_update(ex_id, status="error", payload={"answer": acc, "error": str(exc)})
        db.update_job(job_id, status="failed", error=str(exc),
                      control={"requested": None}, finished_at=db.now())
        return

    # a cancel/terminate may have arrived during the final (post-loop) window, or
    # terminate_project's 20s wait may already have force-failed this job — never
    # resurrect a terminal job to 'done' (Invariant 5 wipe stays effective).
    if _aborted():
        _finish_cancelled(acc)
        return

    grounding_kinds = sorted({s.get("kind") for s in built["sources"] if s.get("kind")})
    slim_att = [{"name": a.get("name"), "kind": a.get("kind"),
                 "status": a.get("status", "")} for a in attachments]
    db.ask_update(ex_id, status="done", payload={
        "answer": acc, "attachments": slim_att,
        "grounding_used": built["meta"].get("source_count", 0) > 0,
        "grounding_kinds": grounding_kinds})
    db.update_job(job_id, status="done", step="done",
                  progress={"phase": "done", "answer": acc},
                  control={"requested": None}, finished_at=db.now())
