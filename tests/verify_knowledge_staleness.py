"""Graph staleness: revision movement stales bindings, claims and dependent
relations; manual entities with valid claims stay active; authority
preserved; the startup backstop repairs missed events."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _knowledge_helpers import (REQUIREMENTS_MD, check,  # noqa: E402
                                finish, find_evidence,
                                make_confirmed_candidate,
                                make_minimal_workspace)

from openmind import db  # noqa: E402
from openmind.knowledge import store  # noqa: E402
from openmind.knowledge.reconciliation import (  # noqa: E402
    reconcile_graph_staleness)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

# a promoted claim bound to the document's current revision
candidate_id = make_confirmed_candidate(runtime, pid)
promoted = knowledge.promote_candidate(pid, candidate_id, actor="r",
                                       note="promoted before staleness")
entity_id = promoted["entity"]["id"]
claim_id = promoted["claim"]["id"]
knowledge.set_authority(pid, kind="claim", object_id=claim_id,
                        authority="authoritative", actor="lead", note="a")

# a manual entity + claim on the same evidence, plus a relation between them
manual = knowledge.create_entity(
    pid, entity_type="business-rule", canonical_key="business-rule:BR-9",
    display_name="BR-9", evidence=[{"evidence_id": evidence_id}],
    actor="t", note="manual")["entity"]
manual_claim = knowledge.create_claim(
    pid, entity_id=manual["id"], claim_type="normative-statement",
    statement="Failed name checks must be retried three times.",
    evidence=[{"evidence_id": find_evidence(pid, "retried three times")}],
    actor="t", note="manual claim")["claim"]
relation = knowledge.create_relation(
    pid, source_entity_id=manual["id"], target_entity_id=entity_id,
    relation_type="refines", relation_state="confirmed",
    evidence=[{"evidence_id": evidence_id}], actor="t",
    note="relation to be staled")["relation"]

check("promoted claim starts active",
      store.get_claim(pid, claim_id)["lifecycle_status"] == "active")

# -- a NEW document revision stales the promoted knowledge -------------------
docs = Path(tempfile.mkdtemp(prefix="om_kgstale_"))
changed = REQUIREMENTS_MD.replace("within 2 seconds", "within 5 seconds")
doc_asset = db.list_assets(pid, state="active", limit=10)[0]
path = docs / "requirements.md"
path.write_text(changed, encoding="utf-8")
result = runtime.documents.add_document(pid, str(path),
                                        asset_id=doc_asset["id"],
                                        wait=True, timeout=180)
check("fixture produced a new document revision",
      result.get("status") == "revision")

# the import hook already reconciled; run again to prove idempotence
outcome = reconcile_graph_staleness(pid)
claim_after = store.get_claim(pid, claim_id)
check("new document revision staled the promoted claim",
      claim_after["lifecycle_status"] == "stale")
check("authority status preserved on the stale claim",
      claim_after["authority_status"] == "authoritative")
check("stale claim remains queryable with its evidence",
      len(claim_after["evidence"]) >= 1)
check("old-revision bindings went stale",
      any(b["status"] == "stale" and b["ref_kind"] == "revision"
          for b in store.list_bindings(pid, entity_id)))
check("stale claim staled the dependent relation",
      store.get_relation(pid, relation["id"])["lifecycle_status"]
      == "stale")
# The promoted ENTITY keeps its active asset binding (the document asset
# still exists), so it stays active as the identity anchor a re-analysis
# would re-attach to — only its CLAIM content went stale.
check("promoted entity stays active as an identity anchor (asset binding "
      "still current)",
      store.get_entity(pid, entity_id)["lifecycle_status"] == "active"
      and any(b["status"] == "active" and b["ref_kind"] == "asset"
              for b in store.list_bindings(pid, entity_id)))
check("manual entity's claim also staled (its evidence moved on) but the "
      "record survives",
      store.get_claim(pid, manual_claim["id"])["lifecycle_status"]
      == "stale")

# review history intact on the source candidate
check("confirmed review status preserved on the stale candidate",
      runtime.semantic.get_candidate(pid, candidate_id)["review_status"]
      == "confirmed")

# -- manual entity with a VALID active claim stays active --------------------
fresh_evidence = find_evidence(pid, "within 5 seconds")
active_manual = knowledge.create_entity(
    pid, entity_type="decision", canonical_key="decision:D-1",
    display_name="D-1", evidence=[{"evidence_id": fresh_evidence}],
    actor="t", note="fresh manual")["entity"]
knowledge.create_claim(
    pid, entity_id=active_manual["id"], claim_type="decision-rationale",
    statement="The 5 second budget was chosen after load testing.",
    evidence=[{"evidence_id": fresh_evidence}], actor="t", note="c")
reconcile_graph_staleness(pid)
check("manual entity with a valid current claim stays active",
      store.get_entity(pid, active_manual["id"])["lifecycle_status"]
      == "active")

# -- second reconcile pass changes nothing (idempotent) ----------------------
second = reconcile_graph_staleness(pid)
check("reconciliation is idempotent",
      not second["changed"])

# -- the startup backstop repairs a missed event -----------------------------
# simulate a missed event: force the stale claim active again directly
conn, lock = db.shared_connection()
with lock:
    conn.execute("UPDATE engineering_claims SET lifecycle_status='active' "
                 "WHERE id=?", (claim_id,))
    conn.commit()
from openmind import jobs  # noqa: E402
jobs.reconcile_semantic_staleness(pid)   # the worker-startup hook
check("startup reconciliation repaired the missed staleness event",
      store.get_claim(pid, claim_id)["lifecycle_status"] == "stale")

# -- reappearing evidence revives ---------------------------------------------
# revert the document to its original content: a NEW revision whose blocks
# match the original evidence text again... the ORIGINAL evidence rows still
# belong to the old revision, so the claim STAYS stale (revisions never
# become current again) — the honest contract.
path.write_text(REQUIREMENTS_MD, encoding="utf-8")
runtime.documents.add_document(pid, str(path), asset_id=doc_asset["id"],
                               wait=True, timeout=180)
reconcile_graph_staleness(pid)
check("a content revert does not falsely revive old-revision claims",
      store.get_claim(pid, claim_id)["lifecycle_status"] == "stale")

finish()
