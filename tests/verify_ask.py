"""Acceptance for the grounded Ask backend: budgeted context, user-supplied
attachments labeled+cited, OCR graceful, model-not-ready refusal, SSE streaming."""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)
from fastapi.testclient import TestClient
from openmind.main import app
from openmind import ask, llm_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(ROOT, "fixtures", "testrepos").replace("\\", "/")
results = []
def check(n, c, d=""):
    results.append((n, bool(c))); print(("PASS " if c else "FAIL ")+n+(("  -- "+d) if d else ""))

def widle(c, n=300):
    for _ in range(n):
        if not [x for x in c.get("/jobs").json()["jobs"] if x["status"] in ("queued", "running")]:
            return
        time.sleep(0.3)

with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})    # dead -> deterministic + not-ready
    pid = c.post("/projects", json={"name": "ask", "path": REPO, "exclude": []}).json()["id"]
    c.post("/ingest", json={"project_id": pid}); widle(c)
    budget, att_cap = ask._budget()

    # --- build_grounded: attachment is user-supplied context, labeled + cited ---
    att = [{"name": "err.log", "kind": "text",
            "text": "ERROR NullPointerException at OrderListener.onOrder line 42",
            "status": "text attached (user-supplied context)"}]
    built = ask.build_grounded([pid], "why does the order consumer fail", att, k=8)
    user_src = [s for s in built["sources"] if s["kind"] == "user"]
    code_src = [s for s in built["sources"] if s["kind"] == "code"]
    umsg = built["messages"][1]["content"]
    check("attachment becomes a user-supplied source (labeled distinctly)",
          len(user_src) == 1 and "user-supplied" in user_src[0]["title"].lower())
    check("attachment text injected into prompt as USER-SUPPLIED",
          "USER-SUPPLIED ATTACHMENT" in umsg and "NullPointerException" in umsg)
    check("retrieval still contributes sources alongside the attachment", len(code_src) >= 1)
    check("sources are numbered for citation ([n])",
          all("n" in s for s in built["sources"]) and built["sources"][0]["n"] == 1)
    check("within context budget", built["meta"]["within_budget"])

    # --- oversized attachment is truncated to respect the budget ---
    big = "LOGLINE " * 60000     # ~480k chars
    built2 = ask.build_grounded([pid], "explain", [{"name": "big.log", "kind": "text",
              "text": big, "status": "text"}], k=8)
    m2 = built2["meta"]
    check("oversized attachment truncated to attachment cap",
          m2["attachment_chars"] <= att_cap and m2["attachment_chars"] > 0,
          f"att={m2['attachment_chars']} cap={att_cap}")
    check("assembled context stays within budget",
          m2["context_chars"] <= budget and m2["within_budget"],
          f"ctx={m2['context_chars']} budget={budget}")
    check("truncated attachment flagged in its source",
          any(s["kind"] == "user" and s.get("truncated") for s in built2["sources"]))

    # --- multi-turn: prior (q,a) history is compacted into the prompt ---
    built3 = ask.build_grounded([pid], "and the producer?", [], k=6,
              history=[("who consumes orders.created", "OrderListener consumes it [1]")])
    check("prior conversation folded into prompt (multi-turn context)",
          built3["meta"]["history_turns"] == 1
          and "CONVERSATION SO FAR" in built3["messages"][1]["content"]
          and "OrderListener consumes it" in built3["messages"][1]["content"])

    # --- OCR endpoint: graceful (no tesseract -> reference, never 500) ---
    r = c.post("/ocr", files={"file": ("shot.png", b"\x89PNG not-a-real-image", "image/png")})
    oj = r.json()
    check("/ocr graceful (200, never sends raw image to model)",
          r.status_code == 200 and oj["available"] is False and "reference" in oj["status"].lower())

    # --- /ask refuses when model server not ready (no doomed request) ---
    r = c.post("/ask", json={"scope": pid, "question": "hello"})
    check("/ask refuses (409) when model server not ready", r.status_code == 409)

    # --- /ask now ENQUEUES on the single-task worker; the answer streams into a
    #     persisted exchange that we read back (Parts 1+2). ---
    llm_client.is_ready = lambda timeout=2.0: True
    llm_client.chat_stream = lambda messages, **k: iter(["Per ", "[1]", " the log, ", "an NPE occurs."])
    r = c.post("/ask", json={"scope": pid, "question": "explain the error",
               "attachments": [{"name": "e.log", "kind": "text",
                                "text": "NPE at OrderListener", "status": "text attached"}]})
    j = r.json()
    check("/ask enqueues an ask job (returns job_id + exchange_id)",
          r.status_code == 200 and j.get("job_id") and j.get("exchange_id"), str(j))
    widle(c)
    ex = c.get("/ask/exchange/" + j["exchange_id"]).json()
    check("ask exchange completes (status done)", ex.get("status") == "done", str(ex.get("status")))
    check("streamed answer assembled + persisted",
          ex.get("answer") == "Per [1] the log, an NPE occurs.", str(ex.get("answer")))
    meta = ex.get("meta") or {}
    check("exchange meta carries user-supplied source + raw matches",
          any(s["kind"] == "user" for s in meta.get("sources", []))
          and "code_chunks" in (meta.get("raw") or {}))

print(f"\n{sum(1 for _, c in results if c)}/{len(results)} checks passed")
sys.exit(0 if all(c for _, c in results) else 1)
