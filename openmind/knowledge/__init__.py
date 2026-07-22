"""The canonical Engineering Knowledge Graph (OpenMind v2 Phase 5).

Everything in this package operates on the canonical graph tables created by
migration v0006: Entities, Claims, Relations, aliases, bindings, Evidence
joins, Human Decisions, Knowledge Revisions, promotion records and the
deterministic projection state.

Boundaries this package holds:

* graph truth enters ONLY through deterministic projection, explicit manual
  creation, or explicit Candidate/Relation-Candidate promotion — never from a
  review action, never from ingestion, never automatically;
* every active Claim carries Evidence; every Relation carries provenance;
* every governance write records a Human Decision and one Knowledge Revision;
* Phase 4 Candidates stay in their own tables — a promoted Candidate remains
  queryable and the graph stores only its id as provenance;
* nothing here calls a semantic provider; the projector is model-free.

Import cost: importing this package pulls no provider SDK, no vector store
and no database connection — modules bootstrap lazily like the rest of the
runtime.
"""
from __future__ import annotations

#: Versions the deterministic projection rules. Bump when a projection rule
#: changes so `sync` knows stored state was produced by older rules.
GRAPH_PROJECTOR_VERSION = "1"

#: Recorded on every promotion (knowledge_promotions.policy_version) so a
#: later phase can tell which eligibility rules admitted a promotion.
PROMOTION_POLICY_VERSION = "1"

__all__ = ["GRAPH_PROJECTOR_VERSION", "PROMOTION_POLICY_VERSION"]
