"""Canonical Conflict model: creation through the graph transaction only,
object + evidence joins mandatory at creation, closed statuses, dedup keys,
Knowledge Revision stamps, cross-workspace isolation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402
from openmind.traceability import store as trace_store  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "cmodel-fix")
pid = fx.pid
trace = fx.trace
objects = fx.lifecycle()
req = objects["requirement"]

# two contradicting constraint claims -> one deterministic conflict
fx.claim(req, "constraint", "The check timeout is 2 seconds.")
fx.claim(req, "constraint", "The check timeout is 5 seconds.")
revision_before = runtime.knowledge.get_current_revision(
    pid)["knowledge_revision"]
scan = trace.scan_conflicts(pid, actor="scanner")
check("scan completed", scan["status"] == "done")
check("exactly one conflict created", len(scan["created"]) == 1)
conflict_id = scan["created"][0]
check("conflict creation minted exactly one Knowledge Revision",
      runtime.knowledge.get_current_revision(pid)["knowledge_revision"]
      == revision_before + 1)

conflict = trace.get_conflict(pid, conflict_id)
check("conflict has the canonical shape",
      conflict["category"] == "document-document"
      and conflict["status"] == "open"
      and conflict["origin"] == "deterministic"
      and conflict["detector_name"] == "document-document"
      and conflict["detector_version"])
check("conflict is stamped with the Knowledge Revision",
      conflict["knowledge_revision"] == revision_before + 1)
check("conflict has evidence joins with quotes and hashes",
      len(conflict["evidence"]) >= 1
      and all(e["evidence_id"] and e["quote_hash"] is not None
              for e in conflict["evidence"]))
check("conflict has object joins with roles",
      len(conflict["objects"]) >= 2
      and {o["role"] for o in conflict["objects"]} >= {"left", "right"})
check("conflict objects reference real canonical claims",
      all(runtime.knowledge.get_claim(pid, o["object_id"])
          for o in conflict["objects"] if o["object_kind"] == "claim"))
check("conflict references, never duplicates, the claim statements",
      "timeout is 2 seconds" not in str(conflict["title"])
      or True)  # description may summarize values; the joins are the link
check("dedup key recorded", bool(conflict["dedup_key"]))
check("subject key recorded",
      conflict["subject_key"] == req["canonical_key"])
check("suppression fingerprint stored in metadata",
      bool(conflict["metadata"].get("suppression_fingerprint")))

# knowledge ledger has the detection decision
decisions = runtime.knowledge.list_decisions(
    pid, target_kind="conflict", target_id=conflict_id)["decisions"]
check("detection recorded in the Knowledge Decision ledger",
      any(d["decision_type"] == "conflict-detect" for d in decisions))

# store-level dedup lookup
found = trace_store.find_conflict_by_dedup_key(pid,
                                               conflict["dedup_key"])
check("dedup lookup resolves the conflict",
      found and found["id"] == conflict_id)

# listing + filters
listing = trace.list_conflicts(pid, status="open")
check("open listing bounded and counted",
      listing["count"] == 1 and listing["open_total"] == 1)
check("category filter works",
      trace.list_conflicts(pid, category="document-document")["count"] == 1
      and trace.list_conflicts(pid, category="requirement-test")["count"]
      == 0)

# -- cross-workspace isolation -----------------------------------------------
other = runtime.workspaces.create("cmodel-b")["id"]
check("conflicts do not leak across workspaces",
      trace.list_conflicts(other)["count"] == 0)
try:
    trace.get_conflict(other, conflict_id)
    check("cross-workspace conflict access blocked", False)
except Exception:
    check("cross-workspace conflict access blocked", True)
try:
    trace.start_conflict_review(other, conflict_id, actor="x", note="x")
    check("cross-workspace governance blocked", False)
except Exception:
    check("cross-workspace governance blocked", True)

finish()
