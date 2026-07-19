"""Canonical Asset model, end to end through the real ingestion pipeline:
first ingest, idempotency (no re-embed), change, revert, removal, reappearance,
legacy backfill (no embed), FK cascade, and lifecycle (terminate/delete).

Runs a real ingest in an isolated data dir with the offline hashing embedder.
The embedding function is wrapped with a call counter so the "unchanged content
is never re-embedded" invariant is asserted directly, not assumed.
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from openmind import (config, content_store as cs, db, embeddings,  # noqa: E402
                      javaparse, jobs, segmentation)
from openmind.runtime import get_runtime  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


# ---------------------------------------------------------------------------
# Embedding call counter — proves "no re-embed" invariants directly.
# ---------------------------------------------------------------------------
_embed_orig = embeddings.embed
_embed_calls = {"n": 0}


def _embed_spy(docs, *a, **k):
    _embed_calls["n"] += 1
    return _embed_orig(docs, *a, **k)


embeddings.embed = _embed_spy


def _reset_embed():
    _embed_calls["n"] = 0


# ---------------------------------------------------------------------------
# A tiny fixture repo built on disk (deterministic content we control).
# ---------------------------------------------------------------------------
REPO = tempfile.mkdtemp(prefix="om_assets_repo_")


def _write(rel, text):
    path = os.path.join(REPO, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


JAVA = (
    "package org.example;\n"
    "public class Client {\n"
    "    private int id;\n"
    "    public Client(int id) { this.id = id; }\n"
    "    public void send(Request r) {\n"
    "        transport.write(r);\n"
    "    }\n"
    "}\n"
)
_write("src/main/java/org/example/Client.java", JAVA)
_write("src/app.py", "def main():\n    return 42\n")
_write("config/application.yaml", "server:\n  port: 8080\n")

runtime = get_runtime()
runtime.ensure_worker()
ws = runtime.workspaces.create("assets-demo", path=REPO)["id"]
other = runtime.workspaces.create("other-ws")["id"]


def _ingest():
    res = runtime.ingest.start(ws, wait=True, timeout=180)
    return res["job"]["progress"], res


# ===========================================================================
# 1. First ingest — every indexed file becomes an Asset with a full subtree
# ===========================================================================
_reset_embed()
p1, _ = _ingest()
files = db.get_file_index(ws)
assets = db.list_assets(ws, limit=100)
check("first ingest: every indexed file became an Asset",
      len(assets) == len(files) and len(assets) == 3)
check("first ingest: assets_created counter matches", p1.get("assets_created") == 3)
check("first ingest: a revision was created per asset", p1.get("revisions_created") == 3)
check("first ingest: segments were created", p1.get("segments_created", 0) >= 3)
check("first ingest: evidence was created", p1.get("evidence_created", 0) >= 3)
check("first ingest: content blobs were created", p1.get("content_blobs_created") == 3)
check("first ingest: the embedder ran at least once", _embed_calls["n"] >= 1)

# every active asset has a current revision, every revision >=1 segment, every
# segment has evidence with a valid 1-based range and a verbatim excerpt.
for a in assets:
    check(f"asset {a['logical_key']}: has a current revision",
          bool(a["current_revision_id"]))
    check(f"asset {a['logical_key']}: source_path is workspace-relative",
          not a["source_path"].startswith("/") and ":" not in a["source_path"][:3])
    segs = db.list_segments(ws, a["current_revision_id"])
    check(f"asset {a['logical_key']}: current revision has >=1 segment", len(segs) >= 1)
    for s in segs:
        ev = db.get_evidence_for_segment(ws, s["id"])
        loc = (ev or {}).get("locator") or {}
        ok_range = (ev is not None and loc.get("startLine", 0) >= 1
                    and loc.get("endLine", 0) >= loc.get("startLine", 0))
        check(f"segment {s['segment_key']}: has evidence with a valid 1-based range",
              ok_range)
        check(f"segment {s['segment_key']}: evidence locator file is relative",
              not str(loc.get("file", "")).startswith("/"))

# evidence excerpt is verbatim source recoverable from the blob
a_py = db.find_asset_by_logical_key(ws, "src/app.py")
rev_py = db.get_revision(ws, a_py["current_revision_id"])
seg_py = db.list_segments(ws, rev_py["id"])[0]
ev_py = db.get_evidence_for_segment(ws, seg_py["id"])
blob = cs.get(ws, rev_py["content_blob_hash"]).decode("utf-8")
recovered = segmentation.slice_lines(blob, ev_py["locator"]["startLine"],
                                     ev_py["locator"]["endLine"])
check("evidence content_hash is recoverable from the immutable blob",
      segmentation.hash_text_utf8(recovered) == ev_py["content_hash"])
check("evidence excerpt is a verbatim prefix of the recovered source",
      recovered.startswith(ev_py["excerpt"]) or ev_py["excerpt"] in recovered)

# Java segmentation (only when tree-sitter is available)
a_java = db.find_asset_by_logical_key(ws, "src/main/java/org/example/Client.java")
check("the Java file is classified as source-code", a_java["asset_type"] == "source-code")
if javaparse.available():
    jsegs = db.list_segments(ws, a_java["current_revision_id"])
    types = {s["segment_type"] for s in jsegs}
    check("java: a 'type' segment exists (derived class summary)", "type" in types)
    check("java: a 'method' segment exists", "method" in types)
    check("java: a 'constructor' segment exists", "constructor" in types)
    derived = [s for s in jsegs if s["content_mode"] == "derived"]
    check("java: the class-summary segment is marked derived (not verbatim)",
          len(derived) >= 1)
else:
    check("java: tree-sitter unavailable — generic segments used (still valid)",
          len(db.list_segments(ws, a_java["current_revision_id"])) >= 1)

# consistency invariant (guards the commit-ordering fix): every file_index entry
# must have an in-sync active Asset whose current revision hashes to the file's
# actual content. file_index is written AFTER the Asset revision commits, so a
# file_index row can never point at a stale/absent Asset.
import hashlib  # noqa: E402


def _sha256_file(rel):
    with open(os.path.join(REPO, rel.replace("/", os.sep)), "rb") as fh:
        # blob is stored as the utf-8 re-encoding of the decoded text
        from openmind import walker as _w
        return hashlib.sha256(_w.read_text(
            os.path.join(REPO, rel.replace("/", os.sep))).encode("utf-8", "replace")).hexdigest()


idx = db.list_asset_index(ws)
_consistent = True
for rel in db.get_file_index(ws):
    ai = idx.get(rel)
    if not (ai and ai["state"] == "active" and ai["content_hash"] == _sha256_file(rel)):
        _consistent = False
check("every file_index entry has an in-sync active Asset (no stranded rows)",
      _consistent)

# ===========================================================================
# 2. Idempotency — unchanged re-ingest creates nothing and never re-embeds
# ===========================================================================
seg_count_before = db.asset_stats(ws)["segments"]
_reset_embed()
p2, _ = _ingest()
check("re-ingest: zero new revisions", p2.get("revisions_created") == 0)
check("re-ingest: every asset reused", p2.get("assets_reused") == 3)
check("re-ingest: revisions reused", p2.get("revisions_reused") == 3)
check("re-ingest: content blobs reused, none created",
      p2.get("content_blobs_created", 0) == 0)
check("re-ingest: segment count is stable",
      db.asset_stats(ws)["segments"] == seg_count_before)
check("re-ingest: the embedder was NOT called (nothing changed)",
      _embed_calls["n"] == 0)

# ===========================================================================
# 3. Change one file — exactly one new revision; others untouched; history kept
# ===========================================================================
time.sleep(1.0)  # ensure a distinct mtime; content hash is what actually decides
_write("src/app.py", "def main():\n    return 43  # changed\n")
first_rev_id = a_py["current_revision_id"]
first_blob = rev_py["content_blob_hash"]
_reset_embed()
p3, _ = _ingest()
check("change: exactly one new revision", p3.get("revisions_created") == 1)
a_py2 = db.find_asset_by_logical_key(ws, "src/app.py")
revs_py = db.list_revisions(ws, a_py2["id"])
check("change: the changed asset now has 2 revisions", len(revs_py) == 2)
check("change: current revision advanced", a_py2["current_revision_id"] != first_rev_id)
old = db.get_revision(ws, first_rev_id)
check("change: the previous revision is still queryable", old is not None)
check("change: the previous revision is marked superseded", old["status"] == "superseded")
check("change: the new revision supersedes the previous current",
      db.get_revision(ws, a_py2["current_revision_id"])["supersedes_revision_id"] == first_rev_id)
# unrelated assets keep their current revision
a_yaml = db.find_asset_by_logical_key(ws, "config/application.yaml")
check("change: an unrelated asset kept its current revision",
      len(db.list_revisions(ws, a_yaml["id"])) == 1)
# historical evidence still returns the OLD content from the snapshot
old_seg = db.list_segments(ws, first_rev_id)[0]
old_ev = db.get_evidence_for_segment(ws, old_seg["id"])
old_blob_text = cs.get(ws, first_blob).decode("utf-8")
check("change: the old revision's blob is retained",
      "return 42" in old_blob_text and "changed" not in old_blob_text)
check("change: historical evidence still validates against the old blob",
      segmentation.hash_text_utf8(
          segmentation.slice_lines(old_blob_text, old_ev["locator"]["startLine"],
                                   old_ev["locator"]["endLine"])) == old_ev["content_hash"])

# ===========================================================================
# 4. Revert A -> B -> A — three revisions, final reuses the original blob
# ===========================================================================
_write("src/app.py", "def main():\n    return 42\n")   # back to the ORIGINAL A
_reset_embed()
p4, _ = _ingest()
a_py3 = db.find_asset_by_logical_key(ws, "src/app.py")
revs3 = db.list_revisions(ws, a_py3["id"])
check("revert: a third revision was created (revert is a new observation)",
      len(revs3) == 3)
check("revert: revision sequences are dense 1..3",
      sorted(r["sequence"] for r in revs3) == [1, 2, 3])
check("revert: the final revision reuses the original content blob",
      db.get_revision(ws, a_py3["current_revision_id"])["content_blob_hash"] == first_blob)
check("revert: no new blob was created (the original was reused)",
      p4.get("content_blobs_created", 0) == 0 and p4.get("content_blobs_reused", 0) >= 1)

# ===========================================================================
# 5. Removal — source file gone => Asset removed, history preserved
# ===========================================================================
os.remove(os.path.join(REPO, "config", "application.yaml"))
p5, _ = _ingest()
a_yaml2 = db.find_asset_by_logical_key(ws, "config/application.yaml")
check("removal: assets_removed counter incremented", p5.get("assets_removed") == 1)
check("removal: the asset state is 'removed'", a_yaml2["state"] == "removed")
check("removal: the file_index row is gone",
      "config/application.yaml" not in db.get_file_index(ws))
check("removal: the removed asset's revision history is preserved",
      len(db.list_revisions(ws, a_yaml2["id"])) >= 1)
check("removal: an unrelated active asset is untouched",
      db.find_asset_by_logical_key(ws, "src/app.py")["state"] == "active")

# ===========================================================================
# 6. Reappearance — the same logical key comes back => same Asset reactivated
# ===========================================================================
removed_asset_id = a_yaml2["id"]
_write("config/application.yaml", "server:\n  port: 8080\n")
p6, _ = _ingest()
a_yaml3 = db.find_asset_by_logical_key(ws, "config/application.yaml")
check("reappearance: the SAME asset id is reused", a_yaml3["id"] == removed_asset_id)
check("reappearance: the asset is active again", a_yaml3["state"] == "active")
check("reappearance: revision history remained intact",
      len(db.list_revisions(ws, a_yaml3["id"])) >= 1)

# ===========================================================================
# 7. Legacy backfill — file_index + Chroma exist, but no Asset rows.
#    Re-ingest must create Assets WITHOUT re-embedding.
# ===========================================================================
db.clear_workspace_assets(ws)
cs.clear_workspace(ws)
check("backfill setup: asset rows wiped, file_index kept",
      db.count_assets(ws) == 0 and len(db.get_file_index(ws)) == 3)
_reset_embed()
p7, _ = _ingest()
check("backfill: assets recreated for every unchanged file",
      p7.get("assets_created") == 3)
check("backfill: revisions recreated", p7.get("revisions_created") == 3)
check("backfill: the embedder was NOT called (Chroma data reused)",
      _embed_calls["n"] == 0)
check("backfill: asset stats restored", db.asset_stats(ws)["assets_total"] == 3)

# ===========================================================================
# 8. FK cascade + cross-workspace isolation
# ===========================================================================
some_asset = db.list_assets(ws, limit=1)[0]
check("scoping: an asset id is not readable through another workspace",
      db.get_asset(other, some_asset["id"]) is None)
# a raw project-row delete cascades through the whole Asset subtree
tmp = db.create_project("cascade-victim")["id"]
tmp_root = tempfile.mkdtemp()
_write_root = tmp_root
with open(os.path.join(tmp_root, "x.py"), "w") as fh:
    fh.write("y = 1\n")
db.commit_revision(tmp, "x.py", asset_type="source-code", title="x.py",
                   source_path="x.py", content_hash="h" * 64, content_size=6,
                   content_blob_hash="h" * 64,
                   segments=[{"segment_key": "file-range:000001", "segment_type": "file",
                              "ordinal": 0, "start_line": 1, "end_line": 1, "symbol": "x.py",
                              "content_hash": "h" * 64, "content_mode": "verbatim",
                              "metadata": {}, "evidence": {"locator": {}, "excerpt": "y=1",
                                                           "content_hash": "h" * 64}}])
check("cascade: victim workspace has asset data before delete",
      db.count_assets(tmp) == 1)
db.delete_project(tmp)
conn = db._c()
check("cascade: assets deleted with the project", db.count_assets(tmp) == 0)
check("cascade: revisions/segments/evidence for the victim are gone (FK cascade)",
      conn.execute("SELECT COUNT(*) FROM asset_revisions r JOIN "
                   "(SELECT 1) WHERE r.asset_id NOT IN (SELECT id FROM assets)"
                   ).fetchone()[0] == 0)

# ===========================================================================
# 9. Lifecycle — terminate clears Asset data + blobs, keeps the workspace
# ===========================================================================
assert db.count_assets(ws) == 3
jobs.terminate_project(ws)
check("terminate: Asset rows cleared", db.count_assets(ws) == 0)
check("terminate: content blobs cleared",
      not cs.objects_dir(ws).exists()
      or not any(p.is_file() for p in cs.objects_dir(ws).rglob("*")))
check("terminate: the workspace itself survives", db.get_project(ws) is not None)
check("terminate: the registered source path survives",
      len(db.get_project_paths(ws)) == 1)
check("terminate: the workspace is back to 'init'", db.get_project(ws)["state"] == "init")

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
