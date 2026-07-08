"""Delete responsiveness + resume acceptance — the two failure modes that made
project delete look hung:

  1. chroma 1.x rust bindings hold the GIL for a whole call, so a one-shot
     delete_collection on a learned project froze EVERY thread (all HTTP,
     SSE, the worker, and SIGINT — Ctrl+C appeared dead) for its duration.
     The fix drains in small batches; this test asserts the API keeps
     answering (bounded latency) WHILE a big collection is being dropped.
  2. a kill mid-delete rolled the whole transaction back, so the delete never
     completed across restarts. The fix makes cleanup interruptible +
     resumable; this test interrupts it (begin_shutdown) and asserts a
     simulated restart finishes the job.

Runs fully ISOLATED + CPU-only. Run:  python tests/verify_delete_responsive.py
"""
import os
import sys
import tempfile
import time

os.environ["OPENMIND_DATA_DIR"] = tempfile.mkdtemp(prefix="om_delresp_")
os.environ["OPENMIND_MACHINE_DIR"] = tempfile.mkdtemp(prefix="om_machine_")
os.environ["OPENMIND_EMBED_DEVICE"] = "cpu"
os.environ["OPENMIND_EMBED_OFFLINE"] = "1"
os.environ["OPENMIND_INGEST_FREE_GPU"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402
from openmind.main import app  # noqa: E402
from openmind import config, db, jobs, vectorstore  # noqa: E402

N_CHUNKS = 6000
DIM = 384
results = []


def check(n, c, d=""):
    results.append(bool(c))
    print(("PASS " if c else "FAIL ") + n + (("  -- " + str(d)) if d else ""), flush=True)


def inflate(pid: str, n: int = N_CHUNKS) -> None:
    """Give the project a learned-size code collection without a real ingest."""
    import random
    rnd = random.Random(7)
    store = vectorstore.get_code_store(pid)
    doc = "class Foo {\n" + "  void m() { int x = 1; }\n" * 8 + "}\n"
    for start in range(0, n, 1000):
        cnt = min(1000, n - start)
        ids = [f"{pid}_c{start + i}" for i in range(cnt)]
        embs = [[rnd.random() for _ in range(DIM)] for _ in range(cnt)]
        store.add(ids=ids, embeddings=embs, documents=[doc] * cnt,
                  metadatas=[{"project_id": pid, "file": f"src/F{i}.java"}
                             for i in range(cnt)])
    config.ensure_project_dirs(pid)
    (config.project_map_dir(pid) / "glossary.json").write_text("{}", encoding="utf-8")


def collection_names():
    if vectorstore.backend_name() != "chroma":
        return set()
    return {c.name for c in vectorstore._chroma_client.list_collections()}


def wait_gone(c, pid: str, datadir, timeout: float = 180.0):
    """Poll /projects while the cleanup runs; return (fully_gone, max_latency)."""
    deadline = time.time() + timeout
    max_lat = 0.0
    while time.time() < deadline:
        t0 = time.time()
        r = c.get("/projects")
        max_lat = max(max_lat, time.time() - t0)
        assert r.status_code == 200
        row_gone = not db.list_projects("deleting") and \
            not any(p["id"] == pid for p in r.json()["projects"])
        if row_gone and not os.path.exists(datadir) and \
                f"code_{pid}" not in collection_names():
            return True, max_lat
        time.sleep(0.05)
    return False, max_lat


with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})   # dead model -> deterministic

    # ---- 1) responsiveness: API keeps answering while a big drop drains ----
    pid = c.post("/projects", json={"name": "big", "path": ".", "exclude": []}).json()["id"]
    inflate(pid)
    check(f"seeded {N_CHUNKS} chunks",
          vectorstore.get_code_store(pid).count() == N_CHUNKS)
    datadir = config.project_dir(pid)

    t0 = time.time()
    r = c.delete(f"/projects/{pid}")
    took = time.time() - t0
    check("DELETE returns immediately with {deleting}",
          r.status_code == 200 and r.json().get("deleting") == pid and took < 2.0,
          f"{took:.2f}s")
    gone, max_lat = wait_gone(c, pid, datadir)
    check("storage fully reclaimed (row + collection + data dir)", gone)
    check("API stayed responsive during the drop (no GIL freeze)",
          max_lat < 2.0, f"max /projects latency {max_lat:.2f}s")

    # ---- 2) interrupted cleanup resumes after a (simulated) restart ----
    pid2 = c.post("/projects", json={"name": "doomed2", "path": ".", "exclude": []}).json()["id"]
    inflate(pid2)
    datadir2 = config.project_dir(pid2)
    c.delete(f"/projects/{pid2}")
    time.sleep(0.3)                    # let the drain start
    jobs.begin_shutdown()              # what lifespan does on Ctrl+C
    for _ in range(100):               # cleanup thread should stop within ~a batch
        with jobs._deleting_lock:
            if pid2 not in jobs._deleting:
                break
        time.sleep(0.1)
    with jobs._deleting_lock:
        stopped = pid2 not in jobs._deleting
    check("cleanup stops promptly on shutdown", stopped)
    still_tombstoned = any(p["id"] == pid2 for p in db.list_projects("deleting"))
    survived = (f"code_{pid2}" in collection_names()) or os.path.exists(datadir2)
    check("interrupted delete keeps the 'deleting' tombstone (not lost, not leaked)",
          still_tombstoned and (survived or vectorstore.backend_name() != "chroma"))

    # simulated restart: clear the shutdown flag + run the recovery pass
    jobs._shutdown.clear()
    jobs._recover_on_restart()
    gone2, max_lat2 = wait_gone(c, pid2, datadir2)
    check("resumed cleanup finishes the delete after restart", gone2)
    check("API stayed responsive during resumed drop",
          max_lat2 < 2.0, f"max latency {max_lat2:.2f}s")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
