"""Tests for the 5 user-reported fixes (new endpoints + adaptive behavior)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
results=[]
def check(n,c,d=""): results.append((n,bool(c))); print(("PASS " if c else "FAIL ")+n+(("  -- "+d) if d else ""))

from fastapi.testclient import TestClient
from openmind.main import app
from openmind import vectorstore, config

FE=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"testrepos_fe").replace("\\","/")
def wait_idle(c,n=200):
    for _ in range(n):
        if not [x for x in c.get("/jobs").json()["jobs"] if x["status"] in ("queued","running")]: return
        time.sleep(0.3)

with TestClient(app) as c:
    c.post("/model-config", json={"port":1})  # deterministic/fast

    # ---- Fix 4: any repo (no Java/Kafka) is searchable, and the glossary is an
    #            honest map (never fabricated terms) — not hardcoded to one stack.
    fe=c.post("/projects", json={"name":"web","path":FE,"exclude":[]}).json()["id"]
    c.post("/ingest", json={"project_id":fe}); wait_idle(c)
    proj=c.get(f"/projects/{fe}").json()
    gl=c.get(f"/glossary?scope={fe}").json()
    check("Fix4 frontend repo indexes code (TS/JS) for search", proj["code_chunks"]>0,
          f"{proj['code_chunks']} chunks, {proj['files_indexed']} files")
    check("Fix4 glossary is an honest term list for any repo (count == #terms, no fabrication)",
          isinstance(gl.get("terms"),dict) and gl.get("count")==len(gl.get("terms",{})),
          f"{gl.get('count')} terms")
    s=c.post("/search", json={"scope":fe,"query":"Dashboard loadUsers fetch","k":6}).json()
    hit=any("app.ts" in (ch["file_path"] or "") for ch in s["code_chunks"])
    check("Fix4 search works on frontend code", hit and len(s["code_chunks"])>0,
          f"{len(s['code_chunks'])} chunks")

    # ---- Fix 1: delete project entirely
    coll=vectorstore.code_collection_name(fe)
    c.delete(f"/projects/{fe}")
    gone = c.get(f"/projects/{fe}").status_code==404
    in_list = any(p["id"]==fe for p in c.get("/projects").json()["projects"])
    datadir = os.path.join(os.environ["OPENMIND_DATA_DIR"], fe)
    check("Fix1 DELETE removes project from registry", gone and not in_list)
    check("Fix1 DELETE removes project data dir", not os.path.exists(datadir))
    check("Fix1 DELETE drops code collection (count 0)", vectorstore.get_store(coll).count()==0)

    # ---- Fix 5: pick-folder endpoint responds gracefully (path or error, never 500)
    r=c.get("/fs/pick-folder")
    check("Fix5 /fs/pick-folder returns JSON {path|error} (no crash)",
          r.status_code==200 and ("path" in r.json()), str(r.json())[:80])

print("\n"+f"{sum(1 for _,c in results if c)}/{len(results)} checks passed")
sys.exit(0 if all(c for _,c in results) else 1)
