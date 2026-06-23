"""Async project-delete acceptance — the concurrency case that matters: delete
WHILE an ingest is active, then assert the project (a) vanishes from the API
immediately with {deleting}, (b) NEVER reappears even as the terminated worker
unwinds (the one-way 'deleting' tombstone), and (c) its storage is eventually freed.

Runs fully ISOLATED + CPU-only so it never touches a live data dir or the GPU.
Run:  python tests/verify_async_delete.py
"""
import os
import sys
import tempfile
import time

# isolate everything BEFORE importing the app (see memory: isolate-test-datadir)
os.environ["OPENMIND_DATA_DIR"] = tempfile.mkdtemp(prefix="om_asyncdel_")
os.environ["OPENMIND_MACHINE_DIR"] = tempfile.mkdtemp(prefix="om_machine_")
os.environ["OPENMIND_EMBED_DEVICE"] = "cpu"      # never grab the GPU
os.environ["OPENMIND_EMBED_OFFLINE"] = "1"       # hashing embedder: fast, no download
os.environ["OPENMIND_INGEST_FREE_GPU"] = "0"     # never touch a model server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
from openmind.main import app  # noqa: E402
from openmind import vectorstore, db, config  # noqa: E402

REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "openmind").replace("\\", "/")
results = []


def check(n, c, d=""):
    results.append(bool(c))
    print(("PASS " if c else "FAIL ") + n + (("  -- " + str(d)) if d else ""))


with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})    # dead model -> deterministic, not-ready
    pid = c.post("/projects", json={"name": "doomed", "path": REPO, "exclude": []}).json()["id"]
    datadir = config.project_dir(pid)

    # kick off an ingest and DELETE without waiting for it to finish
    c.post("/ingest", json={"project_id": pid})
    r = c.delete(f"/projects/{pid}")
    check("DELETE returns immediately with {deleting}",
          r.status_code == 200 and r.json().get("deleting") == pid, r.json())
    check("project is 404 right after delete", c.get(f"/projects/{pid}").status_code == 404)
    check("project absent from /projects right after delete",
          not any(p["id"] == pid for p in c.get("/projects").json()["projects"]))

    # it must NEVER reappear while the terminated ingest's worker unwinds (the
    # tombstone race: a job handler writing 'paused'/'ready' must not revive it)
    reappeared = False
    for _ in range(60):
        if any(p["id"] == pid for p in c.get("/projects").json()["projects"]):
            reappeared = True
            break
        time.sleep(0.05)
    check("project NEVER reappears in the listing during cleanup", not reappeared)

    # storage is eventually freed and the row removed for real
    cleaned = False
    for _ in range(200):
        if not os.path.exists(datadir):
            cleaned = True
            break
        time.sleep(0.1)
    check("data dir eventually removed (background cleanup finished)", cleaned)
    check("entity row dropped (not even in state='deleting')",
          not any(p["id"] == pid for p in db.list_projects("deleting")))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
