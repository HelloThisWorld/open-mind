"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later enterprise
knowledge features (Claim/Relation tables, the engineering Knowledge Graph,
cloud providers, requirement traceability, OCR, semantic document analysis) are
NOT implemented; labelling the current build ``2.0.0`` would be a false claim.
``1.3.0-dev`` is the honest reading: Phase 3 added the deterministic
document-ingestion plane — a parser SPI, document Assets with block-level
Evidence, a separate document vector index, document search and deterministic
candidate association — on top of the Phase 2 Asset/Revision/Segment/Evidence
foundation and the Phase 1 tool-first runtime, while the shipped artifact
contract is still schema 1.1.x.

What Phase 3 pointedly does NOT do is extract Requirements, Business Rules or
Design Decisions from those documents, or infer any relationship between them
and the code. It ingests and normalizes; the semantics are Phase 4.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0`` (neither the Asset model nor the
document model is exported yet).
"""
from __future__ import annotations

RUNTIME_VERSION = "1.3.0-dev"

__all__ = ["RUNTIME_VERSION"]
