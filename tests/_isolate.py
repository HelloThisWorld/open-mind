"""Test bootstrap: import this FIRST — before any ``openmind`` import.

Forces an isolated data dir so a bare ``python tests/verify_x.py`` can NEVER
touch the live ``./data`` dir. (Twice now an un-isolated run has overwritten
the live model config with the test stub (port:1), left test projects in the
live registry, and opened the live Chroma store from a second process.)
An explicit OPENMIND_DATA_DIR from the runner is honored — unless it points
at the repo's live data dir, in which case it is replaced with a temp dir.

Also defaults tests to CPU-only, fully-offline embeddings (the deterministic
hashing embedder) and no enrichment egress, so suites are fast, deterministic,
and never touch the GPU or the network. A test that needs different settings
can still override AFTER importing this (or set the env before running).
"""
import os
import tempfile
from pathlib import Path

_LIVE = (Path(__file__).resolve().parent.parent / "data").resolve()
_cur = os.environ.get("OPENMIND_DATA_DIR", "")
try:
    _points_live = bool(_cur) and Path(_cur).resolve() == _LIVE
except OSError:
    _points_live = True   # unresolvable -> don't trust it
if not _cur or _points_live:
    os.environ["OPENMIND_DATA_DIR"] = tempfile.mkdtemp(prefix="om_test_")
os.environ.setdefault("OPENMIND_MACHINE_DIR", tempfile.mkdtemp(prefix="om_machine_"))
os.environ.setdefault("OPENMIND_EMBED_DEVICE", "cpu")
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
os.environ.setdefault("OPENMIND_INGEST_FREE_GPU", "0")
os.environ.setdefault("OPENMIND_ENRICH_EGRESS", "0")
