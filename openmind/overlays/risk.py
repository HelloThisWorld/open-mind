"""Deterministic change-risk classification (spec §28).

Rule-based, never model-scored. Reads the overlay's persisted files, entity
deltas, trace impacts and conflict impacts and assigns an overall risk over the
ordered scale ``critical > high > medium > low > info`` with a SEPARATE
``unknown`` list. ``unknown`` is never folded into a known level and never
downgraded to ``low``.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..git.vocabularies import ChangeType, DeltaType, RiskLevel
from ..knowledge.vocabularies import EntityType
from . import store as ovl_store


class RiskClassifier:
    def __init__(self, workspace_id: str, overlay_id: str, *,
                 knowledge_service=None) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id
        self.knowledge = knowledge_service

    def classify(self) -> Dict[str, Any]:
        overall = RiskLevel.INFO
        reasons: List[Dict[str, Any]] = []
        high_objects: List[Dict[str, Any]] = []
        unknowns: List[Dict[str, Any]] = []

        files = ovl_store.list_files(self.overlay_id)
        deltas = ovl_store.list_entity_deltas(self.overlay_id)
        trace_impacts = ovl_store.list_trace_impacts(self.overlay_id)
        conflict_impacts = ovl_store.list_conflict_impacts(self.overlay_id)

        # -- files: unknown / low / info signals -------------------------------
        for f in files:
            if f["is_binary"]:
                unknowns.append({"kind": "binary-change", "path": f["new_path"] or f["old_path"]})
            elif f["is_lfs_pointer"]:
                unknowns.append({"kind": "lfs-object-unavailable", "path": f["new_path"]})
            elif f["is_submodule"]:
                unknowns.append({"kind": "submodule-change", "path": f["new_path"] or f["old_path"]})
            elif f["change_type"] == ChangeType.RENAMED and f["similarity"] == 100:
                overall = RiskLevel.max(overall, RiskLevel.LOW)
                reasons.append({"level": RiskLevel.LOW,
                                "reason": f"pure rename {f['old_path']} -> {f['new_path']}"})

        # -- entity deltas: removals ------------------------------------------
        for d in deltas:
            if d["delta_type"] == DeltaType.REMOVED and d["base_entity_id"]:
                et = self._entity_type(d["base_entity_id"])
                if et == EntityType.REQUIREMENT:
                    overall = RiskLevel.max(overall, RiskLevel.CRITICAL)
                    reasons.append({"level": RiskLevel.CRITICAL,
                                    "reason": f"requirement {d.get('canonical_key','')} removed"})
                    high_objects.append({"entityId": d["base_entity_id"],
                                         "reason": "requirement removed"})
                elif et in (EntityType.CODE_SYMBOL, EntityType.CODE_COMPONENT,
                            EntityType.INTERFACE, EntityType.TEST_CASE):
                    overall = RiskLevel.max(overall, RiskLevel.HIGH)
                    reasons.append({"level": RiskLevel.HIGH,
                                    "reason": f"{et} {d.get('canonical_key','')} removed"})
                    high_objects.append({"entityId": d["base_entity_id"],
                                         "reason": f"{et} removed"})
            elif d["delta_type"] == DeltaType.MODIFIED and d["base_entity_id"]:
                overall = RiskLevel.max(overall, RiskLevel.MEDIUM)

        # -- trace impacts ----------------------------------------------------
        for ti in trace_impacts:
            if ti["impact_type"] in ("trace-broken",):
                overall = RiskLevel.max(overall, RiskLevel.HIGH)
                reasons.append({"level": RiskLevel.HIGH,
                                "reason": f"trace broken for requirement {ti['root_requirement_id']}"})
                high_objects.append({"entityId": ti["root_requirement_id"],
                                     "reason": "verified trace broken"})
            elif ti["impact_type"] in ("implementation-changed",
                                       "interface-changed",
                                       "configuration-changed",
                                       "data-model-changed", "trace-weakened",
                                       "test-impacted"):
                overall = RiskLevel.max(overall, RiskLevel.MEDIUM)

        # -- conflict impacts -------------------------------------------------
        for ci in conflict_impacts:
            if ci["impact_type"] == "introduced":
                lvl = RiskLevel.CRITICAL if ci["severity"] == "critical" else RiskLevel.HIGH
                overall = RiskLevel.max(overall, lvl)
                reasons.append({"level": lvl,
                                "reason": f"conflict introduced on {ci['subject_key']}"})
                high_objects.append({"subjectKey": ci["subject_key"],
                                     "reason": "deterministic conflict introduced"})
            elif ci["impact_type"] == "persisting":
                overall = RiskLevel.max(overall, RiskLevel.MEDIUM)
            elif ci["impact_type"] == "unknown":
                unknowns.append({"kind": "conflict-unknown",
                                 "subjectKey": ci["subject_key"]})

        # A change with no engineering binding and no unknowns is at most low/info.
        if not reasons and not high_objects and not unknowns and files:
            overall = RiskLevel.max(overall, RiskLevel.LOW if any(
                not _doc_only(f) for f in files) else RiskLevel.INFO)

        reasons.sort(key=lambda r: (-RiskLevel.rank(r["level"]), r["reason"]))
        return {
            "overallRisk": overall,
            "reasons": reasons,
            "highestRiskObjects": high_objects,
            "unknowns": unknowns,
        }


    def _entity_type(self, entity_id: str) -> str:
        from ..knowledge import store as kstore
        e = kstore.get_entity(self.workspace_id, entity_id)
        return (e or {}).get("entity_type", "")


def _doc_only(f: Dict[str, Any]) -> bool:
    path = (f.get("new_path") or f.get("old_path") or "").lower()
    return path.endswith((".md", ".rst", ".txt", ".adoc"))


__all__ = ["RiskClassifier"]
