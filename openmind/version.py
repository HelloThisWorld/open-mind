"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later enterprise
knowledge features (Claim/Relation tables, the engineering Knowledge Graph,
cloud providers, requirement traceability, document parsing) are NOT
implemented; labelling the current build ``2.0.0`` would be a false claim.
``1.2.0-dev`` is the honest reading: Phase 2 added the canonical
Asset/Revision/Segment/Evidence foundation and its immutable content store on
top of the Phase 1 tool-first runtime, while the shipped artifact contract is
still schema 1.1.x.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0`` (the Asset model is not exported
yet).
"""
from __future__ import annotations

RUNTIME_VERSION = "1.2.0-dev"

__all__ = ["RUNTIME_VERSION"]
