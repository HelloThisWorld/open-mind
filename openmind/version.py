"""The single runtime version constant.

Everything that reports a version — the CLI ``--version``, ``doctor``, the
FastAPI app + ``GET /api/health``, and the ``.openmind`` artifact manifest's
``generator.version`` — reads it from here, so the value can never drift
between surfaces.

This is a PRE-v2 development version on purpose. OpenMind v2's later enterprise
knowledge features (canonical Claim/Relation tables, the engineering Knowledge
Graph, requirement traceability, OCR) are NOT implemented; labelling the
current build ``2.0.0`` would be a false claim. ``1.4.0-dev`` is the honest
reading: Phase 4 added the policy-governed semantic reasoning plane — a
provider-neutral SPI (local OpenAI-compatible, OpenAI, Anthropic, Azure
OpenAI, mock) behind an audited egress path, evidence-bound candidate
extraction with local verification, a resumable budget-bounded analysis
pipeline with a local result cache, candidate staleness, and Adaptive Project
Lenses — on top of the Phase 3 document plane, the Phase 2
Asset/Revision/Segment/Evidence foundation and the Phase 1 tool-first runtime.

What Phase 4 pointedly does NOT do is turn any of those candidates into
canonical truth: entity/claim/relation tables, candidate promotion and the
Knowledge Graph are Phase 5. Cloud reasoning exists but is disabled until a
workspace explicitly opts in; ordinary ingestion still makes zero model calls.

The artifact ``schemaVersion`` is deliberately NOT derived from this constant —
it is a separate, frozen integration contract owned by
:mod:`openmind.artifacts` and remains ``1.1.0`` (neither the Asset model, the
document model nor any semantic candidate is exported).
"""
from __future__ import annotations

RUNTIME_VERSION = "1.4.0-dev"

__all__ = ["RUNTIME_VERSION"]
