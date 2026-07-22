"""OpenMind v2 Phase 6 — formal Requirement Traceability and governed
Conflict management over the Phase 5 canonical Engineering Knowledge Graph.

This package READS the canonical graph (:mod:`openmind.knowledge`); it never
becomes a second graph store. Trace paths, gaps, coverage snapshots and
canonical Conflicts reference canonical objects by id and are stamped with
the Knowledge Revision, Traceability Policy checksum and Trace Engine
version they were computed against — the three coordinates that make
incremental recomputation and honest staleness possible.

Nothing in this package calls a semantic provider. Trace building, gap
detection and conflict detection are deterministic; anything a model might
propose stays in the Phase 4 candidate plane until a human promotes it.
"""
from __future__ import annotations

#: Version of the deterministic trace engine. Bumping it invalidates every
#: active trace snapshot (a new engine may classify paths differently, so
#: reusing old paths would misreport what THIS engine believes).
TRACE_ENGINE_VERSION = "1.0.0"

#: Version of the deterministic conflict-detector framework (individual
#: detectors carry their own versions on top).
CONFLICT_FRAMEWORK_VERSION = "1.0.0"

__all__ = ["TRACE_ENGINE_VERSION", "CONFLICT_FRAMEWORK_VERSION"]
