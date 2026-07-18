"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's enterprise
knowledge features (Asset/Revision/Claim tables, the engineering Knowledge
Graph, cloud providers, traceability) are NOT implemented; labelling the
current build ``2.0.0`` would be a false claim. ``1.1.0-dev`` is the honest
reading: the shipped artifact contract is schema 1.1.x, and this is
development work on top of it.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts`.
"""
from __future__ import annotations

RUNTIME_VERSION = "1.1.0-dev"

__all__ = ["RUNTIME_VERSION"]
