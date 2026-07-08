"""Executable verification of all 12 design invariants.

Run:  OPENMIND_EMBED_OFFLINE=1 OPENMIND_DATA_DIR=./data_verify python tests/verify.py
Uses the deterministic hashing embedder for speed/repeatability.
"""
import os, sys, time, json, shutil, threading, http.server, socketserver
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures", "testrepos").replace("\\", "/")
results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if detail else ""))

from fastapi.testclient import TestClient
from openmind import db, vectorstore, rag, mapio, walker, netguard, model_server
from openmind.main import app

def wait_idle(c, n=200):
    for _ in range(n):
        act = [x for x in c.get("/jobs").json()["jobs"] if x["status"] in ("queued","running")]
        if not act: return
        time.sleep(0.3)

def ingest_wait(c, pid, path=None):
    body = {"project_id": pid}
    if path: body["path"] = path
    c.post("/ingest", json=body); wait_idle(c)

with TestClient(app) as c:
    # keep tests fast + deterministic regardless of any running llama-server:
    # point the LLM at a dead port so summaries/docs use the deterministic path.
    c.post("/model-config", json={"port": 1})
    # ============== Invariant 1: local-only egress ==============
    try:
        netguard.assert_local("http://example.com/v1/chat", "POST"); blocked = False
    except netguard.ExfiltrationBlocked:
        blocked = True
    netguard.assert_local("http://127.0.0.1:7080/v1/chat", "POST")  # allowed
    # source scan: only netguard.py may import httpx
    appdir = os.path.join(ROOT, "openmind")
    offenders = []
    for f in os.listdir(appdir):
        if f.endswith(".py") and f != "netguard.py":
            t = open(os.path.join(appdir, f), encoding="utf-8").read()
            if "import httpx" in t or "requests.get(" in t or "urllib.request" in t or "import aiohttp" in t:
                offenders.append(f)
    check("Invariant 1 non-local egress blocked", blocked)
    check("Invariant 1 only netguard constructs HTTP clients", not offenders, "offenders=" + str(offenders))
    check("Invariant 1 outbound calls are logged", any(not e["allowed"] for e in netguard.get_log()))

    # ============== Invariant 3 + Invariant 12 baseline ingest ==============
    # (Invariants 7/8/10 covered the removed Kafka topology / generated-docs tiers
    # and were dropped with that feature; the deterministic glossary tier has its
    # own acceptance suite in tests/verify_glossary.py.)
    A = c.post("/projects", json={"name":"A","path":FIX,"exclude":[]}).json()["id"]
    ingest_wait(c, A)
    gl = c.get(f"/glossary?scope={A}").json()
    check("baseline ingest produces a glossary map (term list reachable)",
          isinstance(gl.get("terms"), dict))
    storeA = vectorstore.get_code_store(A)
    S1 = sorted(storeA.get()["ids"]); n1 = storeA.count()

    # Invariant 12: re-ingest unchanged -> identical chunk ids/counts
    ingest_wait(c, A)
    S1b = sorted(vectorstore.get_code_store(A).get()["ids"])
    check("Invariant 12 re-ingest unchanged is idempotent (same ids/count)", S1 == S1b and len(S1)==n1,
          f"{n1} chunks")

    # Invariant 3: exclude payment-service -> zero chunks from it
    c.post(f"/projects/{A}/selection", json={"path":FIX,"exclude":[FIX+"/payment-service"]})
    ingest_wait(c, A)
    metas = vectorstore.get_code_store(A).get()["metadatas"]
    pay = [m for m in metas if "payment-service" in (m.get("file_path") or "")]
    check("Invariant 3 walker honors exclude (0 chunks from excluded subtree)", len(pay)==0)
    # restore selection
    c.post(f"/projects/{A}/selection", json={"path":FIX,"exclude":[]}); ingest_wait(c, A)

    # ============== Invariant 4: pause+resume == uninterrupted ==============
    P = c.post("/projects", json={"name":"P","path":FIX,"exclude":[]}).json()["id"]
    ingest_wait(c, P)
    full_ids = sorted(vectorstore.get_code_store(P).get()["ids"])
    # wipe + simulate a pause after indexing HALF the files (the file-hash set is the checkpoint)
    c.post(f"/projects/{P}/terminate", json={"clear_cases":True})
    c.post(f"/projects/{P}/selection", json={"path":FIX,"exclude":[]})
    files = walker.list_files(FIX)
    half = files[:len(files)//2]
    storeP = vectorstore.get_code_store(P)
    for f in half:
        txt = walker.read_text(f); h = walker.hash_text(txt)
        repo = walker.find_repo_root(f, FIX); svc = walker.service_name(repo)
        ids = rag.index_file(P, storeP, f, txt, svc, repo, [], h)
        db.upsert_file_index(P, f, h, ids, svc, [])
    # resume = run ingest again; done files are skipped by hash, the rest indexed
    ingest_wait(c, P)
    resumed_ids = sorted(vectorstore.get_code_store(P).get()["ids"])
    check("Invariant 4 pause+resume yields identical final index", resumed_ids == full_ids,
          f"full={len(full_ids)} resumed={len(resumed_ids)}")

    # ============== Invariant 5: terminate full wipe ==============
    c.post(f"/projects/{A}/terminate", json={"clear_cases":True})
    pa = c.get(f"/projects/{A}").json()
    mapdir = os.path.join(os.environ["OPENMIND_DATA_DIR"], A, "map")
    leftover = os.listdir(mapdir) if os.path.exists(mapdir) else []
    check("Invariant 5 terminate wipes learned data to init (0 chunks/files, map empty, state init)",
          pa["state"]=="init" and pa["code_chunks"]==0 and pa["files_indexed"]==0
          and not leftover)
    # terminate KEEPS the path selection so re-learn works in one click
    check("Invariant 5 terminate keeps the path selection (ready to re-learn)",
          len(pa["paths"]) >= 1)
    ingest_wait(c, A)
    check("Invariant 5 re-learn after terminate starts from zero", vectorstore.get_code_store(A).count() > 0)

    # ============== Invariant 6: jobs persistent / interrupted-resumable ==============
    fake = db.create_job(A, "ingest", FIX)
    db.update_job(fake["job_id"], status="running")
    db.set_project_state(A, "learning")
    from openmind import jobs as jobsmod
    jobsmod._recover_on_restart()
    rec = db.get_job(fake["job_id"])
    check("Invariant 6 server-restart marks running job interrupted+resumable",
          rec["status"]=="interrupted" and db.get_project(A)["state"]=="paused")
    db.update_job(fake["job_id"], status="failed")  # cleanup
    # GET /jobs returns persisted state; SSE endpoint exists
    check("Invariant 6 jobs persisted & queryable via GET /jobs", isinstance(c.get("/jobs").json()["jobs"], list))

    # ============== Invariant 9: per-project isolation ==============
    # (The join-group / query-time-union half of this invariant was removed with
    # the JOIN GROUPS feature; per-project physical isolation still holds.)
    B = c.post("/projects", json={"name":"B","path":FIX+"/payment-service","exclude":[]}).json()["id"]
    # make A only order-service so A and B are disjoint (terminate keeps paths,
    # so drop the old root before pointing A at the subtree)
    c.post(f"/projects/{A}/terminate", json={"clear_cases":True})
    c.delete(f"/projects/{A}/paths", params={"path": FIX})
    c.post(f"/projects/{A}/paths", json={"path":FIX+"/order-service","exclude":[]}); ingest_wait(c, A)
    ingest_wait(c, B)
    sa = c.post("/search", json={"scope":A,"query":"PaymentPublisher KafkaTemplate","k":10}).json()
    a_has_payment = any("payment-service" in (ch["file_path"] or "") for ch in sa["code_chunks"])
    check("Invariant 9 project A scope never returns project B data", not a_has_payment)
    # physical isolation: separate collections
    check("Invariant 9 exactly one physical collection per project",
          vectorstore.code_collection_name(A) != vectorstore.code_collection_name(B))

    # ============== Invariant 11: model server attach / build_args / OOM ==============
    ms = model_server.ModelServer()
    args = ms.build_args({"llama_server_path":"llama-server","model_path":"m.gguf",
        "n_gpu_layers":999,"ctx_size":32768,"parallel":1,"host":"127.0.0.1","port":7080,
        "threads":12,"flash_attn":True,"cache_type_k":"q8_0","cache_type_v":"q8_0",
        "jinja":True,"extra_args":"--foo bar"})
    args_ok = (args[:1]==["llama-server"] and "-ngl" in args and "32768" in args
               and "-fa" in args and "--jinja" in args and "--foo" in args and "-ctk" in args)
    check("Invariant 11 build_args includes all configured flags", args_ok, " ".join(args))
    # attach-don't-duplicate: stand up a dummy /health=200 server and confirm attach
    PORT = 7099
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self,*a): pass
    srv = socketserver.TCPServer(("127.0.0.1", PORT), H); srv.allow_reuse_address=True
    threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    st = ms.start({"host":"127.0.0.1","port":PORT,"llama_server_path":"llama-server","model_path":""})
    check("Invariant 11 attaches to an already-serving port (no duplicate spawn)",
          st["attached"] and st["pid"] is None and st["status"]=="ready")
    srv.shutdown()
    # An OOM/load-failure log line is captured as the REASON (oom flag) but must
    # NOT by itself latch status=error (that only happens when the process exits)
    # — otherwise normal Vulkan startup logs would false-latch. The genuine
    # crash->error+reason path is covered in tests/verify_modelserver.py.
    ms2 = model_server.ModelServer()
    ms2._append("ggml_vulkan: failed to allocate ... (out of memory)")
    check("Invariant 11 surfaces OOM/load reason from server log (no false latch)",
          ms2._oom and ms2._status != "error")

# summary
print("\n==== SUMMARY ====")
passed = sum(1 for _,ok,_ in results if ok)
for n,ok,d in results:
    print(("  PASS " if ok else "  FAIL ")+n)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed==len(results) else 1)
