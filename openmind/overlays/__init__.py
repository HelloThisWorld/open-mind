"""Isolated Git Overlay plane (OpenMind v2 Phase 7).

An overlay is a derived, provisional projection of a branch / PR / commit-range
/ working-tree change onto a coherent snapshot of the canonical Base Workspace.
It references Base objects by id but never mutates a canonical table, and every
overlay-derived result carries the Base coordinates that make it reproducible.

Boundaries kept explicit (spec §7):

* overlay persistence   -> store.py
* build orchestration   -> builder.py
* segmentation/evidence -> segmentation.py, evidence.py
* composed search       -> search.py
* virtual graph         -> graph_view.py, projector.py
* impact analysis       -> trace_impact.py, conflict_impact.py, risk.py
* report generation     -> report.py
* reconciliation        -> reconcile.py
* service surface       -> service.py
"""
from __future__ import annotations

#: Version of the overlay builder. Participates in an overlay's source hash so
#: a change to how overlays are built invalidates cached overlay work.
OVERLAY_BUILDER_VERSION = "1"

#: The Change Impact Report / Packet draft schema (spec §29, §41).
CHANGE_IMPACT_SCHEMA_VERSION = "1.0.0-draft.1"

__all__ = ["OVERLAY_BUILDER_VERSION", "CHANGE_IMPACT_SCHEMA_VERSION"]
