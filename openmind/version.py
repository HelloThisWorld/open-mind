"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later
enterprise features (connectors, OCR, cloud runtime, plugin packaging) are
NOT implemented; labelling the current build ``2.0.0`` would be a false
claim. ``1.7.0-dev`` is the honest reading: Phase 7 added the isolated Git
Overlay plane — a read-only Git command boundary (no mutation, no remote),
local repository discovery, coherent canonical Git baselines, branch / PR /
commit-range / working-tree / multi-repository change-set overlays,
before/after immutable Evidence, a virtual Graph View (Base graph + overlay
delta), evidence-cited Requirement / Test / Trace / Gap / Conflict impact,
deterministic rule-based change risk, byte-deterministic Change Impact
Reports and a separate Change Impact Packet — all of which reference the
canonical Base Workspace but never write a single canonical row — on top of
the Phase 6 formal Traceability and governed Conflicts, the Phase 5 canonical
Engineering Knowledge Graph, the Phase 4 semantic plane, the Phase 3 document
plane, the Phase 2 Asset/Revision/Segment/Evidence foundation and the Phase 1
tool-first runtime.

What Phase 7 pointedly does NOT do: mutate Git, contact a Git remote, fetch/
merge/comment on a PR, run semantic providers on changed files, execute tests,
auto-promote projected overlay relations, or write a projected gap/conflict
into a canonical table. Ordinary ingestion, and every overlay build and impact
analysis, still make zero model calls.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0``. The Knowledge Bundle has its
own draft version (``2.0.0-draft.2``) owned by :mod:`openmind.knowledge.bundle`.
"""
from __future__ import annotations

RUNTIME_VERSION = "1.7.0-dev"

__all__ = ["RUNTIME_VERSION"]
