"""Change Impact Packet Draft: determinism, hashes, referential integrity and
verifier (Phase 7 §30).

Builds a small overlay, exports a packet, and asserts: deterministic file set,
SHA-256 file hashes that the standalone verifier accepts, no absolute paths, no
author emails, and that the verifier CATCHES a tampered file and a missing
evidence reference. Also asserts the canonical Knowledge Bundle stays
2.0.0-draft.2 (the packet is separate).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _git_helpers import (check, checkout, checkout_new_branch, commit,  # noqa: E402
                          finish, head_branch, ingest_and_sync, make_workspace,
                          new_repo, write)

from openmind.git.command import default_runner  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402
from openmind.impact_verify import verify_packet  # noqa: E402

if not default_runner().available:
    print("git not available; skipping")
    raise SystemExit(0)

rt = get_runtime()
repo = new_repo("om_pkt_")
write(repo, "svc.java", "class S {\n int f() { return 3000; }\n}\n")
write(repo, "conf.properties", "timeout=3000\n")
commit(repo, "base")
main = head_branch(repo)
checkout_new_branch(repo, "feat")
write(repo, "svc.java", "class S {\n int f() { return 5000; }\n int g(){return 1;} }\n")
write(repo, "conf.properties", "timeout=5000\n")
commit(repo, "feat change")
checkout(repo, main)

pid = make_workspace(rt, repo, "pkt")
ingest_and_sync(rt, pid, timeout=180)
rt.git.discover_repositories(pid)
rt.git.capture_baseline(pid, actor="fx")
ovl = rt.overlays.create_overlay(
    pid, kind="branch",
    repositories=[{"repository": "git:.", "base": main, "head": "feat",
                   "target_branch": main}], name="pkt")
oid = ovl["overlay"]["id"]

out1 = tempfile.mkdtemp(prefix="pkt1_")
out2 = tempfile.mkdtemp(prefix="pkt2_")
p1 = rt.overlays.export_impact_packet(pid, oid, out1)
p2 = rt.overlays.export_impact_packet(pid, oid, out2)

# -- required files present --------------------------------------------------
required = ["manifest.json", "summary.json", "report.md", "repositories.jsonl",
            "commits.jsonl", "file-changes.jsonl", "segment-changes.jsonl",
            "entity-deltas.jsonl", "relation-deltas.jsonl",
            "requirement-impact.jsonl", "test-impact.jsonl",
            "trace-impact.jsonl", "gap-impact.jsonl", "conflict-impact.jsonl",
            "evidence.jsonl"]
for name in required:
    check(f"packet has {name}", os.path.isfile(os.path.join(out1, name)))

# -- determinism: identical content hashes for the data files ----------------
def read(path):
    with open(path, "rb") as fh:
        return fh.read()

deterministic = all(
    read(os.path.join(out1, n)) == read(os.path.join(out2, n))
    for n in required if n != "manifest.json")
check("packet data files are byte-identical across exports", deterministic)

# -- manifest fields ---------------------------------------------------------
manifest = json.load(open(os.path.join(out1, "manifest.json")))
check("manifest schema is 1.0.0-draft.1",
      manifest["schemaVersion"] == "1.0.0-draft.1")
check("manifest records runtime 1.7.0-dev",
      manifest["runtimeVersion"] == "1.7.0-dev")
check("manifest has file hashes", bool(manifest["fileHashes"]))
check("manifest declares partial state explicitly", "partial" in manifest)

# -- no absolute paths, no author emails -------------------------------------
blob = ""
for n in required:
    blob += read(os.path.join(out1, n)).decode("utf-8", "replace")
check("no absolute POSIX path in packet", "/home/" not in blob
      and "/Users/" not in blob)
check("no windows drive path in packet",
      ":\\" not in blob and ":/Users" not in blob.replace("\\", "/"))
check("no author email in packet", "@example.test" not in blob)

# -- verifier accepts a good packet ------------------------------------------
ok, summary = verify_packet(out1)
check("verifier accepts a good packet", ok, detail=str(summary.get("errors")))

# -- verifier catches a tampered file ----------------------------------------
tampered = os.path.join(out1, "file-changes.jsonl")
with open(tampered, "a", encoding="utf-8") as fh:
    fh.write('{"changeType":"forged"}\n')
ok2, summary2 = verify_packet(out1)
check("verifier catches a hash mismatch", not ok2,
      detail=str(summary2.get("errors")))

# -- verifier catches a missing evidence reference ---------------------------
out3 = tempfile.mkdtemp(prefix="pkt3_")
rt.overlays.export_impact_packet(pid, oid, out3)
# inject a trace impact row that references a non-existent evidence id, and
# re-hash it into the manifest so ONLY the referential check should fail.
trace_path = os.path.join(out3, "trace-impact.jsonl")
with open(trace_path, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"rootRequirementId": "e_x", "impactType": "trace-broken",
                         "evidenceIds": ["oev_missing"]}) + "\n")
import hashlib
man = json.load(open(os.path.join(out3, "manifest.json")))
with open(trace_path, "rb") as fh:
    man["fileHashes"]["trace-impact.jsonl"] = hashlib.sha256(fh.read()).hexdigest()
with open(os.path.join(out3, "manifest.json"), "w", encoding="utf-8") as fh:
    json.dump(man, fh, sort_keys=True, indent=2)
ok3, summary3 = verify_packet(out3)
check("verifier catches a dangling evidence reference", not ok3,
      detail=str(summary3.get("errors")))

# -- canonical Bundle stays draft.2 (packet is separate) ---------------------
from openmind.knowledge import bundle  # noqa: E402
bv = getattr(bundle, "BUNDLE_SCHEMA_VERSION", None) or \
    getattr(bundle, "SCHEMA_VERSION", None)
check("canonical Knowledge Bundle remains 2.0.0-draft.2",
      bv == "2.0.0-draft.2", detail=str(bv))

raise SystemExit(finish("verify_impact_packet"))
