"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later
enterprise features (formal Requirement Traceability, conflict resolution,
change-impact analysis, OCR) are NOT implemented; labelling the current build
``2.0.0`` would be a false claim. ``1.5.0-dev`` is the honest reading:
Phase 5 added the canonical Engineering Knowledge Graph — durable
evidence-bound Entities, Claims and Relations entered only through
deterministic projection, explicit manual creation or the explicit promotion
of a human-confirmed semantic Candidate, versioned by per-workspace Knowledge
Revisions, audited by immutable Human Decisions, searchable through a
separate graph vector projection, and exportable as the Knowledge Bundle
2.0 **Draft** — on top of the Phase 4 semantic plane, the Phase 3 document
plane, the Phase 2 Asset/Revision/Segment/Evidence foundation and the
Phase 1 tool-first runtime.

What Phase 5 pointedly does NOT do: promote anything automatically (review
and promotion stay separate acts), resolve conflicts (conflict candidates
stay candidates for Phase 6), or claim the generic graph path command is
formal traceability. Ordinary ingestion still makes zero model calls.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0``. The Knowledge Bundle has its
own draft version (``2.0.0-draft.1``) owned by :mod:`openmind.knowledge.bundle`.
"""
from __future__ import annotations

RUNTIME_VERSION = "1.5.0-dev"

__all__ = ["RUNTIME_VERSION"]
