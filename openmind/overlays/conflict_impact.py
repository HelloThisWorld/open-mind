"""Projected Conflict impact (spec §27).

Runs the SAME deterministic comparable-fact logic the Phase 6 conflict
detectors use, but against ONLY the subjects an overlay actually changed — never
the whole virtual graph, and never a model. For each changed
configuration/interface subject it extracts the overlay's NEW value from the
changed after-segment content (via :func:`openmind.traceability.facts.
facts_from_statement`), compares it against the canonical documented-side facts
for the same subject, and classifies the result:

    introduced  — before agreed (or absent), after conflicts
    resolved    — before conflicted, after agrees
    persisting  — before and after both conflict
    unknown     — binary / unparseable / unsupported subject

Projected conflicts are written ONLY to the overlay's own table; canonical
``engineering_conflicts`` is never touched, and no conflict-governance action is
available until the change is merged and canonical sync confirms it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import content_store
from ..knowledge.vocabularies import EntityType
from ..traceability import facts as trace_facts
from ..traceability.models import ComparableFact
from ..git.vocabularies import DeltaType
from . import store as ovl_store


_ACTUAL_TYPES = {EntityType.CODE_COMPONENT, EntityType.CODE_SYMBOL,
                 EntityType.CONFIGURATION, EntityType.DATABASE_OBJECT,
                 EntityType.MESSAGE_TOPIC, EntityType.INTERFACE,
                 EntityType.DATA_MODEL}
_DOCUMENTED_TYPES = {EntityType.REQUIREMENT, EntityType.BUSINESS_RULE,
                     EntityType.DECISION, EntityType.CONSTRAINT,
                     EntityType.DESIGN}


class ConflictImpactAnalyzer:
    """Computes and persists projected conflict impact for one overlay."""

    def __init__(self, workspace_id: str, overlay_id: str) -> None:
        self.workspace_id = workspace_id
        self.overlay_id = overlay_id

    def analyze(self) -> Dict[str, Any]:
        from ..knowledge import store as kstore
        base_facts = trace_facts.collect_comparable_facts(self.workspace_id)
        # documented-side facts grouped by (subject, property)
        documented: Dict[tuple, List[ComparableFact]] = {}
        actual_by_entity: Dict[str, List[ComparableFact]] = {}
        entity_type_cache: Dict[str, str] = {}

        def etype(eid: str) -> str:
            if eid not in entity_type_cache:
                e = kstore.get_entity(self.workspace_id, eid)
                entity_type_cache[eid] = (e or {}).get("entity_type", "")
            return entity_type_cache[eid]

        for f in base_facts:
            t = etype(f.source_entity_id)
            if t in _DOCUMENTED_TYPES:
                documented.setdefault((f.subject_key, f.property), []).append(f)
            if t in _ACTUAL_TYPES:
                actual_by_entity.setdefault(f.source_entity_id, []).append(f)

        counts = {"introduced": 0, "resolved": 0, "persisting": 0, "unknown": 0}
        for d in ovl_store.list_entity_deltas(self.overlay_id):
            if d["delta_type"] != DeltaType.MODIFIED or not d["base_entity_id"]:
                continue
            base = kstore.get_entity(self.workspace_id, d["base_entity_id"])
            if not base or base["entity_type"] not in _ACTUAL_TYPES:
                continue
            self._analyze_subject(base, actual_by_entity, documented, counts)
        return {"conflict_impacts": sum(counts.values()), "by_type": counts}

    def _analyze_subject(self, base: Dict[str, Any],
                         actual_by_entity: Dict[str, List[ComparableFact]],
                         documented: Dict[tuple, List[ComparableFact]],
                         counts: Dict[str, int]) -> None:
        base_id = base["id"]
        old_facts = actual_by_entity.get(base_id, [])
        new_content = self._overlay_after_content(base_id)
        if new_content is None:
            # No parseable overlay content for this subject -> unknown.
            self._record(base, "unknown", None, None, counts,
                         reason="changed content not parseable for facts")
            return
        new_facts = self._extract_new_facts(old_facts, new_content)
        # Compare each (subject, property) that has a documented counterpart.
        seen_props = set()
        for of in old_facts:
            key = (of.subject_key, of.property)
            if key in seen_props:
                continue
            seen_props.add(key)
            docs = documented.get(key, [])
            if not docs:
                continue
            nf = new_facts.get(of.property)
            before_conflict = any(
                trace_facts.compare_facts(doc, of) == "different" for doc in docs)
            after_conflict = (nf is not None and any(
                trace_facts.compare_facts(doc, nf) == "different" for doc in docs))
            if nf is None:
                self._record(base, "unknown", of, None, counts,
                             reason="new value for property not extractable",
                             property_name=of.property)
                continue
            if after_conflict and not before_conflict:
                self._record(base, "introduced", of, nf, counts,
                             property_name=of.property, docs=docs)
            elif before_conflict and not after_conflict:
                self._record(base, "resolved", of, nf, counts,
                             property_name=of.property, docs=docs)
            elif before_conflict and after_conflict:
                self._record(base, "persisting", of, nf, counts,
                             property_name=of.property, docs=docs)

    def _overlay_after_content(self, base_entity_id: str) -> Optional[str]:
        """The after-side content of the segment(s) that changed this entity.
        Concatenated text of the file's changed after-segments."""
        # Find the file(s) whose after-segments changed this entity via its
        # canonical key match is expensive; instead read every changed after
        # segment's content blob for the overlay and join — bounded by segment
        # count. In practice conflict subjects are small config/interface files.
        texts: List[str] = []
        for s in ovl_store.list_segments(self.overlay_id, side="after"):
            if s.get("change_class") not in ("added", "modified"):
                continue
            h = s.get("content_blob_hash")
            if not h:
                continue
            try:
                data = content_store.get(self.workspace_id, h)
                texts.append(data.decode("utf-8", "replace"))
            except Exception:
                continue
        return "\n".join(texts) if texts else None

    def _extract_new_facts(self, old_facts: List[ComparableFact],
                           content: str) -> Dict[str, ComparableFact]:
        """Extract the overlay's new facts from changed content, keyed by
        property, restricted to the properties the subject already had."""
        wanted = {of.property for of in old_facts}
        subject = old_facts[0].subject_key if old_facts else ""
        out: Dict[str, ComparableFact] = {}
        for raw in trace_facts.facts_from_statement(content):
            prop = raw.get("property", "")
            if prop not in wanted:
                continue
            out[prop] = ComparableFact(
                subject_key=subject, property=prop,
                operator=raw.get("operator", "="), value=raw.get("value"),
                unit=raw.get("unit", ""), value_type=raw.get("value_type", ""),
                source_entity_id="overlay", raw_value=str(raw.get("raw_value", "")),
                raw_unit=raw.get("raw_unit", ""))
        return out

    def _record(self, base: Dict[str, Any], impact_type: str,
                old_fact: Optional[ComparableFact],
                new_fact: Optional[ComparableFact], counts: Dict[str, int], *,
                reason: str = "", property_name: str = "",
                docs: Optional[List[ComparableFact]] = None) -> None:
        counts[impact_type] = counts.get(impact_type, 0) + 1
        category = ("specification-code"
                    if base["entity_type"] in (EntityType.CONFIGURATION,
                                               EntityType.CODE_SYMBOL,
                                               EntityType.CODE_COMPONENT)
                    else "interface-schema")
        severity = "high" if impact_type == "introduced" else (
            "info" if impact_type == "resolved" else "medium")
        if impact_type == "unknown":
            severity = "info"
        before = {"value": _shown(old_fact)} if old_fact else {}
        after = {"value": _shown(new_fact)} if new_fact else {}
        if docs:
            before["requirement"] = _shown(docs[0])
            after["requirement"] = _shown(docs[0])
        detail = reason or (
            f"{property_name} on {base.get('canonical_key','')}: "
            f"code now {_shown(new_fact)} vs requirement {_shown(docs[0]) if docs else '?'}")
        ovl_store.add_conflict_impact(
            self.overlay_id, subject_key=base.get("canonical_key", ""),
            impact_type=impact_type, category=category, severity=severity,
            before=before, after=after, reason=detail)


def _shown(fact: Optional[ComparableFact]) -> str:
    if fact is None:
        return ""
    if fact.raw_value:
        return f"{fact.raw_value}{(' ' + fact.raw_unit) if fact.raw_unit else ''}"
    return f"{fact.value}{fact.unit or ''}"


__all__ = ["ConflictImpactAnalyzer"]
