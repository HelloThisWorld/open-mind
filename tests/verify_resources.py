"""Acceptance test for the ingest resource governor (openmind/resources.py).

The headroom policy must scale to the machine (fraction of total RAM + floor); the
guard must ABORT (not freeze) when memory stays critically low; it must PROCEED
INSTANTLY when below the comfortable soft target but not critical (a large resident
model keeps free RAM under the soft target forever — stalling there is what makes
ingest look frozen); and it must degrade to a safe no-op when psutil is absent.

Run:  python tests/verify_resources.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openmind import config, resources  # noqa: E402

config.INGEST_MEM_WAIT_S = 0  # don't actually sleep in the guard during the test

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    ok = bool(cond)
    print(("PASS" if ok else "FAIL"), "-", name, ("" if ok else f"   >> {detail}"))
    _passed += ok
    _failed += (not ok)


def _sim(total_gb, avail_gb):
    resources.total_bytes = lambda: int(total_gb * 1e9)
    resources.available_bytes = lambda: int(avail_gb * 1e9)


GB = 1e9

# --- headroom scales with the machine (fraction OR floor, whichever is larger) ---
_sim(4, 4)
check("small box: floor dominates (1GB > 15% of 4GB)",
      resources.min_free_bytes() == 1024 * 1024 * 1024)
_sim(100, 100)
check("big box: fraction dominates (15% of 100GB)",
      abs(resources.min_free_bytes() - 15 * GB) < 1e8)

# --- headroom_ok / critically_low ---
_sim(32, 10)
check("32GB box, 10GB free -> headroom OK (>15%=4.8GB)", resources.headroom_ok())
_sim(32, 3)
check("32GB box, 3GB free -> below soft target", not resources.headroom_ok())
check("3GB free is NOT critical", not resources.critically_low())
# THE regression guard: a big machine whose RAM is legitimately held by a large
# resident model still leaves only ~1GB free — that must NOT count as 'critical',
# or every ingest aborts immediately (the hard floor is a small ABSOLUTE).
_sim(32, 1)
check("32GB box, 1GB free -> NOT critical (hard floor is absolute)",
      not resources.critically_low())
check("critical floor is a fixed ~300MB regardless of machine size",
      resources.critical_floor_bytes() == 300 * 1024 * 1024)
_sim(256, 1)
check("256GB box, 1GB free -> still NOT critical (no fraction blow-up)",
      not resources.critically_low())
_sim(32, 0.2)
check("0.2GB free -> critically low (<300MB)", resources.critically_low())

# --- guard_or_abort: raises ONLY when critically low and unrecoverable ---
_sim(32, 10)
try:
    resources.guard_or_abort()
    check("guard_or_abort returns quietly when there is headroom", True)
except Exception as e:
    check("guard_or_abort returns quietly when there is headroom", False, str(e))
_sim(32, 0.2)
try:
    resources.guard_or_abort()
    check("guard_or_abort ABORTS when critically low", False, "did not raise")
except resources.MemoryGuardAbort:
    check("guard_or_abort ABORTS when critically low", True)

# --- below the soft target but not critical -> PROCEED INSTANTLY (no stall) ---
config.INGEST_MEM_WAIT_S = 20.0   # a proceed must NOT consume this
_sim(32, 3)
t0 = time.time()
ok = resources.guard_or_abort()
dt = time.time() - t0
check("below soft target but not critical -> proceeds instantly (no 20s stall)",
      ok is True and dt < 1.0, f"returned {ok} in {dt:.1f}s")
config.INGEST_MEM_WAIT_S = 0

# --- psutil-absent: every guard is a safe no-op ---
resources.available_bytes = lambda: None
resources.total_bytes = lambda: None
check("no psutil -> headroom_ok True (no-op)", resources.headroom_ok())
check("no psutil -> not critically_low", not resources.critically_low())
check("no psutil -> guard_or_abort does not raise", resources.guard_or_abort() is True)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
