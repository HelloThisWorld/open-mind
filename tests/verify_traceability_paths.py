"""Trace paths: complete lifecycle verified; optional design absent under
the API policy; required design missing under the V-model; possibly-related
never satisfies implements; calls alone never proves implementation; stale
relations create stale paths; the inferred-relation cap is enforced;
authority is disclosed; ordering is deterministic; caps are disclosed;
generic graph paths never appear as formal traces; and the reverse Code and
Test traces resolve honestly."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime

runtime = get_runtime()
fx = make_fixture(runtime)
pid = fx.pid
trace = fx.trace
trace.set_workspace_policy(pid, policy_name="api-service", actor="fx",
                           note="api lifecycle")
objects = fx.lifecycle(with_design=False)
req, iface = objects["requirement"], objects["interface"]
code, test, result = objects["code"], objects["test"], objects["result"]

# -- complete lifecycle -------------------------------------------------------
full = trace.trace_requirement(pid, req["id"])
kinds = {p["path_kind"]: p for p in full["paths"]}
check("requirement-to-interface path found",
      "requirement-to-interface" in kinds)
check("requirement-to-implementation path found",
      "requirement-to-implementation" in kinds)
check("requirement-to-test path found", "requirement-to-test" in kinds)
evidence_path = kinds.get("requirement-to-evidence")
check("complete Requirement -> Interface -> Code -> Test -> Evidence path "
      "verified",
      evidence_path is not None
      and evidence_path["status"] == "verified"
      and evidence_path["completeness"] == 1.0
      and evidence_path["confidence"] == "high")
check("optional design stage absent under API policy creates NO gap",
      not any(g["gap_type"] == "missing-design" for g in full["gaps"]))
check("stage coverage reports every policy stage",
      set(full["stage_coverage"])
      == {"requirement", "design", "interface", "implementation",
          "verification", "evidence"})
check("traversal caps disclosed",
      full["limits"]["maximum_depth"] >= 1
      and "max_chains_per_root" in full["limits"])
check("result carries policy checksum + knowledge revision",
      full["policy"]["checksum"] and full["knowledge_revision"] >= 1)

# -- V-model: required design missing ----------------------------------------
trace.set_workspace_policy(pid, policy_name="japanese-v-model", actor="fx",
                           note="v-model")
vmodel_result = trace.trace_requirement(pid, req["id"])
check("required design missing under V-model creates missing-design gap",
      any(g["gap_type"] == "missing-design"
          for g in vmodel_result["gaps"]))
trace.set_workspace_policy(pid, policy_name="api-service", actor="fx",
                           note="back to api")

# -- possibly-related never satisfies implements ------------------------------
config = fx.entity("configuration", "configuration:namecheck.timeout",
                   "namecheck.timeout")
fx.claim(config, "constraint", "namecheck.timeout=2000")
fx.relation(config, iface, "possibly-related")
result2 = trace.trace_requirement(pid, req["id"])
impl_targets = {p["target_entity_id"] for p in result2["paths"]
                if p["path_kind"] == "requirement-to-implementation"}
check("possibly-related does not satisfy implements",
      config["id"] not in impl_targets)
check("the rejected possibly-related edge is disclosed",
      any(e["relation_type"] == "possibly-related"
          for e in result2["rejected_edges"]))

# -- calls alone never proves implementation ----------------------------------
helper = fx.entity("code-component", "code-component:helper", "Helper")
fx.claim(helper, "behavior", "Helper normalizes text.")
fx.relation(code, helper, "calls")
result3 = trace.trace_requirement(pid, req["id"])
impl_targets3 = {p["target_entity_id"] for p in result3["paths"]
                 if p["path_kind"] == "requirement-to-implementation"}
check("calls alone does not satisfy Requirement implementation",
      helper["id"] not in impl_targets3)

# -- generic graph path is NOT a formal trace ---------------------------------
generic = runtime.knowledge.find_path(pid, req["id"], helper["id"])
check("generic graph path DOES reach the helper (reachability)",
      generic["outcome"] == "found")
check("the same chain never appears as a formal trace",
      helper["id"] not in impl_targets3)

# -- inferred relation count limit --------------------------------------------
# api-service allows at most 2 inferred relations; build a chain with 3.
r2 = fx.entity("requirement", "requirement:REQ-NC-030", "REQ-NC-030")
fx.claim(r2, "normative-statement", "Checks must be recorded.")
i2 = fx.entity("interface", "interface:POST:/record", "Record API")
fx.claim(i2, "interface-contract", "POST /record stores the record.")
c2 = fx.entity("code-component", "code-component:recorder", "Recorder")
fx.claim(c2, "behavior", "Stores records.")
t2 = fx.entity("test-case", "test-case:REC-T-01", "Recorder test")
fx.claim(t2, "test-expectation", "Recording works.")
fx.inferred_relation(i2, r2, "refines")
fx.inferred_relation(c2, i2, "implements")
fx.inferred_relation(t2, c2, "verifies")
result4 = trace.trace_requirement(pid, r2["id"])
reached = {p["path_kind"] for p in result4["paths"]}
check("two inferred hops are allowed (interface + implementation reached)",
      "requirement-to-implementation" in reached)
check("the third inferred hop exceeds the policy cap (no test path)",
      "requirement-to-test" not in reached)
impl4 = [p for p in result4["paths"]
         if p["path_kind"] == "requirement-to-implementation"][0]
check("a complete-but-inferred path is medium confidence at best",
      impl4["confidence"] in ("medium", "low"))

# -- authority disclosed ------------------------------------------------------
runtime.knowledge.set_authority(pid, kind="entity", object_id=req["id"],
                                authority="informational", actor="fx",
                                note="informational root")
result5 = trace.trace_requirement(pid, req["id"])
check("authority disclosed on the root",
      result5["root"]["authority_status"] == "informational")
evidence5 = [p for p in result5["paths"]
             if p["path_kind"] == "requirement-to-evidence"][0]
check("informational root path is not high confidence",
      evidence5["confidence"] != "high")
runtime.knowledge.set_authority(pid, kind="entity", object_id=req["id"],
                                authority="authoritative", actor="fx",
                                note="restore")

# -- deterministic ordering ---------------------------------------------------
alt = fx.entity("code-component", "code-component:namecheck-alt",
                "NameCheckAlt")
fx.claim(alt, "behavior", "Alternate implementation.")
fx.relation(alt, iface, "partially-implements")
result6 = trace.trace_requirement(pid, req["id"])
impl_paths = [p for p in result6["paths"]
              if p["path_kind"] == "requirement-to-implementation"]
check("alternate implementation paths are NOT hidden",
      len(impl_paths) >= 2)
result6b = trace.trace_requirement(pid, req["id"])
check("multiple paths ordered deterministically (stable across calls)",
      [p["path_hash"] for p in result6["paths"]]
      == [p["path_hash"] for p in result6b["paths"]])
check("higher completeness orders first within a kind",
      impl_paths[0]["completeness"] >= impl_paths[-1]["completeness"])

# -- stale relation creates a stale path -------------------------------------
# Withdraw the evidenced-by relation's endpoint? No — stale the RELATION:
# supersede the verifies relation via governance and confirm the path goes
# stale on the persisted plane after refresh + reconcile.
trace.refresh(pid)
from openmind.traceability import store as trace_store  # noqa: E402
from openmind.knowledge import store as kg  # noqa: E402
verifies = [r for r in kg.list_relations(pid, entity_id=test["id"],
                                         relation_type="verifies")
            if r["lifecycle_status"] == "active"][0]
runtime.knowledge.reject_relation(pid, relation_id=verifies["id"],
                                  actor="fx", note="wrong link")
rec = trace.reconcile_staleness(pid)
check("trace staleness reconciliation marks affected paths",
      rec["trace"]["paths_staled"] >= 1)
stale_paths = trace_store.list_paths(pid, root_entity_id=req["id"],
                                     status="stale", current_only=False)
check("stale relation creates stale persisted path",
      any(p["stale_at"] for p in stale_paths))
live = trace.trace_requirement(pid, req["id"])
check("live trace no longer reaches the test through the rejected edge",
      "requirement-to-test" not in {p["path_kind"] for p in live["paths"]})
runtime.knowledge.restore_relation(pid, relation_id=verifies["id"],
                                   actor="fx", note="restore")

# -- reverse code trace -------------------------------------------------------
code_trace = trace.trace_code(pid, code["id"])
check("code resolves its upstream Requirement",
      any(r["entity_id"] == req["id"]
          for r in code_trace["requirements"]))
check("code sees its downstream test",
      any(t["entity_id"] == test["id"] for t in code_trace["tests"]))
check("code sees its downstream test result",
      any(t["entity_id"] == result["id"]
          for t in code_trace["test_results"]))
helper_trace = trace.trace_code(pid, helper["id"])
check("utility code is orphan/untraced, never 'invalid'",
      helper_trace["orphan"]
      and helper_trace["classification"] == "untraced"
      and "invalid" not in str(helper_trace.get("classification")))
try:
    trace.trace_code(pid, req["id"])
    check("a requirement is not a code-trace subject", False)
except Exception:
    check("a requirement is not a code-trace subject", True)

# -- reverse test trace -------------------------------------------------------
test_trace = trace.trace_test(pid, test["id"])
check("test resolves its Requirement",
      any(r["entity_id"] == req["id"]
          for r in test_trace["requirements"]))
check("test resolves its implementation target",
      any(s["entity_id"] == code["id"]
          for s in test_trace["implementation_targets"]))
check("test carries supporting evidence",
      len(test_trace["supporting_evidence"]) >= 1)
loose = fx.entity("test-case", "test-case:LOOSE-01", "Loose test")
fx.claim(loose, "test-expectation", "An operational check.")
loose_trace = trace.trace_test(pid, loose["id"])
check("a test with no requirement path is untraced, not invalid",
      loose_trace["untraced"] and loose_trace["orphan"])

# -- root eligibility ---------------------------------------------------------
bare = fx.entity("requirement", "requirement:REQ-BARE", "Bare requirement")
try:
    trace.trace_requirement(pid, bare["id"])
    check("a claim-less root is ineligible (typed)", False)
except Exception:
    check("a claim-less root is ineligible (typed)", True)
try:
    trace.trace_requirement(pid, code["id"])
    check("a non-root entity type is ineligible (typed)", False)
except Exception:
    check("a non-root entity type is ineligible (typed)", True)

finish()
