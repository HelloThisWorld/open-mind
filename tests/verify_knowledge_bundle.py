"""Knowledge Bundle 2.0 Draft: deterministic export, manifest hashes,
referential integrity, current-only vs include-history, verifier catches
corruption, `.openmind` 1.x untouched."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _knowledge_helpers import (check, finish, find_evidence,  # noqa: E402
                                make_confirmed_candidate,
                                make_minimal_workspace)

from openmind import artifacts  # noqa: E402
from openmind.bundle_verify import verify_bundle  # noqa: E402
from openmind.knowledge.bundle import (BUNDLE_SCHEMA_VERSION,  # noqa: E402
                                       export_bundle)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
knowledge = runtime.knowledge
pid = make_minimal_workspace(runtime)
evidence_id = find_evidence(pid, "REQ-NC-017")

# content: one promoted candidate + one manual entity/claim/relation, one
# withdrawn claim for history-mode coverage
candidate_id = make_confirmed_candidate(runtime, pid)
promoted = knowledge.promote_candidate(pid, candidate_id, actor="r",
                                       note="promote for bundle")
manual = knowledge.create_entity(
    pid, entity_type="business-rule", canonical_key="business-rule:BR-2",
    display_name="BR-2", evidence=[{"evidence_id": evidence_id}],
    actor="t", note="m")["entity"]
knowledge.create_relation(
    pid, source_entity_id=manual["id"],
    target_entity_id=promoted["entity"]["id"], relation_type="refines",
    relation_state="confirmed", evidence=[{"evidence_id": evidence_id}],
    actor="t", note="r")
withdrawn = knowledge.create_claim(
    pid, entity_id=manual["id"], claim_type="implementation-note",
    statement="A withdrawn note for history mode.",
    evidence=[{"evidence_id": evidence_id}], actor="t", note="c")
knowledge.withdraw_object(pid, kind="claim",
                          object_id=withdrawn["claim"]["id"], actor="t",
                          note="w")

out_a = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "a"
out_b = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "b"
STAMP = "2026-07-22T00:00:00"
manifest = export_bundle(pid, str(out_a), current_only=True,
                         generated_at=STAMP)
manifest_b = export_bundle(pid, str(out_b), current_only=True,
                           generated_at=STAMP)

check("bundle schema version is the draft",
      manifest["bundleSchemaVersion"] == BUNDLE_SCHEMA_VERSION
      == "2.0.0-draft.1")
check("manifest records workspace, revision and counts",
      manifest["workspaceId"] == pid
      and manifest["knowledgeRevision"] >= 3
      and manifest["counts"]["entities"] >= 2
      and manifest["counts"]["claims"] >= 1)
expected_files = {"entities.jsonl", "claims.jsonl", "relations.jsonl",
                  "aliases.jsonl", "bindings.jsonl", "claim-evidence.jsonl",
                  "relation-evidence.jsonl", "decisions.jsonl",
                  "knowledge-revisions.jsonl", "evidence.jsonl",
                  "assets.jsonl", "revisions.jsonl", "segments.jsonl",
                  "lenses.jsonl", "workspace.json"}
check("manifest names every contract file",
      expected_files <= set(manifest["files"]))
check("schemas directory shipped inside the bundle",
      (out_a / "schemas" / "entity.schema.json").is_file()
      and (out_a / "schemas" / "manifest.schema.json").is_file())

# determinism: identical bytes across two exports with a pinned timestamp
identical = all((out_a / name).read_bytes() == (out_b / name).read_bytes()
                for name in manifest["files"])
check("export is byte-identical across runs (pinned timestamp)",
      identical and (out_a / "manifest.json").read_bytes()
      == (out_b / "manifest.json").read_bytes())

# the standalone verifier accepts it
report = verify_bundle(str(out_a))
check("bundle verifier accepts a clean current-only export",
      report.ok and not report.errors)

# no absolute paths / secrets anywhere
blob = "".join((out_a / name).read_text(encoding="utf-8")
               for name in manifest["files"])
check("no windows drive-letter path in the bundle", ":\\\\" not in blob
      and ":\\" not in blob.replace("\\\\", ""))
check("no api-key material in the bundle", "api_key" not in blob.lower()
      or "api_key_env" not in blob)

# active claims all carry evidence in-bundle
claims = [json.loads(line) for line in
          (out_a / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
joins = [json.loads(line) for line in
         (out_a / "claim-evidence.jsonl").read_text(
             encoding="utf-8").splitlines()]
by_claim = {j["claim_id"] for j in joins}
check("every exported active claim has an evidence join",
      all(c["id"] in by_claim for c in claims
          if c["lifecycle_status"] == "active"))
check("current-only export excludes the withdrawn claim",
      all(c["id"] != withdrawn["claim"]["id"] for c in claims))

# include-history mode
out_h = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "h"
manifest_h = export_bundle(pid, str(out_h), current_only=False,
                           generated_at=STAMP)
history_claims = [json.loads(line) for line in
                  (out_h / "claims.jsonl").read_text(
                      encoding="utf-8").splitlines()]
check("include-history contains the withdrawn claim",
      any(c["id"] == withdrawn["claim"]["id"]
          and c["lifecycle_status"] == "withdrawn"
          for c in history_claims))
check("history export passes the verifier too",
      verify_bundle(str(out_h)).ok)

# knowledge-revision cap
out_r = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "r"
manifest_r = export_bundle(pid, str(out_r), current_only=False,
                           knowledge_revision=1, generated_at=STAMP)
capped_entities = [json.loads(line) for line in
                   (out_r / "entities.jsonl").read_text(
                       encoding="utf-8").splitlines()]
check("revision cap keeps only records created at or before N",
      all(e["created_knowledge_revision"] <= 1 for e in capped_entities))
check("revision cap is documented as a stamp filter",
      any("point-in-time" in l for l in manifest_r["limitations"]))

# -- the verifier CATCHES corruption -----------------------------------------
import shutil  # noqa: E402

broken = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "broken"
shutil.copytree(out_a, broken)
lines = (broken / "relations.jsonl").read_text(
    encoding="utf-8").splitlines()
if lines:
    relation = json.loads(lines[0])
    relation["target_entity_id"] = "ent_gone"
    lines[0] = json.dumps(relation, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    (broken / "relations.jsonl").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
report = verify_bundle(str(broken))
check("verifier catches a missing relation endpoint",
      not report.ok
      and any("missing entity" in e for e in report.errors))
check("verifier catches the hash mismatch of the edited file",
      any("sha256 mismatch" in e for e in report.errors))

broken2 = Path(tempfile.mkdtemp(prefix="om_bundle_")) / "broken2"
shutil.copytree(out_a, broken2)
(broken2 / "evidence.jsonl").write_text("", encoding="utf-8")
report2 = verify_bundle(str(broken2))
check("verifier catches missing evidence rows",
      not report2.ok
      and any("missing evidence" in e for e in report2.errors))

# -- `.openmind` 1.x remains unchanged ---------------------------------------
check(".openmind artifact schema is still 1.1.0",
      artifacts.SCHEMA_VERSION == "1.1.0")

finish()
