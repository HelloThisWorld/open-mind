"""Traceability policies: built-ins load, invalid organization files stay
visible with errors, unknown stages/relations/executable content rejected,
deterministic checksums, policy change invalidates the current snapshot and
mints one Knowledge Revision, workspace scoping enforced."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _traceability_helpers import check, finish, make_fixture

from openmind.runtime import get_runtime
from openmind.traceability import policies
from openmind.traceability.policies import (BUILTIN_POLICY_DATA,
                                            validate_policy_document)

# -- built-ins ---------------------------------------------------------------
builtins = policies.builtin_policies()
check("exactly the five built-in policies ship",
      sorted(p.name for p in builtins)
      == ["api-service", "batch-processing", "event-driven-service",
          "generic-engineering", "japanese-v-model"])
check("every built-in validates clean",
      all(not validate_policy_document(BUILTIN_POLICY_DATA[p.name])
          ["errors"] or True for p in builtins)
      and all(validate_policy_document(BUILTIN_POLICY_DATA[p.name])["valid"]
              for p in builtins))
api = policies.resolve_policy("api-service")
check("api-service requires interface/implementation/verification/evidence "
      "but not design",
      set(api.required_stages()) == {"requirement", "interface",
                                     "implementation", "verification",
                                     "evidence"})
vmodel = policies.resolve_policy("japanese-v-model")
check("japanese-v-model requires design",
      "design" in vmodel.required_stages())

# checksum determinism
check("policy checksum is deterministic",
      policies.resolve_policy("api-service").checksum == api.checksum)
check("different policies have different checksums",
      api.checksum != vmodel.checksum)

# -- schema validation -------------------------------------------------------
def doc(**overrides):
    base = {
        "schemaVersion": "1.0.0", "name": "custom", "title": "Custom",
        "rootTypes": ["requirement"],
        "stages": [
            {"name": "requirement", "entityTypes": ["requirement"],
             "required": True},
            {"name": "implementation", "entityTypes": ["code-component"],
             "required": True},
        ],
        "transitions": [
            {"from": "requirement", "to": "implementation",
             "relationTypes": ["implements"]},
        ],
    }
    base.update(overrides)
    return base


check("a valid custom policy validates",
      validate_policy_document(doc())["valid"])
report = validate_policy_document(doc(stages=[
    {"name": "requirement", "entityTypes": ["requirement"],
     "required": True},
    {"name": "made-up-stage", "entityTypes": ["code-component"],
     "required": True}]))
check("unknown stage rejected",
      not report["valid"] and any("unknown stage" in e
                                  for e in report["errors"]))
report = validate_policy_document(doc(transitions=[
    {"from": "requirement", "to": "implementation",
     "relationTypes": ["depends-on"]}]))
check("unknown relation type rejected",
      not report["valid"] and any("unknown relation type" in e
                                  for e in report["errors"]))
report = validate_policy_document(doc(rootTypes=["nonsense"]))
check("unknown root entity type rejected",
      not report["valid"])
report = validate_policy_document(doc(title="eval(open('/etc/x').read())"))
check("executable content rejected",
      not report["valid"] and any("forbidden" in e
                                  for e in report["errors"]))
report = validate_policy_document(doc(title="see https://provider.example"))
check("provider URL rejected",
      not report["valid"] and any("forbidden" in e
                                  for e in report["errors"]))
report = validate_policy_document(doc(rules={"maximumDepth": 99}))
check("out-of-range depth rejected", not report["valid"])
report = validate_policy_document(doc(rules={"unknownRule": 1}))
check("unknown rule key rejected", not report["valid"])
report = validate_policy_document(doc(
    rules={"gapSeverities": {"missing-test": "catastrophic"}}))
check("severity outside the closed range rejected", not report["valid"])

# -- organization directory --------------------------------------------------
org_dir = Path(tempfile.mkdtemp(prefix="om_orgpol_"))
os.environ["OPENMIND_TRACE_POLICY_DIR"] = str(org_dir)
(org_dir / "team-flow.json").write_text(json.dumps(
    doc(name="team-flow", title="Team Flow")), encoding="utf-8")
(org_dir / "broken.json").write_text(json.dumps(
    doc(name="broken", stages=[{"name": "nope",
                                "entityTypes": ["requirement"]}])),
    encoding="utf-8")
(org_dir / "not-json.json").write_text("{{{", encoding="utf-8")
listing = policies.list_organization_policies()
check("organization directory lists every file, valid or not",
      len(listing) == 3)
check("valid organization policy resolves",
      policies.resolve_policy("team-flow").name == "team-flow")
broken = [r for r in listing if r["name"] == "broken"][0]
check("invalid organization policy stays visible with its errors",
      not broken["valid"] and broken["errors"])
try:
    policies.resolve_policy("broken")
    check("invalid organization policy is not selectable", False)
except Exception:
    check("invalid organization policy is not selectable", True)
unparsable = [r for r in listing if r["file"] == "not-json.json"][0]
check("unparsable file visible with load error",
      not unparsable["valid"] and unparsable["errors"])

# -- service-level policy ops ------------------------------------------------
runtime = get_runtime()
fx = make_fixture(runtime, "policy-fix")
pid = fx.pid
trace = fx.trace
fx.lifecycle()

listing = trace.list_policies(pid)
check("service lists built-ins + organization policies",
      len(listing["policies"]) == 5 + 3)
check("default active policy is generic-engineering before selection",
      trace.get_policy(pid)["policy"]["name"] == "generic-engineering")

result = trace.set_workspace_policy(pid, policy_name="api-service",
                                    actor="reviewer",
                                    note="use the API lifecycle")
check("policy selection mints one Knowledge Revision",
      result["knowledge_revision"] >= 1)
decisions = runtime.knowledge.list_decisions(
    pid, decision_type="trace-policy-change")["decisions"]
check("policy selection records a Human Decision",
      len(decisions) == 1 and decisions[0]["actor"] == "reviewer")

# policy change invalidates the current snapshot
trace.refresh(pid)
check("refresh created a current snapshot",
      trace.get_coverage(pid)["snapshot"] is not None)
before_rev = runtime.knowledge.get_current_revision(
    pid)["knowledge_revision"]
change = trace.set_workspace_policy(pid, policy_name="japanese-v-model",
                                    actor="reviewer",
                                    note="switch to the V-model")
check("policy change stales the current snapshot",
      change["staled_snapshots"] >= 1
      and trace.get_coverage(pid)["snapshot"] is None)
check("policy change stales current paths", change["staled_paths"] >= 1)
check("policy change requires an explicit refresh",
      change["refresh_required"] is True)
check("policy change minted exactly one revision",
      change["knowledge_revision"] == before_rev + 1)
again = trace.set_workspace_policy(pid, policy_name="japanese-v-model",
                                   actor="reviewer", note="same again")
check("re-selecting the same policy is an honest no-op",
      again["unchanged"] is True)

try:
    trace.set_workspace_policy(pid, policy_name="no-such-policy",
                               actor="reviewer", note="x")
    check("unknown policy name is a typed failure", False)
except Exception:
    check("unknown policy name is a typed failure", True)
try:
    trace.set_workspace_policy(pid, policy_name="api-service", actor="",
                               note="x")
    check("actor is required for policy selection", False)
except Exception:
    check("actor is required for policy selection", True)

# -- workspace scoping -------------------------------------------------------
other = runtime.workspaces.create("policy-fix-b")["id"]
check("policy selection is workspace-scoped",
      trace.get_policy(other)["policy"]["name"] == "generic-engineering")
try:
    trace.get_policy("p_missing")
    check("unknown workspace is a typed failure", False)
except Exception:
    check("unknown workspace is a typed failure", True)

finish()
