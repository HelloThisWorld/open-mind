"""Runtime diagnostics — the machine-readable answer behind ``openmind doctor``
and ``GET /api/health``.

SEVERITY POLICY
---------------
Only ``error`` fails ``doctor``. ``warn`` means degraded-but-usable, and the
most important instance of that is the local model: OpenMind ingests, extracts,
exports artifacts and answers glossary/structure queries with NO model at all.
An absent ``llama-server`` binary or an unloaded model is therefore a warning,
never a failure — a missing optional local model must not break diagnostics for
someone who never asked for a model-dependent operation. Callers that DO need a
model check :meth:`model_ready` explicitly.

Every check is non-destructive and independently guarded: one probe raising
never prevents the others from reporting. A probe that cannot answer says so
(``error``/``warn`` with the reason) rather than reporting a healthy default.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List

from .. import config
from .. import db as db_module
from ..domain.types import (STATUS_ERROR, STATUS_OK, STATUS_WARN, HealthCheck,
                            HealthReport)
from ..version import RUNTIME_VERSION


class HealthService:
    """Non-destructive runtime diagnostics."""

    def __init__(self, db: Any = None) -> None:
        self._db: Any = db if db is not None else db_module

    # -- the report ---------------------------------------------------------
    def report(self) -> HealthReport:
        """Run every diagnostic and aggregate. Never raises."""
        checks: List[HealthCheck] = [
            self._runtime_version(),
            self._data_dir(),
            self._database(),
            self._migrations(),
            self._project_dir_permissions(),
            self._vectorstore_backend(),
            self._embedding_backend(),
            self._mcp_dependency(),
            self._model_config(),
            self._model_readiness(),
            self._network_policy(),
        ]
        return HealthReport(version=RUNTIME_VERSION, checks=checks)

    def summary(self) -> Dict[str, Any]:
        """The report as a plain dict, for ``--json`` and the HTTP adapter."""
        return self.report().as_dict()

    def model_ready(self) -> bool:
        """Whether a local model is actually reachable right now.

        Separate from :meth:`report` on purpose: this is the gate for a
        model-dependent operation, not a health signal.
        """
        try:
            from .. import llm_client
            return bool(llm_client.is_ready())
        except Exception:
            return False

    # -- individual probes --------------------------------------------------
    @staticmethod
    def _guard(name: str, probe: Callable[[], HealthCheck]) -> HealthCheck:
        """Run one probe; convert an unexpected failure into an error check.

        This is the one place a broad except is right: a diagnostic tool that
        crashes because a diagnostic crashed is useless. The exception type and
        message are reported, not swallowed.
        """
        try:
            return probe()
        except Exception as exc:
            return HealthCheck(name, STATUS_ERROR,
                               f"probe failed: {type(exc).__name__}: {exc}")

    def _runtime_version(self) -> HealthCheck:
        return HealthCheck("runtime_version", STATUS_OK, RUNTIME_VERSION,
                           {"version": RUNTIME_VERSION})

    def _data_dir(self) -> HealthCheck:
        def probe() -> HealthCheck:
            path = Path(config.DATA_DIR)
            data = {"path": str(path)}
            if not path.exists():
                return HealthCheck("data_dir", STATUS_ERROR,
                                   f"data directory does not exist: {path}", data)
            if not os.access(path, os.W_OK):
                return HealthCheck("data_dir", STATUS_ERROR,
                                   f"data directory is not writable: {path}", data)
            usage = shutil.disk_usage(str(path))
            data["free_mb"] = round(usage.free / (1024 * 1024))
            if usage.free < 100 * 1024 * 1024:
                return HealthCheck("data_dir", STATUS_WARN,
                                   f"less than 100 MB free at {path}", data)
            return HealthCheck("data_dir", STATUS_OK, str(path), data)

        return self._guard("data_dir", probe)

    def _database(self) -> HealthCheck:
        def probe() -> HealthCheck:
            path = Path(config.DB_PATH)
            data: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
            if path.exists():
                data["size_bytes"] = path.stat().st_size
            conn = sqlite3.connect(str(path))
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                data["tables"] = sorted(tables)
                journal = conn.execute("PRAGMA journal_mode").fetchone()
                if journal:
                    data["journal_mode"] = journal[0]
            finally:
                conn.close()
            missing = {"projects", "jobs", "file_index"} - set(data["tables"])
            if missing:
                return HealthCheck(
                    "database", STATUS_ERROR,
                    "core tables missing: " + ", ".join(sorted(missing)), data)
            return HealthCheck("database", STATUS_OK,
                               f"{len(data['tables'])} tables", data)

        return self._guard("database", probe)

    def _migrations(self) -> HealthCheck:
        def probe() -> HealthCheck:
            status = self._db.migration_status()
            version = status.get("version", 0)
            data = dict(status)
            if not version:
                return HealthCheck("migrations", STATUS_ERROR,
                                   "database has no applied migrations", data)
            unknown = status.get("unknown_applied") or []
            if unknown:
                return HealthCheck(
                    "migrations", STATUS_WARN,
                    f"schema version {version}, but this build does not know "
                    f"migration(s) {unknown} — the database was written by a "
                    f"newer OpenMind", data)
            return HealthCheck("migrations", STATUS_OK,
                               f"schema version {version}", data)

        return self._guard("migrations", probe)

    def _project_dir_permissions(self) -> HealthCheck:
        def probe() -> HealthCheck:
            path = Path(config.DATA_DIR)
            data = {"path": str(path)}
            if not path.exists():
                return HealthCheck("project_dirs", STATUS_ERROR,
                                   f"data directory does not exist: {path}", data)
            # Prove writability by actually writing; an ACL can make os.access
            # optimistic on Windows.
            try:
                handle, probe_path = tempfile.mkstemp(prefix=".om_doctor_",
                                                      dir=str(path))
                os.close(handle)
                os.unlink(probe_path)
            except OSError as exc:
                return HealthCheck("project_dirs", STATUS_ERROR,
                                   f"cannot create files under {path}: {exc}", data)
            data["writable"] = True
            return HealthCheck("project_dirs", STATUS_OK,
                               f"writable: {path}", data)

        return self._guard("project_dirs", probe)

    def _vectorstore_backend(self) -> HealthCheck:
        def probe() -> HealthCheck:
            from .. import vectorstore
            name = vectorstore.backend_name()
            data = {"backend": name}
            if not name:
                return HealthCheck("vectorstore", STATUS_ERROR,
                                   "no vector-store backend available", data)
            # The numpy fallback is fully functional, just slower and in-process.
            if "numpy" in name.lower():
                return HealthCheck(
                    "vectorstore", STATUS_WARN,
                    f"{name} (in-process fallback; chromadb is not available)", data)
            return HealthCheck("vectorstore", STATUS_OK, name, data)

        return self._guard("vectorstore", probe)

    def _embedding_backend(self) -> HealthCheck:
        def probe() -> HealthCheck:
            from .. import embeddings
            name = embeddings.backend_name()
            data = {"backend": name, "dim": embeddings.dim(),
                    "offline": os.environ.get("OPENMIND_EMBED_OFFLINE") == "1"}
            if not name:
                return HealthCheck("embeddings", STATUS_ERROR,
                                   "no embedding backend available", data)
            # The hashing embedder keeps ingestion working offline, but recall
            # is materially worse than a real model — say so plainly.
            if "hash" in name.lower():
                return HealthCheck(
                    "embeddings", STATUS_WARN,
                    f"{name} (deterministic fallback; semantic recall is reduced)",
                    data)
            return HealthCheck("embeddings", STATUS_OK, name, data)

        return self._guard("embeddings", probe)

    def _mcp_dependency(self) -> HealthCheck:
        def probe() -> HealthCheck:
            import importlib.util
            spec = importlib.util.find_spec("mcp")
            if spec is None:
                return HealthCheck(
                    "mcp", STATUS_WARN,
                    "the 'mcp' package is not installed; "
                    "'openmind mcp serve' will not start (pip install mcp)",
                    {"available": False})
            return HealthCheck("mcp", STATUS_OK, "the 'mcp' package is available",
                               {"available": True})

        return self._guard("mcp", probe)

    def _model_config(self) -> HealthCheck:
        def probe() -> HealthCheck:
            cfg = self._db.get_model_config()
            model_path = str(cfg.get("model_path") or "")
            server = str(cfg.get("llama_server_path") or "")
            data = {"configured": bool(model_path),
                    "host": cfg.get("host"), "port": cfg.get("port"),
                    "llama_server_path": server}
            if not model_path:
                return HealthCheck(
                    "model_config", STATUS_WARN,
                    "no local model is configured; ingestion, extraction and "
                    "artifact export do not need one", data)
            if not Path(model_path).exists():
                data["model_path"] = model_path
                return HealthCheck(
                    "model_config", STATUS_WARN,
                    f"configured model file is missing: {model_path}", data)
            data["model_path"] = model_path
            return HealthCheck("model_config", STATUS_OK,
                               f"model configured: {Path(model_path).name}", data)

        return self._guard("model_config", probe)

    def _model_readiness(self) -> HealthCheck:
        def probe() -> HealthCheck:
            from .. import llm_client
            base = llm_client.base_url()
            data = {"base_url": base, "loopback": llm_client.is_local_endpoint()}
            if not llm_client.is_local_endpoint():
                # A non-loopback model endpoint would send project content off
                # the machine. That IS an error.
                return HealthCheck(
                    "model_server", STATUS_ERROR,
                    f"model endpoint is not loopback: {base}", data)
            ready = self.model_ready()
            data["ready"] = ready
            if not ready:
                return HealthCheck(
                    "model_server", STATUS_WARN,
                    "no local model server is responding; model-dependent "
                    "features (Ask) are unavailable, everything else works", data)
            return HealthCheck("model_server", STATUS_OK, f"ready at {base}", data)

        return self._guard("model_server", probe)

    def _network_policy(self) -> HealthCheck:
        def probe() -> HealthCheck:
            from .. import netguard
            data = {
                "enrich_egress": config.ENRICH_EGRESS,
                "sourcelink_egress": config.SOURCELINK_EGRESS,
                "allowed_hosts": sorted(config.ALLOWED_HOSTS),
                "outbound_calls_logged": len(netguard.get_log(1000)),
                "audit_log": str(config.OUTBOUND_LOG),
            }
            enabled = [n for n, on in (("enrichment", config.ENRICH_EGRESS),
                                       ("source-link", config.SOURCELINK_EGRESS))
                       if on]
            detail = ("loopback only" if not enabled
                      else "loopback + audited egress: " + ", ".join(enabled))
            return HealthCheck("network_policy", STATUS_OK, detail, data)

        return self._guard("network_policy", probe)


__all__ = ["HealthService"]
