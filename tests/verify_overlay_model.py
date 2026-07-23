"""Overlay model, diff taxonomy, isolation and revision semantics (Phase 7
§6, §11, §13, §14, §32).

Builds a real repo + canonical graph, captures a baseline, builds overlays and
asserts: the change taxonomy is correct; before/after content is snapshotted;
the canonical Base Workspace is NEVER mutated; overlay revision semantics hold;
and an unchanged refresh is a no-op.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _git_helpers import (canonical_counts, check, checkout,  # noqa: E402
                          checkout_new_branch, commit, finish, head_branch,
                          ingest_and_sync, make_workspace, new_repo, write)

from openmind.git.command import default_runner  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402

if not default_runner().available:
    print("git not available; skipping")
    raise SystemExit(0)

rt = get_runtime()

# -- a repo with a diverse feature branch ------------------------------------
repo = new_repo("om_ovl_")
write(repo, "src/Svc.java",
      "class Svc {\n  int run() { return 3000; }\n"
      "  int keep() { return 1; }\n}\n")
write(repo, "config.properties", "timeout=3000\n")
write(repo, "gone.txt", "delete me\n")
write(repo, "old_name.txt", "stable content that will be renamed\n")
write(repo, "data.bin", "bin\x00\x01\x02data\n")
commit(repo, "main baseline")
main = head_branch(repo)

checkout_new_branch(repo, "feature/x")
write(repo, "src/Svc.java",
      "class Svc {\n  int run() { return 5000; }\n"  # method body changed
      "  int keep() { return 1; }\n"                 # unchanged method
      "  int added() { return 2; }\n}\n")            # new method
write(repo, "config.properties", "timeout=5000\n")   # config changed
os.remove(os.path.join(repo, "gone.txt"))            # deleted
os.rename(os.path.join(repo, "old_name.txt"),
          os.path.join(repo, "new_name.txt"))        # pure rename
write(repo, "data.bin", "bin\x00\x09\x08changed\n")  # binary change
write(repo, "added.txt", "brand new file\n")         # added
commit(repo, "feature: diverse changes")
checkout(repo, main)

pid = make_workspace(rt, repo, "ovl-model")
ingest_and_sync(rt, pid, timeout=180)
rt.git.discover_repositories(pid)
cap = rt.git.capture_baseline(pid, actor="fx")
check("baseline captured", cap["ok"], detail=str(cap.get("blocked")))

from openmind import db  # noqa: E402
conn, lock = db.shared_connection()
before = canonical_counts(conn, lock)

# -- build a branch overlay --------------------------------------------------
ovl = rt.overlays.create_overlay(
    pid, kind="branch",
    repositories=[{"repository": "git:.", "base": main,
                   "head": "feature/x", "target_branch": main}],
    name="diverse")
overlay = ovl["overlay"]
oid = overlay["id"]
check("overlay ready", overlay["state"] == "ready", detail=overlay["state"])
check("overlay revision is 1", overlay["overlayRevision"] == 1)

files = rt.overlays.list_overlay_files(pid, oid)["files"]
by_path = {(f["newPath"] or f["oldPath"]): f for f in files}
by_type = {}
for f in files:
    by_type.setdefault(f["changeType"], []).append(f)

check("modified file detected", any(f["changeType"] == "modified"
      and f["newPath"] == "src/Svc.java" for f in files))
check("config modified detected", "config.properties" in by_path
      and by_path["config.properties"]["changeType"] == "modified")
check("deleted file detected", any(f["changeType"] == "deleted"
      and f["oldPath"] == "gone.txt" for f in files))
check("added file detected", any(f["changeType"] == "added"
      and f["newPath"] == "added.txt" for f in files))
rename = [f for f in files if f["changeType"] in ("renamed", "copied")]
check("pure rename detected as rename (not delete+add)",
      any(f["oldPath"] == "old_name.txt" and f["newPath"] == "new_name.txt"
          and f["similarity"] == 100 for f in rename))
binf = by_path.get("data.bin")
check("binary change flagged is_binary", binf is not None and binf["isBinary"])
check("binary file has no false line counts",
      binf is not None and binf["additions"] == 0 and binf["deletions"] == 0)

# -- segments: only changed methods marked, unchanged preserved --------------
svc_file = next(f for f in files if f["newPath"] == "src/Svc.java")
detail = rt.overlays.get_overlay_file(pid, oid, svc_file["id"])
after_segs = [s for s in detail["segments"] if s["side"] == "after"]
changed_symbols = {s["symbol"] for s in after_segs
                   if s["changeClass"] in ("added", "modified")}
unchanged_symbols = {s["symbol"] for s in after_segs
                     if s["changeClass"] == "unchanged"}
check("changed method segment classified modified/added",
      any("run" in s or "added" in s for s in changed_symbols),
      detail=str(changed_symbols))
check("unchanged method not marked modified",
      any("keep" in s for s in unchanged_symbols)
      or not any("keep" in s for s in changed_symbols),
      detail=str(unchanged_symbols))

# -- evidence: modified file has before/after evidence -----------------------
report = rt.overlays.get_impact_report(pid, oid)["report"]
ev = report["evidenceIndex"]
sides = {e["side"] for e in ev}
check("evidence present", len(ev) > 0)
check("before AND after evidence exist", "before" in sides and "after" in sides,
      detail=str(sides))
check("evidence paths are repository-relative (no absolute path)",
      all(not (e["locator"].get("path", "").startswith("/")
               or (len(e["locator"].get("path", "")) > 1
                   and e["locator"]["path"][1] == ":")) for e in ev))

# -- ISOLATION: canonical row counts unchanged -------------------------------
after = canonical_counts(conn, lock)
drift = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
check("canonical Assets/Revisions/Segments/Evidence unchanged",
      before["assets"] == after["assets"]
      and before["asset_revisions"] == after["asset_revisions"]
      and before["segments"] == after["segments"]
      and before["evidence"] == after["evidence"])
check("canonical graph tables unchanged",
      before["engineering_entities"] == after["engineering_entities"]
      and before["engineering_claims"] == after["engineering_claims"]
      and before["engineering_relations"] == after["engineering_relations"])
check("canonical trace/gap/conflict tables unchanged",
      before["trace_paths"] == after["trace_paths"]
      and before["traceability_gaps"] == after["traceability_gaps"]
      and before["engineering_conflicts"] == after["engineering_conflicts"])
check("no Knowledge Revision minted by overlay",
      before["knowledge_revisions"] == after["knowledge_revisions"])
check("overall canonical drift is empty", not drift, detail=str(drift))

# -- revision semantics: unchanged refresh is a no-op ------------------------
ref = rt.overlays.refresh_overlay(pid, oid)
check("unchanged refresh is a no-op", ref.get("refreshed") is False,
      detail=str(ref.get("reason")))
again = rt.overlays.get_overlay(pid, oid)["overlay"]
check("no-op refresh did not bump overlay revision",
      again["overlayRevision"] == 1, detail=str(again["overlayRevision"]))

# -- deterministic report hash -----------------------------------------------
h1 = rt.overlays.get_impact_report(pid, oid)["reportHash"]
h2 = rt.overlays.get_impact_report(pid, oid)["reportHash"]
check("report hash is stable for identical state", h1 == h2)

# -- overlay delete removes only overlay data --------------------------------
pre_delete = canonical_counts(conn, lock)
res = rt.overlays.delete_overlay(pid, oid)
check("overlay deleted", res["deleted"])
post_delete = canonical_counts(conn, lock)
check("deleting overlay did not touch canonical data",
      pre_delete == post_delete)
with lock:
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM git_overlay_files WHERE overlay_id=?",
        (oid,)).fetchone()["c"]
check("overlay files cascade-deleted", remaining == 0)

raise SystemExit(finish("verify_overlay_model"))
