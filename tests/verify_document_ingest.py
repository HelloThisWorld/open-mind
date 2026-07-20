"""Document import, identity, incrementality and Evidence recovery.

The mandatory Phase 3 import cases, end to end through the real runtime, the
real job worker, the real content store and the real vector store.

The invariant under test throughout: OpenMind must never silently merge two
documents, never lose history when a source file disappears, never let an
absolute machine path into the portable database, and never leave a current
Revision pointing at partial data.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from openmind import content_store, db, vectorstore  # noqa: E402
from openmind.documents import intake  # noqa: E402
from openmind.domain.types import ImportStatus  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

_results = []
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures" / "documents"


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


runtime = get_runtime()
SRC = Path(tempfile.mkdtemp(prefix="om_docsrc_"))
(SRC / "NameCheckService.java").write_text(
    "package com.example.namecheck;\n"
    "public class NameCheckService {\n"
    "    public ScreeningResult screen(ScreeningRequest request) "
    "{ return null; }\n}\n", encoding="utf-8")
WS = runtime.workspaces.create("doc-ingest", path=str(SRC))["id"]
runtime.ingest.start(WS, wait=True, timeout=900)
store = vectorstore.get_documents_store(WS)


def outside(name, source=None, text=None):
    """A file OUTSIDE every registered source root."""
    target = Path(tempfile.mkdtemp(prefix="om_att_")) / name
    if text is not None:
        target.write_text(text, encoding="utf-8")
    else:
        shutil.copy(FIXTURES / source, target)
    return target


def add(path, **kwargs):
    kwargs.setdefault("wait", True)
    kwargs.setdefault("timeout", 900)
    return runtime.documents.add_document(WS, str(path), **kwargs)


# ---------------------------------------------------------------------------
# 1. Add a document from OUTSIDE the registered source roots
# ---------------------------------------------------------------------------
attached = outside("Requirements_v3.md", source="sample-requirements.md")
first = add(attached)
report = first.get("import_report") or {}
check("1: a document outside every source root imports",
      first.get("error") is None)
check("1: it is a new asset", first["status"] == ImportStatus.NEW_ASSET)
check("1: its logical key is the attachment default",
      first["logical_key"] == "documents/Requirements_v3.md")
check("1: the job completed", first.get("completed") is True)
check("1: it produced segments", report.get("segments_created", 0) > 0)
check("1: every content-bearing block was indexed",
      report.get("blocks_indexed", 0) > 0)
check("1: the parse status is recorded", report.get("parse_status") == "parsed")
check("1: the parser name and version are recorded",
      report.get("parser") == "markdown" and report.get("parser_version"))
ASSET = report["asset_id"]
REVISION = report["revision_id"]
check("1: the asset is marked as an attachment",
      db.get_asset(WS, ASSET)["source_kind"] == "attachment")
check("1: a parse record exists for the revision",
      db.get_document_parse(WS, REVISION) is not None)
check("1: every stored segment has evidence",
      len(db.evidence_ids_for_revision(WS, REVISION))
      == db.count_segments(WS, REVISION))

# ---------------------------------------------------------------------------
# 2. The absolute source path is absent from the portable database
# ---------------------------------------------------------------------------
conn = db._c()
dump = json.dumps([dict(r) for r in conn.execute(
    "SELECT job_id, project_id, type, path, payload_json, "
    "progress_json, log_tail_json FROM jobs").fetchall()])
check("2: the absolute origin path is not in any job row",
      str(attached) not in dump and str(attached.parent) not in dump)
assets_dump = json.dumps([dict(r) for r in conn.execute(
    "SELECT logical_key, source_path, title, metadata_json FROM assets"
).fetchall()])
check("2: the absolute origin path is not in any asset row",
      str(attached) not in assets_dump and str(attached.parent) not in assets_dump)
locators = json.dumps([dict(r) for r in conn.execute(
    "SELECT locator_json, excerpt FROM evidence").fetchall()])
check("2: the absolute origin path is not in any evidence locator",
      str(attached.parent) not in locators)
check("2: the job payload carries a staged blob hash, not a path",
      bool(db.get_job_payload(first["job_id"]).get("staged_blob_hash")))
check("2: the job payload's filename is a NAME, not a path",
      "/" not in db.get_job_payload(first["job_id"])["original_filename"])

# ---------------------------------------------------------------------------
# 3. An exact duplicate creates no revision
# ---------------------------------------------------------------------------
revisions_before = len(db.list_revisions(WS, ASSET))
chunks_before = store.count()
duplicate = add(attached)
check("3: re-adding identical bytes reports `duplicate`",
      duplicate["status"] == ImportStatus.DUPLICATE)
check("3: no job is created for a duplicate", duplicate.get("job_id") is None)
check("3: no new revision is created",
      len(db.list_revisions(WS, ASSET)) == revisions_before)
check("3: no vector duplicate is created", store.count() == chunks_before)
check("3: the existing asset is returned", duplicate["asset_id"] == ASSET)

# ---------------------------------------------------------------------------
# 4. An explicit --asset creates the next revision
# ---------------------------------------------------------------------------
changed = outside("Requirements_v3.md",
                  text="# NameCheck Requirements\n\n"
                       "REQ-NC-900: a completely new rule.\n")
revised = add(changed, asset_id=ASSET)
check("4: an explicit asset id yields `revision`",
      revised["status"] == ImportStatus.REVISION)
check("4: the next revision is created",
      (revised.get("import_report") or {}).get("revision_sequence") == 2)
check("4: it is the same asset",
      (revised.get("import_report") or {}).get("asset_id") == ASSET)
history = db.list_revisions(WS, ASSET)
check("4: the previous revision is marked superseded",
      any(r["sequence"] == 1 and r["status"] == "superseded" for r in history))
check("4: the previous revision's segments are PRESERVED",
      db.count_segments(WS, REVISION) > 0)
check("4: the document index now names the new revision",
      db.get_document_index(WS, ASSET)["revision_id"]
      == (revised.get("import_report") or {}).get("revision_id"))

# ---------------------------------------------------------------------------
# 5. A filename collision returns possible_revision and writes NOTHING
# ---------------------------------------------------------------------------
colliding = outside("Requirements_v3.md",
                    text="# A different team's document\n\nUnrelated.\n")
assets_before = db.count_assets(WS)
revisions_before = len(db.list_revisions(WS, ASSET))
chunks_before = store.count()
collision = add(colliding)
check("5: a filename collision with different content is `possible_revision`",
      collision["status"] == ImportStatus.POSSIBLE_REVISION)
check("5: no job is created", collision.get("job_id") is None)
check("5: no asset is created", db.count_assets(WS) == assets_before)
check("5: no revision is created",
      len(db.list_revisions(WS, ASSET)) == revisions_before)
check("5: no vector entry is created", store.count() == chunks_before)
check("5: the possible asset is named",
      (collision.get("possible_asset") or {}).get("id") == ASSET)
check("5: both content hashes are reported",
      bool(collision.get("content_hash"))
      and bool(collision.get("existing_content_hash"))
      and collision["content_hash"] != collision["existing_content_hash"])
guidance = collision.get("guidance") or {}
check("5: the retry commands are given",
      guidance.get("as_revision", "").startswith("--asset ")
      and guidance.get("as_new_document") == "--new-asset")
check("5: a readable deterministic alternative key is suggested",
      guidance.get("suggested_new_key", "").startswith(
          "documents/Requirements_v3--"))

# ---------------------------------------------------------------------------
# 6. --new-asset creates a distinct, deterministic, READABLE asset
# ---------------------------------------------------------------------------
distinct = add(colliding, new_asset=True)
check("6: --new-asset creates a new asset",
      distinct["status"] == ImportStatus.NEW_ASSET)
check("6: the key is distinct from the colliding one",
      distinct["logical_key"] != "documents/Requirements_v3.md")
check("6: the key is readable, not opaque",
      distinct["logical_key"].startswith("documents/Requirements_v3--")
      and distinct["logical_key"].endswith(".md"))
check("6: the key is deterministic",
      intake.distinct_logical_key("Requirements_v3.md",
                                  distinct["content_hash"])
      == distinct["logical_key"])
check("6: re-running --new-asset does NOT create a third asset",
      add(colliding, new_asset=True)["status"] == ImportStatus.DUPLICATE)

# ---------------------------------------------------------------------------
# 7. An explicit version label is stored; nothing is inferred
# ---------------------------------------------------------------------------
labelled = outside("labelled.md", text="# Labelled\n\nbody text here.\n")
with_label = add(labelled, version_label="3.2.1")
label_report = with_label.get("import_report") or {}
check("7: an explicit version label is stored",
      label_report.get("version_label") == "3.2.1")
check("7: the label's SOURCE is recorded as explicit",
      label_report.get("version_label_source") == "explicit")
check("7: the label is on the revision row",
      db.get_revision(WS, label_report["revision_id"])["version_label"] == "3.2.1")
check("7: the revision status stays `unknown` (authority is never inferred)",
      db.get_revision(WS, label_report["revision_id"])["status"] == "unknown")
unlabelled = add(outside("plain.md", text="# Plain\n\nNo version anywhere.\n"))
check("7: no version is invented when the document declares none",
      (unlabelled.get("import_report") or {}).get("version_label") == "")
docx_import = add(outside("req.docx", source="sample-requirements.docx"))
check("7: a DOCX core `revision` property becomes the version label",
      (docx_import.get("import_report") or {}).get("version_label") == "3")
check("7: the label's source is the document's own metadata",
      (docx_import.get("import_report") or {}).get("version_label_source")
      == "document-metadata")

# ---------------------------------------------------------------------------
# 8. A detached attachment survives its original file being deleted
# ---------------------------------------------------------------------------
os.remove(attached)
os.remove(changed)
document = runtime.documents.get_document(WS, ASSET)
check("8: the attachment is still active after its origin is deleted",
      document["state"] == "active")
outline = runtime.documents.get_outline(
    WS, document["current_revision"]["id"], limit=10)
check("8: its outline is still readable", outline["count"] > 0)
evidence_ids = db.evidence_ids_for_revision(WS,
                                            document["current_revision"]["id"])
some_evidence = sorted(evidence_ids.values())[0]
recovered = runtime.assets.get_evidence(WS, some_evidence)
check("8: its evidence content is still recoverable",
      recovered["snapshot"]["status"] == "available" and recovered["content"])
check("8: current-source status is `not-applicable`, never a false `missing`",
      recovered["current_source"]["status"] == "not-applicable")
check("8: a full re-ingest does NOT mark the attachment removed",
      (runtime.ingest.start(WS, wait=True, timeout=900) and
       db.get_asset(WS, ASSET)["state"] == "active"))

# ---------------------------------------------------------------------------
# 9-11. Workspace document files: change, removal, reappearance
# ---------------------------------------------------------------------------
DOCS = SRC / "docs"
DOCS.mkdir(exist_ok=True)
shutil.copy(FIXTURES / "sample-design.pdf", DOCS / "design.pdf")
shutil.copy(FIXTURES / "sample-requirements.md", DOCS / "requirements.md")
runtime.ingest.start(WS, wait=True, timeout=900)
workspace_docs = {d["logical_key"] for d in db.list_document_assets(WS)}
check("9: workspace documents under a source root are discovered",
      {"docs/design.pdf", "docs/requirements.md"} <= workspace_docs)
req_asset = db.find_asset_by_logical_key(WS, "docs/requirements.md")
check("9: a workspace document is a `file`, not an attachment",
      req_asset["source_kind"] == "file")

result = runtime.ingest.start(WS, wait=True, timeout=900)
progress = result["job"]["progress"]
check("9: an unchanged sync creates NO revision",
      progress.get("documents_imported") == 0)
check("9: an unchanged sync reports every document as unchanged",
      progress.get("documents_unchanged") == progress.get("documents_total"))
check("9: an unchanged sync leaves the revision count alone",
      len(db.list_revisions(WS, req_asset["id"])) == 1)

(DOCS / "requirements.md").write_text(
    "# NameCheck Requirements\n\nREQ-NC-777: one changed rule.\n",
    encoding="utf-8")
before_pdf_chunks = len(
    db.get_document_index(WS, db.find_asset_by_logical_key(
        WS, "docs/design.pdf")["id"])["chunk_ids"])
result = runtime.ingest.start(WS, wait=True, timeout=900)
check("9: one changed document creates exactly ONE new revision",
      result["job"]["progress"].get("documents_imported") == 1
      and len(db.list_revisions(WS, req_asset["id"])) == 2)
check("9: the unrelated document was NOT reindexed",
      len(db.get_document_index(WS, db.find_asset_by_logical_key(
          WS, "docs/design.pdf")["id"])["chunk_ids"]) == before_pdf_chunks)

pdf_asset = db.find_asset_by_logical_key(WS, "docs/design.pdf")
pdf_revision = pdf_asset["current_revision_id"]
pdf_segments = db.count_segments(WS, pdf_revision)
os.remove(DOCS / "design.pdf")
runtime.ingest.start(WS, wait=True, timeout=900)
removed = db.find_asset_by_logical_key(WS, "docs/design.pdf")
check("10: a removed workspace document is marked `removed`",
      removed["state"] == "removed")
check("10: its revisions are PRESERVED",
      len(db.list_revisions(WS, removed["id"])) == 1)
check("10: its segments are PRESERVED",
      db.count_segments(WS, pdf_revision) == pdf_segments)
check("10: its content blob is PRESERVED",
      content_store.exists(WS, db.get_revision(WS, pdf_revision)["content_blob_hash"]))
check("10: its active vector projection is dropped",
      db.get_document_index(WS, removed["id"]) is None)
check("10: it no longer appears in the default document listing",
      "docs/design.pdf" not in {d["logical_key"]
                                for d in db.list_document_assets(WS)})

shutil.copy(FIXTURES / "sample-design.pdf", DOCS / "design.pdf")
runtime.ingest.start(WS, wait=True, timeout=900)
back = db.find_asset_by_logical_key(WS, "docs/design.pdf")
check("11: a reappeared document reactivates the SAME asset",
      back["id"] == removed["id"] and back["state"] == "active")
check("11: no duplicate revision is created for unchanged content",
      len(db.list_revisions(WS, back["id"])) == 1)
check("11: its vector projection is REBUILT (not left invisible to search)",
      db.get_document_index(WS, back["id"]) is not None
      and len(db.get_document_index(WS, back["id"])["chunk_ids"]) > 0)
check("11: it is searchable again",
      bool(runtime.documents.search(WS, "NC-100", limit=5)["hits"]))

# ---------------------------------------------------------------------------
# 12. A parse failure leaves no partial current revision
# ---------------------------------------------------------------------------
scanned = add(outside("scan.pdf", source="sample-scanned.pdf"))
scan_report = scanned.get("import_report") or {}
check("12: an image-only PDF imports as `needs-ocr`",
      scan_report.get("parse_status") == "needs-ocr")
check("12: it creates NO content revision",
      scan_report.get("revision_created") is False)
check("12: it is registered honestly as an unsupported asset",
      db.get_asset(WS, scan_report["asset_id"])["state"] == "unsupported")
check("12: no current revision points at partial data",
      db.get_asset(WS, scan_report["asset_id"])["current_revision_id"] is None)
check("12: it is not indexed",
      db.get_document_index(WS, scan_report["asset_id"]) is None)

encrypted = add(outside("locked.pdf", source="sample-encrypted.pdf"))
enc_report = encrypted.get("import_report") or {}
check("12: an encrypted PDF imports as `encrypted`",
      enc_report.get("parse_status") == "encrypted")
check("12: it creates no revision", enc_report.get("revision_created") is False)

check("12: EVERY active document asset has a complete current revision",
      all(db.count_segments(WS, d["current_revision_id"]) > 0
          and db.get_document_parse(WS, d["current_revision_id"]) is not None
          for d in db.list_document_assets(WS)))

# ---------------------------------------------------------------------------
# 13. An interrupted document job is resumable
# ---------------------------------------------------------------------------
resumable = outside("resume.md", text="# Resume me\n\nbody content.\n")
staged = content_store.put(WS, resumable.read_bytes())
job = runtime.jobs.enqueue_document_ingest(WS, {
    "staged_blob_hash": staged, "original_filename": "resume.md",
    "requested_logical_key": "documents/resume.md",
    "import_mode": "new_asset", "source_kind": "attachment"})
db.update_job(job["job_id"], status="running")
import openmind.jobs as jobs_module  # noqa: E402

jobs_module._recover_on_restart()
recovered_job = db.get_job(job["job_id"])
check("13: a document job interrupted by a restart is marked interrupted",
      recovered_job["status"] == "interrupted")
check("13: the staged blob survives the restart",
      content_store.exists(WS, staged))
check("13: its payload survives the restart",
      db.get_job_payload(job["job_id"])["staged_blob_hash"] == staged)
runtime.jobs.resume(job["job_id"])
outcome = runtime.jobs.wait_for_terminal(job["job_id"], timeout=600)
check("13: resuming completes it", outcome.completed is True)
check("13: the resumed import produced the document",
      db.find_asset_by_logical_key(WS, "documents/resume.md") is not None)

# ---------------------------------------------------------------------------
# 14. Evidence: exact block text, hash validation, portability, bounds
# ---------------------------------------------------------------------------
doc_asset = db.find_asset_by_logical_key(WS, "docs/requirements.md")
doc_revision = doc_asset["current_revision_id"]
segments = db.list_segments(WS, doc_revision, limit=100)
ev_by_segment = db.evidence_ids_for_revision(WS, doc_revision)
target = next(s for s in segments if s["segment_type"] == "paragraph")
ev = runtime.assets.get_evidence(WS, ev_by_segment[target["id"]])
check("14: document evidence resolves from the segment content blob",
      ev["snapshot"]["status"] == "available")
check("14: it returns the EXACT stored block text",
      content_store.get(WS, target["content_blob_hash"]).decode()
      == ev["content"])
check("14: the segment content-blob hash validates",
      content_store.verify(WS, target["content_blob_hash"]))
check("14: the evidence content hash matches the segment's",
      ev["content_hash"] == target["content_hash"])
check("14: the locator is portable (no absolute path)",
      ev["locator"]["document"] == "docs/requirements.md")
check("14: the parser that produced it is reported",
      (ev.get("parser") or {}).get("name") == "markdown")
check("14: a workspace document reports current-source `matches`",
      ev["current_source"]["status"] == "matches")

(DOCS / "requirements.md").write_text("# Edited on disk\n\nchanged.\n",
                                      encoding="utf-8")
ev_changed = runtime.assets.get_evidence(WS, ev_by_segment[target["id"]])
check("14: after an on-disk edit the snapshot is STILL available",
      ev_changed["snapshot"]["status"] == "available")
check("14: current-source reports `changed`",
      ev_changed["current_source"]["status"] == "changed")
os.remove(DOCS / "requirements.md")
ev_missing = runtime.assets.get_evidence(WS, ev_by_segment[target["id"]])
check("14: after deletion current-source reports `missing`",
      ev_missing["current_source"]["status"] == "missing")
check("14: the historical content is STILL recoverable after deletion",
      ev_missing["snapshot"]["status"] == "available" and ev_missing["content"])

bounded = runtime.assets.get_evidence(WS, ev_by_segment[target["id"]],
                                      max_chars=12)
check("14: bounded output truncates honestly",
      len(bounded["content"]) <= 12 and bounded["truncated"] is True)

# Code evidence must still resolve the Phase 2 way.
code_asset = db.find_asset_by_logical_key(WS, "NameCheckService.java")
code_revision = code_asset["current_revision_id"]
code_ev = db.evidence_ids_for_revision(WS, code_revision)
code_result = runtime.assets.get_evidence(WS, sorted(code_ev.values())[0])
check("14: CODE evidence still resolves from the revision line range",
      code_result["snapshot"]["status"] == "available")
check("14: code segments have no block blob (they are not backfilled)",
      all(s["content_blob_hash"] == ""
          for s in db.list_segments(WS, code_revision, limit=50)))

# ---------------------------------------------------------------------------
# 15. Filtered ingest must not prune unrelated document assets
# ---------------------------------------------------------------------------
shutil.copy(FIXTURES / "sample-cases.csv", DOCS / "cases.csv")
runtime.ingest.start(WS, wait=True, timeout=900)
active_before = {d["logical_key"] for d in db.list_document_assets(WS)}
runtime.assets.sync_file(WS, str(SRC / "NameCheckService.java"), wait=True,
                         timeout=600)
check("15: a filtered CODE ingest prunes no document asset",
      {d["logical_key"] for d in db.list_document_assets(WS)} == active_before)
check("15: a filtered code ingest leaves the document index intact",
      all(db.get_document_index(WS, d["id"]) is not None
          for d in db.list_document_assets(WS)))

# ---------------------------------------------------------------------------
# 16. Discovery policy: one file never becomes two assets
# ---------------------------------------------------------------------------
from openmind import config  # noqa: E402

check("16: no document discovery extension is also a code extension",
      not (config.DOCUMENT_DISCOVERY_EXTENSIONS & config.CODE_INDEX_EXTENSIONS))
check("16: .pdf was never added to INDEX_EXTENSIONS",
      ".pdf" not in config.INDEX_EXTENSIONS
      and ".docx" not in config.INDEX_EXTENSIONS)
keys = [a["logical_key"] for a in db.list_assets(WS, limit=200)]
check("16: no logical key is duplicated across assets",
      len(keys) == len(set(keys)))

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
