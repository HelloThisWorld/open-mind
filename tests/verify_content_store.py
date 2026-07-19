"""Immutable content-addressed blob store — atomic write, blob reuse,
SHA-256 identity, corruption detection, binary round-trip, workspace cleanup.

Pure stdlib + the content store: no embeddings, no vector store, no FastAPI, no
database. Proves the snapshot store is testable in isolation (invariant 9).
"""
import hashlib
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from openmind import config, content_store as cs  # noqa: E402
from openmind.domain.errors import ContentCorruption  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


WS = "p_cstore00test"

# ---------------------------------------------------------------------------
# 1. Write + SHA-256 identity
# ---------------------------------------------------------------------------
data = b"class Client { void send(Request r) {} }"
h = cs.put(WS, data)
check("put returns the SHA-256 hex of the exact bytes",
      h == hashlib.sha256(data).hexdigest())
check("the returned hash is 64 hex chars", len(h) == 64 and all(c in "0123456789abcdef" for c in h))
check("hash_bytes agrees with put", cs.hash_bytes(data) == h)

# ---------------------------------------------------------------------------
# 2. Round-trip + presence
# ---------------------------------------------------------------------------
check("get returns the exact bytes", cs.get(WS, h) == data)
check("exists is True for a stored blob", cs.exists(WS, h) is True)
check("verify is True for an intact blob", cs.verify(WS, h) is True)
check("exists is False for an unknown hash", cs.exists(WS, "0" * 64) is False)

# ---------------------------------------------------------------------------
# 3. Blob reuse: identical content writes nothing new
# ---------------------------------------------------------------------------
blob_path = cs.objects_dir(WS) / h[:2] / h
mtime_before = blob_path.stat().st_mtime_ns
h2 = cs.put(WS, data)
check("re-putting identical content returns the same hash", h2 == h)
check("re-putting identical content does not rewrite the blob",
      blob_path.stat().st_mtime_ns == mtime_before)
n_objects_1 = sum(1 for p in cs.objects_dir(WS).rglob("*") if p.is_file())
cs.put(WS, data)
n_objects_2 = sum(1 for p in cs.objects_dir(WS).rglob("*") if p.is_file())
check("re-putting identical content adds no new object file", n_objects_1 == n_objects_2)

# ---------------------------------------------------------------------------
# 4. Changed content creates a new blob
# ---------------------------------------------------------------------------
data_b = b"class Client { void send(Request r) { log(); } }"
hb = cs.put(WS, data_b)
check("changed content produces a different hash", hb != h)
check("both blobs coexist (old one is retained)",
      cs.exists(WS, h) and cs.exists(WS, hb))
check("the new blob round-trips independently", cs.get(WS, hb) == data_b)

# ---------------------------------------------------------------------------
# 5. Atomicity: no temp files linger after a write
# ---------------------------------------------------------------------------
leftovers = [p.name for p in cs.objects_dir(WS).rglob("*")
             if p.is_file() and p.name.endswith(".tmp")]
check("no temp files remain after atomic writes", leftovers == [])

# ---------------------------------------------------------------------------
# 6. Binary bytes round-trip (Phase 2 may store what it cannot parse)
# ---------------------------------------------------------------------------
binary = bytes(range(256)) * 4
hbin = cs.put(WS, binary)
check("binary content is stored by its SHA-256",
      hbin == hashlib.sha256(binary).hexdigest())
check("binary content round-trips byte-for-byte", cs.get(WS, hbin) == binary)

# ---------------------------------------------------------------------------
# 7. Corruption is detected explicitly, never returned silently
# ---------------------------------------------------------------------------
tampered_path = cs.objects_dir(WS) / hb[:2] / hb
tampered_path.write_bytes(b"TAMPERED")
check("verify is False for a corrupt blob", cs.verify(WS, hb) is False)
raised = False
try:
    cs.get(WS, hb)
except ContentCorruption:
    raised = True
check("get raises ContentCorruption on a hash mismatch", raised)

missing_raised = False
try:
    cs.get(WS, "a" * 64)
except ContentCorruption:
    missing_raised = True
check("get raises ContentCorruption for a missing blob", missing_raised)

# ---------------------------------------------------------------------------
# 8. No absolute path leaks into the identity (the DB stores only the hash)
# ---------------------------------------------------------------------------
check("the blob identity is a bare hash, not a path",
      "/" not in h and "\\" not in h and os.sep not in h)
check("blobs live under the workspace data dir (removed with it on delete)",
      str(cs.objects_dir(WS)).startswith(str(config.project_dir(WS))))

# ---------------------------------------------------------------------------
# 9. Workspace cleanup removes every blob
# ---------------------------------------------------------------------------
cs.clear_workspace(WS)
check("clear_workspace removes the objects tree", not cs.objects_dir(WS).exists())
check("a cleared blob no longer exists", cs.exists(WS, h) is False)

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
