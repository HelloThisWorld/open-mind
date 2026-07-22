"""Orphans: the four explicit queries; orphan code/tests classified as
untraced (never invalid); orphan documents = promoted claims but no
relations; workspace scoping."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "orphan-fix")
pid = fx.pid
trace = fx.trace
trace.set_workspace_policy(pid, policy_name="api-service", actor="fx",
                           note="api lifecycle")
objects = fx.lifecycle()

# an untraced utility component + an untraced test + an untraced requirement
utility = fx.entity("code-component", "code-component:string-utils",
                    "StringUtils")
fx.claim(utility, "behavior", "General string helpers.")
loose_test = fx.entity("test-case", "test-case:OPS-CHECK-01",
                       "Manual operational check")
fx.claim(loose_test, "test-expectation", "Operations ran the check.")
bare_req = fx.entity("requirement", "requirement:REQ-NC-200", "REQ-NC-200")
fx.claim(bare_req, "normative-statement", "Not yet implemented.")

# -- orphan requirements ------------------------------------------------------
orphan_requirements = trace.find_orphan_requirements(pid)
check("orphan requirement found",
      any(o["entity_id"] == bare_req["id"]
          for o in orphan_requirements["orphans"]))
check("traced requirement is NOT an orphan",
      not any(o["entity_id"] == objects["requirement"]["id"]
              for o in orphan_requirements["orphans"]))
check("orphan requirement reports its reached stages",
      all("reached_stages" in o for o in orphan_requirements["orphans"]))

# -- orphan code --------------------------------------------------------------
orphan_code = trace.find_orphan_code(pid)
check("utility code reported as orphan",
      any(o["entity_id"] == utility["id"]
          for o in orphan_code["orphans"]))
check("orphan code is classified untraced, never invalid",
      all(o["classification"] == "untraced" and o["orphan"] is True
          for o in orphan_code["orphans"]))
check("traced code is NOT an orphan",
      not any(o["entity_id"] == objects["code"]["id"]
              for o in orphan_code["orphans"]))
check("the query reports how many entities it examined",
      orphan_code["entities_examined"] >= 2)

# -- orphan tests -------------------------------------------------------------
orphan_tests = trace.find_orphan_tests(pid)
check("loose test reported as orphan (untraced)",
      any(o["entity_id"] == loose_test["id"]
          for o in orphan_tests["orphans"]))
check("verified test is NOT an orphan",
      not any(o["entity_id"] == objects["test"]["id"]
              for o in orphan_tests["orphans"]))

# -- orphan documents ---------------------------------------------------------
# A document entity carrying a PROMOTED claim but zero relations. Write the
# claim through the graph transaction with semantic-promotion origin (the
# production writer) — manual claims would not qualify.
from openmind.knowledge import store as kg  # noqa: E402
from openmind.knowledge.identity import statement_hash  # noqa: E402
from openmind.knowledge.vocabularies import RevisionAction  # noqa: E402

with kg.graph_transaction(pid, action=RevisionAction.CANDIDATE_PROMOTION,
                          actor="fixture",
                          summary="fixture promoted doc claim") as tx:
    doc_entity = tx.insert_entity(
        entity_type="document", canonical_key="document:asset:a_fix",
        display_name="Loose specification", origin="semantic-promotion")
    tx.insert_claim(
        entity_id=doc_entity["id"], claim_type="classification",
        statement="This document is a requirements specification.",
        normalized_statement_hash=statement_hash(
            "This document is a requirements specification."),
        origin="semantic-promotion",
        evidence=[{"evidence_id": fx.evidence_id, "role": "primary",
                   "quote": "", "quote_hash": ""}])

orphan_documents = trace.find_orphan_documents(pid)
check("document with promoted claims but no relations is an orphan",
      any(o["entity_id"] == doc_entity["id"]
          for o in orphan_documents["orphans"]))
check("orphan document reports its promoted-claim count",
      all(o["promoted_claims"] >= 1
          for o in orphan_documents["orphans"]))

# linking the document removes it from the orphan set
fx.relation(doc_entity, objects["requirement"], "possibly-related")
orphan_documents2 = trace.find_orphan_documents(pid)
check("a related document is no longer an orphan",
      not any(o["entity_id"] == doc_entity["id"]
              for o in orphan_documents2["orphans"]))

# -- refresh records orphan gaps ---------------------------------------------
trace.refresh(pid)
gaps = trace.list_gaps(pid, status="open")
check("orphan-code gap recorded on refresh",
      any(g["gap_type"] == "orphan-code"
          and g["root_entity_id"] == utility["id"]
          for g in gaps["gaps"]))
check("orphan-test gap recorded on refresh",
      any(g["gap_type"] == "orphan-test"
          and g["root_entity_id"] == loose_test["id"]
          for g in gaps["gaps"]))
check("orphan-code severity is info (not a defect)",
      all(g["severity"] == "info" for g in gaps["gaps"]
          if g["gap_type"] == "orphan-code"))

# -- workspace scoping --------------------------------------------------------
other = runtime.workspaces.create("orphan-fix-b")["id"]
check("orphan queries are workspace-scoped",
      trace.find_orphan_code(other)["count"] == 0)

finish()
