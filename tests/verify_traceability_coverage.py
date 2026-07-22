"""Coverage: correct numerators/denominators, honest null percentage on a
zero denominator, full/partial/untraced counts, stage-level coverage, stale
paths excluded from current coverage, policy-driven status, historical
snapshots preserved."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "cov-fix")
pid = fx.pid
trace = fx.trace
trace.set_workspace_policy(pid, policy_name="api-service", actor="fx",
                           note="api lifecycle")

# -- zero requirements: unknown status, null percentages ---------------------
trace.refresh(pid, force=True)
empty = trace.get_coverage(pid)
metrics = empty["snapshot"]["metrics"]
check("zero requirements -> status unknown", metrics["status"] == "unknown")
check("zero denominator -> percentage null (never a false zero)",
      metrics["requirements"]["fully_traced"]["percentage"] is None
      and metrics["requirements"]["fully_traced"]["denominator"] == 0)

# -- three requirements: full / partial / untraced ---------------------------
full = fx.lifecycle(key_suffix="")                      # complete
partial = fx.lifecycle(with_test=False, key_suffix="-P")  # no test
bare = fx.entity("requirement", "requirement:REQ-NC-090", "REQ-NC-090")
fx.claim(bare, "normative-statement", "Nothing links here yet.")

trace.refresh(pid)
coverage = trace.get_coverage(pid)
metrics = coverage["snapshot"]["metrics"]
requirements = metrics["requirements"]
check("total counts every eligible root", requirements["total"] == 3)
check("with_implementation is 2/3",
      requirements["with_implementation"]["numerator"] == 2
      and requirements["with_implementation"]["denominator"] == 3)
check("with_implementation percentage is exact",
      abs(requirements["with_implementation"]["percentage"] - 66.67)
      < 0.01)
check("with_tests is 1/3",
      requirements["with_tests"]["numerator"] == 1)
check("with_test_results is 1/3",
      requirements["with_test_results"]["numerator"] == 1)
check("with_current_evidence is 1/3",
      requirements["with_current_evidence"]["numerator"] == 1)
check("fully traced count", requirements["fully_traced"]["count"] == 1)
check("partially traced count",
      requirements["partially_traced"]["count"] == 1)
check("untraced count", requirements["untraced"]["count"] == 1)

# -- stage-level coverage -----------------------------------------------------
stages = metrics["stages"]
check("stage-level coverage reports every policy stage",
      set(stages) == {"requirement", "design", "interface",
                      "implementation", "verification", "evidence"})
check("stage-level implementation reach is 2/3",
      stages["implementation"]["numerator"] == 2
      and stages["implementation"]["denominator"] == 3)
check("optional design stage is marked not-required",
      stages["design"]["required"] is False)

# -- entity-type and authority levels ----------------------------------------
check("entity-type coverage present",
      metrics["by_entity_type"].get("requirement", {}).get("total") == 3)
check("authority coverage present",
      sum(b["total"] for b in metrics["by_authority"].values()) == 3)

# -- policy-driven status -----------------------------------------------------
check("1/3 fully traced under default thresholds is critical",
      metrics["status"] == "critical")
check("thresholds are reported from the policy",
      metrics["thresholds"]["healthy_minimum_pct"] == 90.0)

# -- historical snapshots preserved ------------------------------------------
snapshots_before = trace.list_coverage_snapshots(pid)["count"]
# make the partial requirement fully traced: add test + result
late_test = fx.entity("test-case", "test-case:NC-T-91", "Late test")
fx.claim(late_test, "test-expectation", "Late verification works.")
fx.relation(late_test, partial["code"], "verifies")
late_result = fx.entity("test-result", "test-result:NC-T-91-r1",
                        "Late run")
fx.claim(late_result, "test-expectation", "Late run passed.")
fx.relation(late_result, late_test, "evidenced-by")
trace.refresh(pid)
coverage2 = trace.get_coverage(pid)
requirements2 = coverage2["snapshot"]["metrics"]["requirements"]
check("coverage is reproducible after the graph change",
      requirements2["fully_traced"]["count"] == 2)
snapshots_after = trace.list_coverage_snapshots(pid)
check("historical snapshots preserved (never overwritten)",
      snapshots_after["count"] == snapshots_before + 1)
older = snapshots_after["snapshots"][-1]
check("old snapshot remains queryable with its own metrics",
      older["id"] != coverage2["snapshot"]["id"])

# -- stale paths excluded from current coverage -------------------------------
# Reject the late test's verifies relation; refresh recomputes with the
# edge excluded, so the root drops out of full coverage.
from openmind.knowledge import store as kg  # noqa: E402
verifies = [r for r in kg.list_relations(pid,
                                         entity_id=late_test["id"],
                                         relation_type="verifies")
            if r["lifecycle_status"] == "active"][0]
runtime.knowledge.reject_relation(pid, relation_id=verifies["id"],
                                  actor="fx", note="wrong link")
trace.refresh(pid)
coverage3 = trace.get_coverage(pid)
requirements3 = coverage3["snapshot"]["metrics"]["requirements"]
check("current coverage uses only current paths (stale drop out)",
      requirements3["fully_traced"]["count"] == 1)
check("snapshot history keeps the pre-stale numbers",
      trace.list_coverage_snapshots(pid)["count"]
      == snapshots_after["count"] + 1)

finish()
