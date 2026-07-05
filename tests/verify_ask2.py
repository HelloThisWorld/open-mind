"""Acceptance for the three Ask upgrades:

  Part 1 — persist the conversation (per-scope, last-N, survives reload/restart,
           isolation, clear).
  Part 2 — single-flight Ask under the global single-task worker (no concurrent
           inference, a second Ask pends, a pending Ask can be cancelled).
  Part 3 — save-to-cases (separate embedded cases layer, not the code index,
           provenance + hashes + staleness, no silent double-save, surfaces in
           future asks, listed under Cases).
"""
import os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from openmind.main import app
from openmind import ask, cases, conversation, db, llm_client, vectorstore, walker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(ROOT, "fixtures", "testrepos").replace("\\", "/")
results = []
def check(n, c, d=""):
    results.append((n, bool(c))); print(("PASS " if c else "FAIL ") + n + (("  -- " + d) if d else ""))

def widle(c, n=400):
    for _ in range(n):
        if not [x for x in c.get("/jobs").json()["jobs"] if x["status"] in ("queued", "running")]:
            return
        time.sleep(0.3)

def ask_done(c, scope, q, atts=None):
    """Enqueue an ask, wait for the worker, return the finished exchange."""
    j = c.post("/ask", json={"scope": scope, "question": q,
                             "attachments": atts or []}).json()
    widle(c)
    return c.get("/ask/exchange/" + j["exchange_id"]).json(), j

# ---- concurrency-detecting slow stream (Part 2) ----------------------------
_cc = {"now": 0, "max": 0, "calls": 0}
_lk = threading.Lock()
def slow_stream(messages, **k):
    with _lk:
        _cc["now"] += 1; _cc["calls"] += 1; _cc["max"] = max(_cc["max"], _cc["now"])
    try:
        for piece in ["partial ", "stream ", "of ", "an ", "answer"]:
            time.sleep(0.2)
            yield piece
    finally:
        with _lk:
            _cc["now"] -= 1

with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})        # dead port -> deterministic
    pid = c.post("/projects", json={"name": "ask-a", "path": REPO, "exclude": []}).json()["id"]
    c.post("/ingest", json={"project_id": pid}); widle(c)
    pid2 = c.post("/projects", json={"name": "ask-b"}).json()["id"]

    # model "ready" + a fast deterministic stream for parts 1 & 3
    llm_client.is_ready = lambda timeout=2.0: True
    llm_client.chat_stream = lambda messages, **k: iter(["ground", "ed [1]"])

    # ================= PART 1 — PERSISTENCE =================
    ex1, _ = ask_done(c, pid, "what consumes the orders topic")
    check("1. an Ask is persisted as a retained exchange",
          ex1["status"] == "done" and ex1["answer"] == "grounded [1]")
    hist = c.get("/ask/history?scope=" + pid).json()
    check("1. retained thread survives (re-fetch returns the exchange + sources)",
          len(hist["exchanges"]) == 1 and hist["exchanges"][0]["answer"] == "grounded [1]"
          and (hist["exchanges"][0].get("meta") or {}).get("sources"))
    check("1. exchange carries a grounding-used indicator",
          "grounding_used" in hist["exchanges"][0])

    # per-scope isolation: project 2 has its own (empty) history
    h2 = c.get("/ask/history?scope=" + pid2).json()
    check("1. per-project isolation (other project history empty)", len(h2["exchanges"]) == 0)
    ask_done(c, pid2, "anything about project two")
    check("1. histories do not cross projects",
          len(c.get("/ask/history?scope=" + pid).json()["exchanges"]) == 1
          and len(c.get("/ask/history?scope=" + pid2).json()["exchanges"]) == 1)

    # at least the last 10 retained; older trimmed
    for i in range(2, 14):                            # 12 more -> 13 total for pid
        ask_done(c, pid, f"Q{i:02d}")
    hist = c.get("/ask/history?scope=" + pid).json()["exchanges"]
    qs = [e["question"] for e in hist]
    check("1. at least the last 10 exchanges retained (cap)", len(hist) == db.ASK_HISTORY_CAP)
    check("1. newest kept, oldest trimmed (order preserved oldest->newest)",
          qs[-1] == "Q13" and qs[0] == "Q04" and "what consumes the orders topic" not in qs,
          str(qs))
    check("1. persistence is SQLite-backed (survives reload/restart)",
          [e["question"] for e in db.ask_list(pid)] == qs)

    # multi-turn context: the next ask folds prior turns in
    pairs = conversation.context_pairs(pid, None)
    check("1. prior turns available for multi-turn context", len(pairs) >= 1)

    # new conversation / clear
    c.post("/ask/clear", json={"scope": pid})
    check("1. clear / new-conversation empties the retained thread",
          len(c.get("/ask/history?scope=" + pid).json()["exchanges"]) == 0
          and len(c.get("/ask/history?scope=" + pid2).json()["exchanges"]) == 1)  # other scope intact

    # ================= PART 2 — SINGLE-FLIGHT =================
    llm_client.chat_stream = slow_stream
    _cc.update(now=0, max=0, calls=0)
    a = c.post("/ask", json={"scope": pid, "question": "run A"}).json()
    b = c.post("/ask", json={"scope": pid, "question": "run B"}).json()
    # wait for A to start, then B must still be queued (pending behind A)
    for _ in range(80):
        if c.get("/jobs/" + a["job_id"]).json()["status"] == "running":
            break
        time.sleep(0.05)
    jb = c.get("/jobs/" + b["job_id"]).json()
    check("2. a second Ask PENDS (queued) while the first runs", jb["status"] == "queued", jb["status"])
    widle(c)
    check("2. NO concurrent inference (max in-flight == 1)", _cc["max"] == 1, str(_cc))
    exA = c.get("/ask/exchange/" + a["exchange_id"]).json()
    exB = c.get("/ask/exchange/" + b["exchange_id"]).json()
    check("2. both queued asks eventually completed", exA["status"] == "done" and exB["status"] == "done")

    # cancel a pending Ask
    _cc.update(now=0, max=0, calls=0)
    a = c.post("/ask", json={"scope": pid, "question": "long A"}).json()
    b = c.post("/ask", json={"scope": pid, "question": "cancel me"}).json()
    for _ in range(80):
        if c.get("/jobs/" + a["job_id"]).json()["status"] == "running":
            break
        time.sleep(0.05)
    canc = c.post("/jobs/" + b["job_id"] + "/cancel", json={}).json()
    widle(c)
    exB = c.get("/ask/exchange/" + b["exchange_id"]).json()
    exA = c.get("/ask/exchange/" + a["exchange_id"]).json()
    check("2. a pending Ask can be cancelled (removed from queue, never ran)",
          exB["status"] == "cancelled" and not exB.get("answer"))
    check("2. the running Ask still completes after a peer is cancelled", exA["status"] == "done")
    check("2. server-ready gate preserved (409 when not ready)",
          (lambda: (setattr(llm_client, "is_ready", lambda timeout=2.0: False),
                    c.post("/ask", json={"scope": pid, "question": "x"}).status_code == 409,
                    setattr(llm_client, "is_ready", lambda timeout=2.0: True))[1])())

    # ================= PART 3 — SAVE-TO-CASES =================
    llm_client.chat_stream = lambda messages, **k: iter(["The order consumer ", "is OrderListener [1]"])
    c.post("/ask/clear", json={"scope": pid})
    ex, jref = ask_done(c, pid, "kafka order listener consumer service code")
    code_before = vectorstore.get_code_store(pid).count()
    cases_before = vectorstore.get_cases_store(pid).count()

    sv = c.post("/ask/save-case", json={"scope": pid, "exchange_id": ex["id"]}).json()
    check("3. save icon creates a solved case", bool(sv.get("case_id")) and not sv.get("already_saved"))
    check("3. case embedded into the SEPARATE cases layer (+1), NOT the code index",
          vectorstore.get_cases_store(pid).count() == cases_before + 1
          and vectorstore.get_code_store(pid).count() == code_before,
          f"cases {cases_before}->{vectorstore.get_cases_store(pid).count()} code {code_before}->{vectorstore.get_code_store(pid).count()}")
    exsaved = c.get("/ask/exchange/" + ex["id"]).json()
    check("3. exchange reflects saved state (saved_case_id set)",
          exsaved.get("saved_case_id") == sv["case_id"])

    # no silent double-save
    sv2 = c.post("/ask/save-case", json={"scope": pid, "exchange_id": ex["id"]}).json()
    check("3. no silent double-save (idempotent)",
          sv2.get("already_saved") is True and sv2["case_id"] == sv["case_id"]
          and vectorstore.get_cases_store(pid).count() == cases_before + 1)

    # content + provenance (question, answer, file refs WITH hashes, grounding, ts)
    saved_case = [x for x in c.get("/cases?scope=" + pid).json()["cases"] if x["id"] == sv["case_id"]][0]
    check("3. case records question + answer", saved_case["problem_text"] == "kafka order listener consumer service code"
          and "OrderListener" in saved_case["resolution_summary"])
    check("3. case lists in the Cases tab", saved_case["id"] == sv["case_id"])
    refs = saved_case.get("file_refs", [])
    check("3. case carries file refs WITH content hashes (provenance)",
          len(refs) >= 1 and all(r.get("file_hash_at_save") for r in refs), str(len(refs)))
    check("3. grounding used recorded on the case",
          any(t.startswith("grounding:") for t in saved_case.get("tags", [])))
    check("3. created_at timestamp recorded", bool(saved_case.get("created_at")))

    # surfaces as supporting material in a FUTURE ask (cases fast-path)
    built = ask.build_grounded([pid], "who consumes the order events", [], k=8)
    check("3. saved case surfaces as a (distinct) source in future answers",
          any(s["kind"] == "case" for s in built["sources"]))

    # staleness: a later change to a referenced file flags the case stale
    tmpdir = os.path.join(ROOT, "tmp"); os.makedirs(tmpdir, exist_ok=True)
    tf = os.path.join(tmpdir, "StaleProbe.java").replace("\\", "/")
    open(tf, "w", encoding="utf-8").write("class StaleProbe { void v1(){} }")
    fake_ex = {"id": "x", "question": "stale probe", "answer": "see file [1]",
               "grounding_kinds": ["code"],
               "meta": {"sources": [{"kind": "code", "file_path": tf, "title": "StaleProbe"}], "raw": {}}}
    scase = cases.save_case(pid, conversation.case_from_exchange(fake_ex))
    fresh = [x for x in cases.list_cases([pid]) if x["id"] == scase["id"]][0]
    open(tf, "w", encoding="utf-8").write("class StaleProbe { void v2(){ /* changed */ } }")
    after = [x for x in cases.list_cases([pid]) if x["id"] == scase["id"]][0]
    check("3. referenced-file change flags the case stale",
          fresh["stale"] is False and after["stale"] is True and tf in after.get("stale_files", []))
    try: os.remove(tf)
    except Exception: pass

    # a DELETED referenced file must ALSO flag stale (regression: '' live hash)
    tf2 = os.path.join(tmpdir, "DeleteProbe.java").replace("\\", "/")
    open(tf2, "w", encoding="utf-8").write("class DeleteProbe {}")
    dex = {"id": "y", "question": "delete probe", "answer": "see [1]", "grounding_kinds": ["code"],
           "meta": {"sources": [{"kind": "code", "file_path": tf2, "title": "DeleteProbe"}], "raw": {}}}
    dcase = cases.save_case(pid, conversation.case_from_exchange(dex))
    fresh2 = [x for x in cases.list_cases([pid]) if x["id"] == dcase["id"]][0]
    os.remove(tf2)
    after2 = [x for x in cases.list_cases([pid]) if x["id"] == dcase["id"]][0]
    check("3. a DELETED referenced file also flags the case stale",
          fresh2["stale"] is False and after2["stale"] is True)

    # ================= REGRESSION FIXES (from adversarial review) =================
    # running-ask cancel is honored on EVERY delta (not only on the 0.25s flush)
    llm_client.chat_stream = slow_stream
    a = c.post("/ask", json={"scope": pid, "question": "cancel me mid-stream"}).json()
    for _ in range(80):
        if c.get("/jobs/" + a["job_id"]).json()["status"] == "running":
            break
        time.sleep(0.05)
    c.post("/jobs/" + a["job_id"] + "/cancel", json={})
    widle(c)
    exa = c.get("/ask/exchange/" + a["exchange_id"]).json()
    check("R. a RUNNING ask cancel is honored (not resurrected to 'done')",
          exa["status"] == "cancelled", exa["status"])

print(f"\n{sum(1 for _, c in results if c)}/{len(results)} checks passed")
sys.exit(0 if all(c for _, c in results) else 1)
