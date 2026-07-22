"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later
enterprise features (Git branch/PR change-impact overlays, connectors, OCR)
are NOT implemented; labelling the current build ``2.0.0`` would be a false
claim. ``1.6.0-dev`` is the honest reading: Phase 6 added formal
Requirement Traceability and governed Conflict management — policy-driven,
evidence-verified trace paths over the Phase 5 canonical graph (a generic
graph path is NOT a trace), coverage snapshots with honest zero-denominator
handling, first-class gaps and orphans, incremental recomputation tied to
Knowledge Revision × policy checksum × trace engine version, deterministic
comparable-fact conflict detection (never free-prose contradiction
guessing), explicit Conflict Candidate promotion, and a fully audited
conflict lifecycle — on top of the Phase 5 canonical Engineering Knowledge
Graph, the Phase 4 semantic plane, the Phase 3 document plane, the Phase 2
Asset/Revision/Segment/Evidence foundation and the Phase 1 tool-first
runtime.

What Phase 6 pointedly does NOT do: rebuild traces implicitly (refresh is
explicit or locally scheduled, model-free), resolve conflicts automatically,
rewrite canonical Claims during resolution, promote Conflict Candidates
automatically, or infer authority. Ordinary ingestion still makes zero
model calls.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0``. The Knowledge Bundle has its
own draft version (``2.0.0-draft.2``) owned by :mod:`openmind.knowledge.bundle`.
"""
from __future__ import annotations

RUNTIME_VERSION = "1.6.0-dev"

__all__ = ["RUNTIME_VERSION"]
