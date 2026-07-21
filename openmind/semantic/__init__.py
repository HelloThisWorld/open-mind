"""The semantic reasoning plane (OpenMind v2 Phase 4).

This package is the ONLY place where a generative model is asked to propose
engineering knowledge. Everything it produces is a **candidate** — schema- and
evidence-verified locally, workspace-scoped, review-gated — and never becomes
canonical truth here (candidate promotion and the Engineering Knowledge Graph
are Phase 5).

Boundaries, kept deliberately explicit:

* ``providers/``  — the provider SPI, the machine-local profile registry and
  the concrete adapters (local OpenAI-compatible, OpenAI, Anthropic, Azure
  OpenAI, mock). SDK imports are lazy; a missing SDK fails only its own kind.
* ``transport``   — the audited egress path. No provider adapter may construct
  its own HTTP client; everything goes through here and is logged.
* ``policy``      — workspace data classification + remote permission. Checked
  BEFORE any project content is serialized into a request.
* ``tasks`` / ``schemas`` / ``prompts`` — the closed, versioned task registry,
  the strict structured-output schemas and the injection-hardened prompt
  builder.
* ``verifier``    — local evidence verification. Model output that cites
  nothing verifiable is rejected, whatever its JSON looks like.
* ``planner`` / ``runner`` — deterministic planning (zero provider calls) and
  the resumable, budget-bounded execution pipeline.
* ``store``       — repositories over the v0005 tables (same shared SQLite
  connection and lock as :mod:`openmind.db`).
* ``lenses/``     — Adaptive Project Lenses: the built-in Template projection,
  organization lens files, and model-induced provisional lenses.

Nothing in this package runs during ordinary ingestion. ``openmind ingest``,
``asset add`` and ``document add`` never import a provider SDK and never make
a model call; semantic analysis is a separate, explicit, policy-gated verb.
"""
from __future__ import annotations

#: Version of the LOCAL semantic machinery (planner + verifier + confidence
#: derivation). Part of every cache key and stored on every run/candidate, so
#: a change in local logic is a cache miss and is visible in provenance.
ANALYZER_VERSION = "1.0.0"

__all__ = ["ANALYZER_VERSION"]
