"""Traceability coverage: reproducible metrics over one refresh's per-root
trace results.

Every ratio is reported as ``{count, numerator, denominator, percentage}``
and a zero denominator produces ``percentage: null`` — never a fabricated
0 or 100. Coverage status is policy-driven (thresholds live in the policy's
rules); a workspace with zero requirements is ``unknown``, not perfect.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import TraceabilityPolicy
from .vocabularies import (CoverageStatus, GapSeverity, TracePathStatus,
                           TraceStage)


def _ratio(numerator: int, denominator: int) -> Dict[str, Any]:
    return {
        "count": numerator,
        "numerator": numerator,
        "denominator": denominator,
        "percentage": (round(100.0 * numerator / denominator, 2)
                       if denominator > 0 else None),
    }


def compute_coverage(policy: TraceabilityPolicy,
                     root_results: List[Dict[str, Any]],
                     root_gaps: List[List[Dict[str, Any]]],
                     *, orphan_code_count: int = 0,
                     orphan_test_count: int = 0,
                     open_critical_gaps: int = 0) -> Dict[str, Any]:
    """Aggregate one refresh. ``root_results`` are engine ``trace_root``
    outputs; ``root_gaps`` the per-root detected gap lists (same order)."""
    total = len(root_results)

    def stage_reached(result: Dict[str, Any], *stages: str) -> bool:
        coverage = result["stage_coverage"]
        return any(coverage.get(s, {}).get("reached") for s in stages)

    with_design = sum(1 for r in root_results
                      if stage_reached(r, TraceStage.DESIGN,
                                       TraceStage.WORKFLOW))
    with_interface = sum(1 for r in root_results
                         if stage_reached(r, TraceStage.INTERFACE,
                                          TraceStage.DATA))
    with_implementation = sum(
        1 for r in root_results
        if stage_reached(r, TraceStage.IMPLEMENTATION,
                         TraceStage.CONFIGURATION))
    with_full_implementation = sum(
        1 for r in root_results
        if stage_reached(r, TraceStage.IMPLEMENTATION,
                         TraceStage.CONFIGURATION)
        and not r.get("partial_implementation_only"))
    with_tests = sum(1 for r in root_results
                     if stage_reached(r, TraceStage.VERIFICATION))
    with_test_results = sum(
        1 for r in root_results
        if stage_reached(r, TraceStage.TEST_RESULT, TraceStage.EVIDENCE))

    def has_current_evidence(result: Dict[str, Any]) -> bool:
        """The evidence-bearing stage is reached by at least one VERIFIED
        path (a partial path there means evidence missing; stale means
        stale)."""
        evidence_stages = [s.name for s in policy.stages
                           if s.requires_evidence]
        if not evidence_stages:
            return False
        for path in result["paths"]:
            if path["status"] != TracePathStatus.VERIFIED:
                continue
            if path["steps"] and \
                    path["steps"][-1]["stage"] in evidence_stages:
                return True
        return False

    with_current_evidence = sum(1 for r in root_results
                                if has_current_evidence(r))

    def classify(result: Dict[str, Any]) -> str:
        """fully / partially / untraced, against required policy stages."""
        coverage = result["stage_coverage"]
        required = [s for s, c in coverage.items() if c["required"]]
        reached = [s for s in required if coverage[s]["reached"]]
        if len(reached) == len(required) and required:
            return "fully"
        if len(reached) > 1:        # more than the root stage itself
            return "partially"
        return "untraced"

    classes = [classify(r) for r in root_results]
    fully = sum(1 for c in classes if c == "fully")
    partially = sum(1 for c in classes if c == "partially")
    untraced = sum(1 for c in classes if c == "untraced")

    stale_traces = sum(
        1 for r in root_results
        if any(p["status"] == TracePathStatus.STALE for p in r["paths"]))
    ambiguous_traces = sum(
        1 for r in root_results
        if any(p["status"] == TracePathStatus.AMBIGUOUS
               for p in r["paths"]) or (r.get("ambiguities")))

    # -- stage-level coverage ------------------------------------------------
    stage_metrics: Dict[str, Any] = {}
    for stage in policy.stages:
        reached_count = sum(
            1 for r in root_results
            if r["stage_coverage"].get(stage.name, {}).get("reached"))
        stage_metrics[stage.name] = {
            "required": stage.required,
            **_ratio(reached_count, total),
        }

    # -- authority-level coverage -------------------------------------------
    by_authority: Dict[str, Dict[str, int]] = {}
    for result, cls in zip(root_results, classes):
        authority = result["root"]["authority_status"]
        bucket = by_authority.setdefault(
            authority, {"total": 0, "fully": 0, "partially": 0,
                        "untraced": 0})
        bucket["total"] += 1
        bucket[cls] += 1

    # -- entity-type coverage ------------------------------------------------
    by_entity_type: Dict[str, Dict[str, int]] = {}
    for result, cls in zip(root_results, classes):
        entity_type = result["root"]["entity_type"]
        bucket = by_entity_type.setdefault(
            entity_type, {"total": 0, "fully": 0, "partially": 0,
                          "untraced": 0})
        bucket["total"] += 1
        bucket[cls] += 1

    # -- open gap totals -----------------------------------------------------
    gap_totals: Dict[str, int] = {}
    for gap_list in root_gaps:
        for gap in gap_list:
            gap_totals[gap["gap_type"]] = \
                gap_totals.get(gap["gap_type"], 0) + 1

    # -- policy-driven status ------------------------------------------------
    status = CoverageStatus.UNKNOWN
    fully_pct = (100.0 * fully / total) if total else None
    healthy_min = policy.rules.coverage_healthy_minimum_pct
    warning_min = policy.rules.coverage_warning_minimum_pct
    if total == 0 or fully_pct is None:
        status = CoverageStatus.UNKNOWN
    elif healthy_min is None and warning_min is None:
        status = CoverageStatus.UNKNOWN
    elif healthy_min is not None and fully_pct >= healthy_min and \
            open_critical_gaps == 0:
        status = CoverageStatus.HEALTHY
    elif warning_min is not None and fully_pct >= warning_min:
        status = CoverageStatus.WARNING
    else:
        status = CoverageStatus.CRITICAL

    return {
        "requirements": {
            "total": total,
            "with_design": _ratio(with_design, total),
            "with_interface": _ratio(with_interface, total),
            "with_implementation": _ratio(with_implementation, total),
            "with_full_implementation":
                _ratio(with_full_implementation, total),
            "with_tests": _ratio(with_tests, total),
            "with_test_results": _ratio(with_test_results, total),
            "with_current_evidence":
                _ratio(with_current_evidence, total),
            "fully_traced": _ratio(fully, total),
            "partially_traced": _ratio(partially, total),
            "untraced": _ratio(untraced, total),
            "stale_traces": _ratio(stale_traces, total),
            "ambiguous_traces": _ratio(ambiguous_traces, total),
        },
        "stages": stage_metrics,
        "by_authority": by_authority,
        "by_entity_type": by_entity_type,
        "orphans": {
            "code": orphan_code_count,
            "tests": orphan_test_count,
        },
        "open_gaps_by_type": dict(sorted(gap_totals.items())),
        "open_critical_gaps": open_critical_gaps,
        "status": status,
        "thresholds": {
            "healthy_minimum_pct": healthy_min,
            "warning_minimum_pct": warning_min,
        },
    }


__all__ = ["compute_coverage"]
