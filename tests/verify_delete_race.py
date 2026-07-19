"""Project delete must actually COMPLETE — the 'deleting' tombstone is not
allowed to survive its own cleanup.

REGRESSION UNDER TEST
---------------------
Two things ran concurrently at every boot:

  * ``jobs._recover_on_restart`` spawned ``_cleanup_deleted`` for each project
    in state 'deleting' (the owner of that project's storage), and
  * the startup janitor called
    ``vectorstore.drop_orphan_collections({p["id"] for p in db.list_projects()})``.

``db.list_projects()`` EXCLUDES 'deleting', so the tombstoned project's id was
missing from that set and the janitor classified its live ``code_``/``cases_``
collections as orphans — starting a second drain of collections the cleanup
thread already owned. Whichever thread reached ``delete_collection`` first made
the other's next ``get()`` raise; ``_drop_chroma_collection`` reported that as
"interrupted" (False), and ``_cleanup_deleted`` returned early — BEFORE removing
the data dir and BEFORE ``db.delete_project``. The tombstone came back on the
next boot and the cycle repeated, so the project could never finish deleting
while every boot re-ran a multi-minute GIL-heavy drain.

The three defences asserted here are independent on purpose — any one of them
alone stops the bug, and each is cheap:
  1. the janitor's "known" set includes 'deleting' projects, so it never
     classifies an owned collection as an orphan;
  2. ``_drop_chroma_collection`` refuses a second concurrent drain of one name;
  3. "collection already gone" is reported as success, not interruption.

Runs fully ISOLATED + CPU-only.  Run:  python tests/verify_delete_race.py
"""
import os
import sys
import tempfile
import threading
import time

os.environ["OPENMIND_DATA_DIR"] = tempfile.mkdtemp(prefix="om_delrace_")
os.environ["OPENMIND_MACHINE_DIR"] = tempfile.mkdtemp(prefix="om_machine_")
os.environ["OPENMIND_EMBED_DEVICE"] = "cpu"
os.environ["OPENMIND_EMBED_OFFLINE"] = "1"
os.environ["OPENMIND_INGEST_FREE_GPU"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openmind import config, db, jobs, vectorstore  # noqa: E402
from openmind.services.workspace_service import WorkspaceService  # noqa: E402

N_CHUNKS = 1200
DIM = 384
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name
          + (("  -- " + str(detail)) if detail else ""), flush=True)


def inflate(pid, n=N_CHUNKS):
    """A learned-size code collection, without paying for a real ingest."""
    import random
    rnd = random.Random(11)
    store = vectorstore.get_code_store(pid)
    doc = "class Foo {\n" + "  void m() { int x = 1; }\n" * 8 + "}\n"
    for start in range(0, n, 1000):
        cnt = min(1000, n - start)
        store.add(ids=[f"{pid}_c{start+i}" for i in range(cnt)],
                  embeddings=[[rnd.random() for _ in range(DIM)] for _ in range(cnt)],
                  documents=[doc] * cnt,
                  metadatas=[{"project_id": pid, "file": f"src/F{i}.java"}
                             for i in range(cnt)])
    config.ensure_project_dirs(pid)
    (config.project_map_dir(pid) / "glossary.json").write_text("{}", encoding="utf-8")


def collection_names():
    if vectorstore.backend_name() != "chroma":
        return set()
    return {c.name for c in vectorstore._chroma_client.list_collections()}


db.init_db()
print(f"[setup] backend = {vectorstore.backend_name()}", flush=True)

# ---------------------------------------------------------------------------
# 1. The janitor must not treat an owned ('deleting') collection as an orphan
# ---------------------------------------------------------------------------
proj = db.create_project("racy")
pid = proj["id"]
inflate(pid)
db.set_project_state(pid, "deleting")

code_name = vectorstore.code_collection_name(pid)
check("setup: tombstoned project still owns its collection",
      code_name in collection_names(), code_name)

# the OLD, buggy "known" set — exactly what main.py used to pass
buggy_known = {p["id"] for p in db.list_projects()}
check("the old 'known' set really did omit the deleting project (bug is real)",
      pid not in buggy_known, f"known={sorted(buggy_known)}")

# the FIXED set, as computed in main.py's _warm_vectorstore
fixed_known = {p["id"] for p in db.list_projects()} | \
              {p["id"] for p in db.list_projects("deleting")}
check("the fixed 'known' set includes the deleting project", pid in fixed_known)

dropped = vectorstore.drop_orphan_collections(fixed_known)
check("janitor leaves an owned 'deleting' collection alone",
      code_name not in dropped and code_name in collection_names(),
      f"dropped={dropped}")

# The real guard: run the ACTUAL startup janitor against a tombstoned project
# whose cleanup has not been spawned. Deterministic — no thread interleaving to
# get lucky with. If main.py's "known" union is ever reverted to a bare
# db.list_projects(), the janitor eats this collection and the check fails.
from openmind import main as om_main  # noqa: E402
om_main._warm_vectorstore()
check("the real startup janitor does not drop a 'deleting' project's collection",
      code_name in collection_names(),
      "main._warm_vectorstore() dropped a collection its cleanup owns")
check("the real startup janitor leaves the tombstone's data dir in place",
      config.project_dir(pid).exists())

# ---------------------------------------------------------------------------
# 2. Two concurrent drains of ONE collection never both proceed
# ---------------------------------------------------------------------------
# Split into a deterministic half and a timing-free half ON PURPOSE. Asserting
# "the second call returned fast while the first was still running" would make
# this a wall-clock race on a shared CI runner, and the acceptance runner's own
# rule is that a flaky gate is worse than an honest exclusion. So:
#   2a occupies the name directly and asserts the refusal — no threads, no clock;
#   2b runs a real drain and asserts only that it claims and releases the name.
if vectorstore.backend_name() == "chroma":
    occupied = "code_p_occupied000"
    with vectorstore._dropping_lock:
        vectorstore._dropping.add(occupied)
    try:
        refused = vectorstore.drop_collection(occupied)
    finally:
        with vectorstore._dropping_lock:
            vectorstore._dropping.discard(occupied)
    check("a drain of an already-in-flight collection is refused",
          refused is False, refused)
    check("...and the refusal did not leave the name claimed",
          occupied not in vectorstore._dropping)

    # A real drain must claim the name for its duration and release it after.
    _orig_batch = vectorstore.DROP_BATCH
    vectorstore.DROP_BATCH = 50            # many small batches -> observable
    first, seen_claimed = {}, threading.Event()

    def slow_drain():
        first["result"] = vectorstore.drop_collection(code_name)

    watcher_stop = threading.Event()

    def watcher():
        while not watcher_stop.is_set():
            if code_name in vectorstore._dropping:
                seen_claimed.set()
                return
            time.sleep(0.005)

    w = threading.Thread(target=watcher, name="claim-watcher")
    w.start()
    t = threading.Thread(target=slow_drain, name="slow-drain")
    t.start()
    t.join(timeout=300)
    watcher_stop.set()
    w.join(timeout=5)
    vectorstore.DROP_BATCH = _orig_batch

    check("a drain in progress registers itself as in-flight",
          seen_claimed.is_set())
    check("the drain completed the collection", first.get("result") is True,
          first)
    check("the collection is gone once the drain finished",
          code_name not in collection_names())
    check("the in-flight guard released the name",
          code_name not in vectorstore._dropping)
else:
    for skipped in ("a drain of an already-in-flight collection is refused",
                    "...and the refusal did not leave the name claimed",
                    "a drain in progress registers itself as in-flight",
                    "the drain completed the collection",
                    "the collection is gone once the drain finished",
                    "the in-flight guard released the name"):
        check(skipped + " [numpy backend: n/a]", True)

# ---------------------------------------------------------------------------
# 3. A collection that vanishes MID-DRAIN is success, not "interrupted"
# ---------------------------------------------------------------------------
# This is the exact shape of the original bug: the losing racer must not report
# failure, because its caller (_cleanup_deleted) reads False as "stop now" and
# returns before removing the data dir and the project row.
# Forced rather than raced: a stub collection that reports rows once and then
# claims not to exist is EXACTLY what the losing thread saw, and it reproduces
# that on every run instead of only when the scheduler cooperates.
class _VanishingCollection:
    def __init__(self):
        self.batches = 0

    def count(self):
        return 500

    def get(self, limit=None, include=None):
        self.batches += 1
        if self.batches == 1:
            return {"ids": [f"x{i}" for i in range(min(limit or 50, 50))]}
        raise ValueError("Collection does not exist.")   # deleted underneath us

    def delete(self, ids=None):
        pass


class _VanishingClient:
    def __init__(self, col):
        self.col = col
        self.deleted = []

    def get_collection(self, name):
        return self.col

    def delete_collection(self, name):
        self.deleted.append(name)


_saved_client, _saved_backend = vectorstore._chroma_client, vectorstore._backend
stub = _VanishingClient(_VanishingCollection())
vectorstore._chroma_client, vectorstore._backend = stub, "chroma"
try:
    vanished = vectorstore.drop_collection("code_p_vanishes00")
finally:
    vectorstore._chroma_client, vectorstore._backend = _saved_client, _saved_backend

check("a drain whose collection vanished mid-way reports SUCCESS",
      vanished is True,
      f"got {vanished!r} — _cleanup_deleted reads False as 'stop now' and would "
      f"return before the rmtree and the row delete, stranding the tombstone")
check("...and the vanished drain did not leave the name claimed",
      "code_p_vanishes00" not in vectorstore._dropping)

check("dropping an already-gone collection reports success",
      vectorstore.drop_collection(code_name) is True)
check("dropping a never-existed collection reports success",
      vectorstore.drop_collection("code_p_doesnotexist") is True)
check("_is_missing_collection recognizes chroma's not-found wording",
      vectorstore._is_missing_collection(ValueError("Collection does not exist."))
      and not vectorstore._is_missing_collection(OSError("disk exploded")))

# ---------------------------------------------------------------------------
# 4. END TO END: the boot sequence that used to strand the tombstone forever
# ---------------------------------------------------------------------------
proj2 = db.create_project("boot-racy")
pid2 = proj2["id"]
inflate(pid2)
jobs.request_delete(pid2)          # tombstone + spawn the owning cleanup

# ...and immediately run the REAL startup janitor, exactly as lifespan does one
# line later. Calling main._warm_vectorstore() rather than re-deriving its
# "known" set here is the point: this is what guards the fix at main.py, and it
# is what fails if someone reverts that union to a bare db.list_projects().
janitor = threading.Thread(target=om_main._warm_vectorstore, name="janitor")
janitor.start()
janitor.join(timeout=120)

# Wait on THIS project only. Section 1 deliberately leaves its own tombstone
# behind, so a "no projects are deleting" condition would never come true and
# this loop would burn its whole timeout on every run.
deadline = time.time() + 180
while time.time() < deadline:
    if db.get_project(pid2) is None and \
            not [p for p in db.list_projects("deleting") if p["id"] == pid2]:
        break
    time.sleep(0.25)

check("tombstone did NOT survive the janitor race",
      not [p for p in db.list_projects("deleting") if p["id"] == pid2],
      f"{pid2} still tombstoned")
check("project row is fully gone (delete actually completed)",
      db.get_project(pid2) is None)
check("data dir reclaimed", not config.project_dir(pid2).exists())
check("no leftover collections for the deleted project",
      not [n for n in collection_names() if pid2 in n], collection_names())

# ---------------------------------------------------------------------------
# 5. The cheap read path: counts must not CREATE storage, and must agree
# ---------------------------------------------------------------------------
proj3 = db.create_project("counts")
pid3 = proj3["id"]
inflate(proj3["id"], n=400)
db.upsert_file_index(pid3, "src/A.java", "h1", chunk_ids=["a1", "a2"])
db.upsert_file_index(pid3, "src/B.java", "h2", chunk_ids=["b1"])

check("count_file_index agrees with len(get_file_index)",
      db.count_file_index(pid3) == len(db.get_file_index(pid3)) == 2,
      db.count_file_index(pid3))

before = collection_names()
cases_name = vectorstore.cases_collection_name(pid3)
n_cases = vectorstore.count_collection(cases_name)
check("count_collection reports 0 for a collection that does not exist",
      n_cases == 0, n_cases)
check("count_collection did NOT create the collection as a side effect",
      collection_names() == before, collection_names() - before)
check("count_collection reports the real size of an existing collection",
      vectorstore.count_collection(vectorstore.code_collection_name(pid3)) == 400)

svc = WorkspaceService()
d = svc.describe(pid3)
check("describe() still returns the documented shape",
      d["code_chunks"] == 400 and d["cases_count"] == 0 and d["files_indexed"] == 2,
      {k: d[k] for k in ("code_chunks", "cases_count", "files_indexed")})

jobs.begin_shutdown()
print(f"\n{sum(results)}/{len(results)} checks passed", flush=True)
sys.exit(0 if all(results) else 1)
