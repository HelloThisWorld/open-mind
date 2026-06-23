"""Acceptance test for the "link source" feature (no local copy -> link the project
to its source via a local folder or GitHub, the latter fetched on demand + audited).

Covers, with NO live network:
  * parse_github_url over every paste form a user might use, and the raw-URL builder;
  * machine-local storage round-trip — set/get/clear github, set_local_root re-points
    the root, set_paths PRESERVES a github link, forget clears EVERYTHING;
  * local resolution mirrors what GET /source does (root + relativize + read);
  * netguard egress POLICY — raw.githubusercontent.com is reachable ONLY via the
    dedicated guarded path AND only when OPENMIND_SOURCELINK_EGRESS=1; any other host
    is refused; every attempt (allow or block) is audited.

Run:  python tests/verify_source_link.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate ALL machine/data state in temp dirs BEFORE importing openmind: config and
# machine read these env vars at import time. Never touch the live config (the
# isolate-test-datadir rule).
_TMP = tempfile.mkdtemp(prefix="openmind_linktest_")
os.environ["OPENMIND_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["OPENMIND_MACHINE_DIR"] = os.path.join(_TMP, "machine")

from openmind import config, machine, netguard, walker  # noqa: E402

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    ok = bool(cond)
    print(("PASS" if ok else "FAIL"), "-", name, ("" if ok else f"   >> {detail}"))
    _passed += ok
    _failed += (not ok)


PID = "proj-test"

# --- parse_github_url: the common paste forms all normalize to owner/repo/ref ------
_EXPECT = {"owner": "apache", "repo": "kafka", "ref": "HEAD",
           "url": "https://github.com/apache/kafka"}
check("https URL", machine.parse_github_url("https://github.com/apache/kafka") == _EXPECT)
check("trailing .git", machine.parse_github_url("https://github.com/apache/kafka.git")["repo"] == "kafka")
check("/tree/<branch> captures ref",
      machine.parse_github_url("https://github.com/apache/kafka/tree/trunk")["ref"] == "trunk")
check("/blob/<branch>/<path> captures ref",
      machine.parse_github_url("https://github.com/apache/kafka/blob/3.7/core/Foo.scala")["ref"] == "3.7")
check("scp git remote", machine.parse_github_url("git@github.com:apache/kafka.git") == _EXPECT)
check("bare owner/repo", machine.parse_github_url("apache/kafka")["owner"] == "apache")
check("www. host stripped", machine.parse_github_url("https://www.github.com/apache/kafka")["repo"] == "kafka")
for bad in ("", "   ", "https://github.com/apache", "garbage"):
    try:
        machine.parse_github_url(bad)
        check(f"reject {bad!r}", False, "did not raise")
    except ValueError:
        check(f"reject {bad!r}", True)

# --- github_raw_url: default HEAD + explicit ref, leading slash stripped -----------
_gh = machine.parse_github_url("https://github.com/apache/kafka")
check("raw url defaults to HEAD",
      machine.github_raw_url(_gh, "core/src/Foo.scala")
      == "https://raw.githubusercontent.com/apache/kafka/HEAD/core/src/Foo.scala")
_gh2 = machine.parse_github_url("https://github.com/apache/kafka/tree/trunk")
check("raw url honors ref + strips leading slash",
      machine.github_raw_url(_gh2, "/a/b.java")
      == "https://raw.githubusercontent.com/apache/kafka/trunk/a/b.java")

# --- storage round-trip: set/get/clear; set_paths preserves the github link --------
machine.set_github(PID, "https://github.com/apache/kafka", "trunk")
check("get_github round-trips", (machine.get_github(PID) or {}).get("ref") == "trunk")
try:
    machine.set_github(PID, "garbage")
    check("set_github rejects an unparseable url", False, "did not raise")
except ValueError:
    check("set_github rejects an unparseable url", True)
machine.set_local_root(PID, _TMP)   # re-point the local root...
check("project_root resolves after set_local_root", machine.project_root(PID) == walker.norm(_TMP))
check("set_local_root PRESERVES the github link", machine.get_github(PID) is not None)
machine.clear_github(PID)
check("clear_github removes the link but keeps the local root",
      machine.get_github(PID) is None and machine.project_root(PID) == walker.norm(_TMP))
machine.forget(PID)
check("forget clears EVERYTHING (paths + github)",
      machine.project_root(PID) == "" and machine.get_github(PID) is None)

# --- local resolution mirrors what GET /source does (root + relativize + read) -----
_src = os.path.join(_TMP, "repo")
os.makedirs(os.path.join(_src, "pkg"), exist_ok=True)
with open(os.path.join(_src, "pkg", "Hello.java"), "w", encoding="utf-8") as fh:
    fh.write("class Hello {}\n")
machine.set_local_root(PID, _src)
_abs = machine.absolutize("pkg/Hello.java", machine.project_root(PID))
check("a relative source re-anchors to the linked local root + reads",
      walker.read_text(_abs).strip() == "class Hello {}")
machine.forget(PID)

# --- netguard egress policy: source-link host reachable ONLY via the dedicated path
_RAW = "https://raw.githubusercontent.com/apache/kafka/HEAD/x.txt"
try:
    netguard.assert_local(_RAW)
    check("raw.githubusercontent blocked on the local-only path", False, "did not raise")
except netguard.ExfiltrationBlocked:
    check("raw.githubusercontent blocked on the local-only path", True)

config.SOURCELINK_EGRESS = True
try:
    netguard.assert_reachable(_RAW, allow_sourcelink=True)
    check("allowed via allow_sourcelink when egress is on", True)
except netguard.ExfiltrationBlocked as e:
    check("allowed via allow_sourcelink when egress is on", False, str(e))

try:
    netguard.assert_reachable("https://evil.example.com/x", allow_sourcelink=True)
    check("a non-allowlisted host is refused on the source-link path", False, "did not raise")
except netguard.ExfiltrationBlocked:
    check("a non-allowlisted host is refused on the source-link path", True)

config.SOURCELINK_EGRESS = False   # kill-switch: even the dedicated path is blocked
try:
    netguard.assert_reachable(_RAW, allow_sourcelink=True)
    check("OPENMIND_SOURCELINK_EGRESS=0 blocks the fetch", False, "did not raise")
except netguard.ExfiltrationBlocked:
    check("OPENMIND_SOURCELINK_EGRESS=0 blocks the fetch", True)
config.SOURCELINK_EGRESS = True

_log = netguard.get_log(50)
check("every egress attempt is audited (allowed or blocked)",
      any(e.get("host") == "raw.githubusercontent.com" for e in _log))

shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
