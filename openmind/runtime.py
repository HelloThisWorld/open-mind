"""The composition root.

One bootstrap path, shared by the CLI, the MCP server, the FastAPI app and the
tests. Before this existed, three call sites initialized the process
differently: the FastAPI lifespan did directories + database + orphan sweep +
job worker, ``mcp_server`` did ``db.init_db()`` at import time, and a lazy
``db._c()`` did the same for anyone else. The most damaging consequence was that
``jobs.start_worker()`` ran ONLY under FastAPI, so a job enqueued from any other
process stayed ``queued`` forever.

BOOTSTRAP IS IDEMPOTENT AND ORDERED
-----------------------------------
1. ensure directories exist
2. open the database connection
3. migrate the schema to head
4. build the service container

Calling :meth:`bootstrap` again is a no-op that returns the same container.

THE WORKER IS OPT-IN
--------------------
Starting the job worker is deliberately NOT part of bootstrap.
``doctor``, ``status`` and ``export`` must not spawn a background thread, and
``mcp serve`` exposes a read/query surface that does not enqueue jobs. Surfaces
that genuinely need work executed — the FastAPI app, and ``ingest --wait`` —
call :meth:`ensure_worker` explicitly.

WHAT STAYS IN THE FASTAPI LIFESPAN
----------------------------------
The leaked-vector-segment sweep. It reads ``chroma.sqlite3`` with a raw sqlite
connection, which is only safe while no Chroma client exists in the process, and
it is a long-lived-server concern. Running it from short-lived CLI processes
would be both wasteful and unsafe against a live server.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from . import config, db
from . import jobs as jobs_engine   # aliased: the class exposes a `jobs` property
from .services.service_container import ServiceContainer
from .version import RUNTIME_VERSION


class OpenMindRuntime:
    """A bootstrapped OpenMind process: configuration, database, services."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._container: Optional[ServiceContainer] = None
        self._worker_started = False

    # -- lifecycle ----------------------------------------------------------
    def bootstrap(self) -> ServiceContainer:
        """Initialize the process and return the service container.

        Idempotent and thread-safe: concurrent callers get the same container,
        and the migration run happens once.
        """
        with self._lock:
            if self._container is None:
                config.ensure_dirs()
                db.init_db()            # opens the connection + migrates to head
                self._container = ServiceContainer(ensure_worker=self.ensure_worker)
            return self._container

    def ensure_worker(self) -> None:
        """Start the background job worker if it is not already running.

        ``jobs.start_worker()`` is itself idempotent; the extra flag here means
        a caller can ask repeatedly without re-entering the engine.
        """
        with self._lock:
            if self._worker_started:
                return
            self.bootstrap()
            jobs_engine.start_worker()
            self._worker_started = True

    def shutdown(self) -> None:
        """Ask background delete-cleanup to stop at its next batch.

        Does not join the worker thread — it is a daemon and exits with the
        process. ``deleting`` tombstones resume on the next start.
        """
        jobs_engine.begin_shutdown()

    # -- accessors ----------------------------------------------------------
    @property
    def services(self) -> ServiceContainer:
        return self.bootstrap()

    @property
    def workspaces(self):
        return self.services.workspaces

    @property
    def jobs(self):
        return self.services.jobs

    @property
    def ingest(self):
        return self.services.ingest

    @property
    def export(self):
        return self.services.export

    @property
    def health(self):
        return self.services.health

    @property
    def version(self) -> str:
        return RUNTIME_VERSION

    @property
    def worker_running(self) -> bool:
        return self._worker_started

    def info(self) -> Dict[str, Any]:
        """Identifying facts about this runtime, for ``doctor`` and health."""
        return {
            "version": RUNTIME_VERSION,
            "data_dir": str(config.DATA_DIR),
            "database": str(config.DB_PATH),
            "schema_version": db.migration_status().get("version", 0),
            "worker_running": self._worker_started,
        }


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------
_default: Optional[OpenMindRuntime] = None
_default_lock = threading.Lock()


def get_runtime() -> OpenMindRuntime:
    """The process-wide runtime, created and bootstrapped on first use.

    Every adapter goes through here, so there is exactly one database
    connection, one migration run and one service container per process.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = OpenMindRuntime()
        runtime = _default
    # Bound to a local INSIDE the lock, then bootstrapped outside it: bootstrap
    # runs migrations and must not hold the module lock, but re-reading the
    # global here would race a concurrent reset_runtime() and hit None.
    runtime.bootstrap()
    return runtime


def reset_runtime() -> None:
    """Drop the process-wide runtime. For tests that need a fresh bootstrap
    (e.g. after repointing ``OPENMIND_DATA_DIR``); not used in production."""
    global _default
    with _default_lock:
        _default = None


__all__ = ["OpenMindRuntime", "get_runtime", "reset_runtime"]
