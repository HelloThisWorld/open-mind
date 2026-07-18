"""Application services: use-case orchestration, shared by every adapter.

Nothing in this package imports FastAPI, argparse or the MCP SDK. Services take
plain arguments, return plain dictionaries or dataclasses from
:mod:`openmind.domain.types`, and raise :mod:`openmind.domain.errors`. Each
adapter maps those to its own transport.

Access them through :class:`~openmind.runtime.OpenMindRuntime`, which wires the
container to a bootstrapped database.

LAZY BY DESIGN
--------------
The convenience re-exports below resolve on first attribute access (PEP 562)
instead of being imported eagerly. Importing ONE service must not drag in all of
them: :class:`~openmind.services.export_service.ExportService` is standalone and
offline — artifact export needs neither the vector store nor the database — but
:class:`~openmind.services.workspace_service.WorkspaceService` imports
:mod:`openmind.vectorstore`, which needs numpy and chromadb.

With eager imports here, ``openmind export`` would have required the full
dependency set on a machine that only wanted the deterministic exporter, and the
dependency-free artifact-contract CI job would fail. ``ServiceContainer`` still
imports every service directly, which is correct: a bootstrapped runtime does
need all of them.
"""
from typing import Any

__all__ = ["ExportService", "HealthService", "IngestService", "JobService",
           "ServiceContainer", "WorkspaceService"]

_EXPORTS = {
    "ExportService": ".export_service",
    "HealthService": ".health_service",
    "IngestService": ".ingest_service",
    "JobService": ".job_service",
    "ServiceContainer": ".service_container",
    "WorkspaceService": ".workspace_service",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__() -> list:
    return sorted(__all__)
