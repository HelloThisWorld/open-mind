"""Runtime composition root — bootstrap idempotency, worker opt-in, and the
single shared version.

The property that matters most here: bootstrap must be idempotent and must NOT
start a background worker. Before Phase 1, ``jobs.start_worker()`` ran only
under FastAPI, so a job enqueued from anywhere else sat queued forever; the fix
must not overcorrect into spawning a thread for every ``doctor`` invocation.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

import threading  # noqa: E402

from openmind import config, db, jobs  # noqa: E402
from openmind.runtime import OpenMindRuntime, get_runtime, reset_runtime  # noqa: E402
from openmind.services.service_container import ServiceContainer  # noqa: E402
from openmind.version import RUNTIME_VERSION  # noqa: E402

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc + (f"  [{extra}]" if extra and not cond else ""))


def _worker_threads():
    return [t for t in threading.enumerate() if t.name == "openmind-job-worker"]


# ---------------------------------------------------------------------------
# Version is single-sourced
# ---------------------------------------------------------------------------
import openmind  # noqa: E402
from openmind import artifacts  # noqa: E402

check("runtime version is a pre-v2 development version, not a false 2.0.0",
      RUNTIME_VERSION.startswith("1.") and "dev" in RUNTIME_VERSION)
check("openmind.__version__ is the runtime version",
      openmind.__version__ == RUNTIME_VERSION)
check("the artifact generator reports the runtime version",
      artifacts._generator_version() == RUNTIME_VERSION)
check("the artifact SCHEMA version is NOT tied to the runtime version "
      "(it is a frozen integration contract)",
      artifacts.SCHEMA_VERSION == "1.1.0")

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
runtime = OpenMindRuntime()
before_workers = len(_worker_threads())

container = runtime.bootstrap()
check("bootstrap returns a service container",
      isinstance(container, ServiceContainer))
check("bootstrap is idempotent: the same container comes back",
      runtime.bootstrap() is container)
check("bootstrap is idempotent a third time",
      runtime.bootstrap() is container)
check("bootstrap created the data directory", os.path.isdir(str(config.DATA_DIR)))
check("bootstrap created the database", os.path.isfile(str(config.DB_PATH)))
check("bootstrap migrated the schema to head",
      db.migration_status()["version"] >= 2)

check("bootstrap does NOT start the job worker",
      len(_worker_threads()) == before_workers and not runtime.worker_running)

# ---------------------------------------------------------------------------
# Services are cached and wired
# ---------------------------------------------------------------------------
check("workspace service is cached", runtime.workspaces is runtime.workspaces)
check("job service is cached", runtime.jobs is runtime.jobs)
check("ingest service is cached", runtime.ingest is runtime.ingest)
check("export service is cached", runtime.export is runtime.export)
check("health service is cached", runtime.health is runtime.health)
check("the runtime exposes its version", runtime.version == RUNTIME_VERSION)

info = runtime.info()
for key in ("version", "data_dir", "database", "schema_version", "worker_running"):
    check(f"runtime.info() reports {key}", key in info)
check("runtime.info() reports the isolated data dir, not the live one",
      str(config.DATA_DIR) in info["data_dir"])

# ---------------------------------------------------------------------------
# Worker is opt-in, and idempotent once asked for
# ---------------------------------------------------------------------------
runtime.ensure_worker()
check("ensure_worker() starts the worker", runtime.worker_running)
check("ensure_worker() actually spawned the worker thread",
      len(_worker_threads()) == before_workers + 1
      or jobs._worker_started)  # engine flag: a prior test may have started it
runtime.ensure_worker()
runtime.ensure_worker()
check("ensure_worker() is idempotent: no duplicate worker threads",
      len(_worker_threads()) <= before_workers + 1)

# ---------------------------------------------------------------------------
# Process-wide runtime
# ---------------------------------------------------------------------------
shared = get_runtime()
check("get_runtime() returns the same instance every time", get_runtime() is shared)
check("get_runtime() returns a bootstrapped runtime",
      isinstance(shared.services, ServiceContainer))
check("get_runtime() shares one service container",
      get_runtime().workspaces is shared.workspaces)

reset_runtime()
fresh = get_runtime()
check("reset_runtime() lets a test rebuild the process-wide runtime",
      fresh is not shared)

# ---------------------------------------------------------------------------
# Thread safety: concurrent bootstrap must produce ONE container
# ---------------------------------------------------------------------------
racy = OpenMindRuntime()
seen = []
barrier = threading.Barrier(8)


def _boot():
    barrier.wait()
    seen.append(racy.bootstrap())


threads = [threading.Thread(target=_boot) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("concurrent bootstrap yields exactly one container",
      len(seen) == 8 and len({id(c) for c in seen}) == 1)

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
