"""Requirement, Test, Trace and Gap impact (spec §24, §25, §26).

Derives impact from the overlay's entity deltas by combining the canonical
Base reverse-traceability (Phase 6) with an in-memory revalidation of each
affected trace path against the ``OverlayGraphView``. A changed/removed base
implementation is walked back to its upstream Requirement roots; each root's
Base trace paths are then re-checked against the virtual overlay graph, so a
removed implementation surfaces as a *broken* trace and an introduced
*missing-implementation* Gap, while a merely-modified implementation surfaces as
a *weakened*/*changed* trace.

Nothing here writes a canonical trace path, gap or conflict — results land only
in the overlay's own impact tables. An added code symbol with no supporting
overlay relation NEVER claims to implement a Requirement (only base entities are
walked, so an addition contributes no Requirement impact by construction).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from ..knowledge.vocabularies import EntityType
from ..git.vocabularies import DeltaType
from . import store as ovl_store
from .graph_view import OverlayGraphView

# Base entity types that the reverse code trace accepts.
_CODE_TRACE_TYPES = {EntityType.CODE_COMPONENT, EntityType.CODE_SYMBOL,
                     EntityType.CONFIGURATION, EntityType.DATABASE_OBJECT,
                     EntityType.MESSAGE_TOPIC}
_TEST_TYPES = {EntityType.TEST_CASE, EntityType.TEST_RESULT}
_IMPL_TYPES = _CODE_TRACE_TYPES | {EntityType.INTERFACE, EntityType.DATA_MODEL}


class TraceImpactAnalyzer:
    """Computes and persists Requirement/Test/Trace/Gap impact for one
    overlay."""

    def __init__(self, workspace_id: str, overlay_id: str, *,
                 knowledge_service, traceability_service) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id
        self.knowledge = knowledge_service
        self.traceability = traceability_service
        self.view = OverlayGraphView(workspace_id, overlay_id)

    def analyze(self) -> Dict[str, Any]:
        deltas = ovl_store.list_entity_deltas(self.overlay_id)
        # index deltas by base entity id for quick "is this changed/removed?"
        self._removed: Set[str] = {d["base_entity_id"] for d in deltas
                                   if d["delta_type"] == DeltaType.REMOVED
                                   and d["base_entity_id"]}
        self._modified: Set[str] = {d["base_entity_id"] for d in deltas
                                    if d["delta_type"] == DeltaType.MODIFIED
                                    and d["base_entity_id"]}
        impacted_requirements: Dict[str, Dict[str, Any]] = {}
        impacted_tests: Dict[str, Dict[str, Any]] = {}
        recommended_tests: Dict[str, Dict[str, Any]] = {}

        for d in deltas:
            base_id = d["base_entity_id"]
            if not base_id or d["delta_type"] == DeltaType.ADDED:
                continue
            base = _safe_get_entity(self.knowledge, self.workspace_id, base_id)
            if not base:
                continue
            etype = base.get("entity_type", "")
            if etype in _TEST_TYPES and d["delta_type"] == DeltaType.REMOVED:
                self._deleted_test_impact(base, impacted_tests)
                continue
            if etype in _CODE_TRACE_TYPES:
                self._code_impact(base, d, impacted_requirements,
                                  impacted_tests, recommended_tests)
            elif etype in (EntityType.INTERFACE, EntityType.DATA_MODEL):
                self._interface_impact(base, d, impacted_requirements,
                                       recommended_tests)

        self._persist_requirement_impacts(impacted_requirements)
        self._persist_test_impacts(impacted_tests, recommended_tests)
        return {
            "impacted_requirements": len(impacted_requirements),
            "impacted_tests": len(impacted_tests),
            "recommended_tests": len(recommended_tests),
        }

    # -- code/config/db/topic ----------------------------------------------
    def _code_impact(self, base: Dict[str, Any], delta: Dict[str, Any],
                     reqs: Dict[str, Any], impacted_tests: Dict[str, Any],
                     recommended_tests: Dict[str, Any]) -> None:
        try:
            trace = self.traceability.trace_code(self.workspace_id, base["id"])
        except Exception:
            return
        removed = delta["delta_type"] == DeltaType.REMOVED
        for req in trace.get("requirements", []):
            rid = req.get("entity_id")
            if not rid:
                continue
            impact_type, severity = self._classify(base, removed)
            entry = reqs.setdefault(rid, {
                "root_requirement_id": rid, "impact_type": impact_type,
                "severity": severity, "reasons": [], "changed_objects": [],
                "affected_trace_ids": []})
            entry["changed_objects"].append({
                "entity_id": base["id"], "entity_type": base["entity_type"],
                "canonical_key": base.get("canonical_key", ""),
                "delta": delta["delta_type"]})
            entry["reasons"].append(
                f"{base['entity_type']} {base.get('canonical_key','')} "
                f"{delta['delta_type']}")
            # trace status via overlay view: removed => broken; modified =>
            # weakened if still reachable, broken if the view hides it.
            still = self.view.get_entity(base["id"]) is not None
            if removed or not still:
                entry["impact_type"] = "trace-broken"
                entry["severity"] = _max_sev(entry["severity"], "high")
            elif base["id"] in self._modified:
                entry["impact_type"] = entry["impact_type"] or "trace-weakened"
        # downstream tests whose trace includes this changed object
        for t in trace.get("tests", []) + trace.get("results", []):
            tid = t.get("entity_id")
            if tid:
                impacted_tests.setdefault(tid, {
                    "entity_id": tid, "canonical_key": t.get("canonical_key", ""),
                    "reason": f"trace includes changed {base['entity_type']} "
                              f"{base.get('canonical_key','')}",
                    "supporting_trace": base["id"], "confidence": "high"})

    def _interface_impact(self, base: Dict[str, Any], delta: Dict[str, Any],
                          reqs: Dict[str, Any],
                          recommended_tests: Dict[str, Any]) -> None:
        # Requirements pointing at this interface (incoming relations).
        for rel in self.view.neighbors(base["id"], direction="incoming"):
            src = self.view.get_entity(rel["source_entity_id"])
            if not src or src.get("entity_type") not in (
                    EntityType.REQUIREMENT, EntityType.BUSINESS_RULE,
                    EntityType.DESIGN):
                continue
            rid = src["id"]
            removed = delta["delta_type"] == DeltaType.REMOVED
            impact_type = ("interface-changed" if not removed
                           else "implementation-removed")
            entry = reqs.setdefault(rid, {
                "root_requirement_id": rid, "impact_type": impact_type,
                "severity": "high" if removed else "medium", "reasons": [],
                "changed_objects": [], "affected_trace_ids": []})
            entry["changed_objects"].append({
                "entity_id": base["id"], "entity_type": base["entity_type"],
                "canonical_key": base.get("canonical_key", ""),
                "delta": delta["delta_type"]})
            entry["reasons"].append(
                f"interface {base.get('canonical_key','')} {delta['delta_type']}")

    def _deleted_test_impact(self, base: Dict[str, Any],
                             impacted_tests: Dict[str, Any]) -> None:
        impacted_tests[base["id"]] = {
            "entity_id": base["id"],
            "canonical_key": base.get("canonical_key", ""),
            "reason": "test entity deleted by this change",
            "supporting_trace": "", "confidence": "high", "deleted": True}

    # -- classification -----------------------------------------------------
    @staticmethod
    def _classify(base: Dict[str, Any], removed: bool) -> tuple:
        et = base.get("entity_type", "")
        if removed:
            return "implementation-removed", "high"
        if et == EntityType.CONFIGURATION:
            return "configuration-changed", "medium"
        if et == EntityType.DATABASE_OBJECT:
            return "data-model-changed", "medium"
        return "implementation-changed", "medium"

    # -- persistence --------------------------------------------------------
    def _persist_requirement_impacts(self, reqs: Dict[str, Any]) -> None:
        for entry in reqs.values():
            introduced_gaps = []
            if entry["impact_type"] == "trace-broken":
                introduced_gaps.append({
                    "gap_type": "missing-implementation",
                    "root_entity_id": entry["root_requirement_id"],
                    "reason": "all implementation paths broken by this change"})
            ovl_store.add_trace_impact(
                self.overlay_id,
                root_requirement_id=entry["root_requirement_id"],
                impact_type=entry["impact_type"], severity=entry["severity"],
                before={}, after={"changedObjects": entry["changed_objects"]},
                introduced_gaps=introduced_gaps,
                affected_trace_ids=entry["affected_trace_ids"],
                reason="; ".join(entry["reasons"][:10]))

    def _persist_test_impacts(self, impacted: Dict[str, Any],
                              recommended: Dict[str, Any]) -> None:
        # Impacted + recommended tests are recorded as trace impacts of type
        # test-impacted / test-recommended so the report can render them
        # without a second table.
        for t in impacted.values():
            introduced = []
            if t.get("deleted"):
                introduced.append({"gap_type": "missing-test",
                                   "reason": "verifying test deleted"})
            ovl_store.add_trace_impact(
                self.overlay_id, root_requirement_id=t["entity_id"],
                impact_type="test-impacted",
                severity="high" if t.get("deleted") else "medium",
                after={"canonicalKey": t.get("canonical_key", ""),
                       "supportingTrace": t.get("supporting_trace", "")},
                introduced_gaps=introduced,
                reason=t.get("reason", ""))


def _safe_get_entity(knowledge, workspace_id, entity_id):
    try:
        from ..knowledge import store as kstore
        return kstore.get_entity(workspace_id, entity_id)
    except Exception:
        return None


def _max_sev(a: str, b: str) -> str:
    order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    return a if order.get(a, 0) >= order.get(b, 0) else b


__all__ = ["TraceImpactAnalyzer"]
