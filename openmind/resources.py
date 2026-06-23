"""Portable system-resource governor for ingest.

A long ingest — especially with a large local model already pinning host RAM and
the GPU — must never drive the machine into an out-of-memory swap-thrash freeze.
This module reads the machine's OWN limits at runtime (via psutil) and the ingest
pipeline keeps a headroom margin expressed as a FRACTION of total RAM plus an
absolute floor, so the SAME code leaves sensible headroom on a 4 GB laptop and a
256 GB server with nothing hardcoded to one machine.

Two thresholds, two behaviours:

  * a COMFORTABLE soft target (fraction of total) — only used to soft-gate
    OPTIONAL work; the core pipeline never blocks on it. When a large resident
    model legitimately holds most of RAM this target is never met, so blocking on
    it would stall every checkpoint forever and make ingest look frozen.
  * a CRITICAL hard floor (a small ABSOLUTE amount) — below this the OS is about
    to swap-thrash the whole box. Only here do we act: gc + a brief bounded wait
    for a transient spike to clear, then raise :class:`MemoryGuardAbort` so the
    job fails cleanly instead of freezing the machine.

psutil is the only dependency; if it is unavailable every guard degrades to a safe
no-op (the app keeps working, just without the headroom protection).
"""
from __future__ import annotations

import gc
import time
from typing import Callable, Optional

from . import config

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - psutil is normally installed
    _HAS_PSUTIL = False

_MB = 1024 * 1024


class MemoryGuardAbort(RuntimeError):
    """Raised to stop a job cleanly when free memory stays critically low — far
    better than letting the OS thrash the whole machine to a freeze."""


def available_bytes() -> Optional[int]:
    if not _HAS_PSUTIL:
        return None
    try:
        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def total_bytes() -> Optional[int]:
    if not _HAS_PSUTIL:
        return None
    try:
        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def min_free_bytes() -> Optional[int]:
    """The comfortable headroom: max(fraction of total, absolute floor)."""
    tot = total_bytes()
    if tot is None:
        return None
    return max(int(tot * config.INGEST_MIN_FREE_FRACTION),
               int(config.INGEST_MIN_FREE_MB) * _MB)


def headroom_ok() -> bool:
    """True if there is comfortable free RAM. True (don't block) when psutil is
    absent. Use this ONLY to soft-gate optional work, never the core pipeline."""
    avail, floor = available_bytes(), min_free_bytes()
    if avail is None or floor is None:
        return True
    return avail >= floor


def critical_floor_bytes() -> Optional[int]:
    """The HARD floor below which continuing risks an OS swap-thrash freeze:
    max(absolute MB floor, a fraction of total). The absolute term dominates by
    design so a big machine whose RAM is legitimately held by a large resident
    model is NOT treated as critical."""
    base = int(config.INGEST_CRITICAL_FREE_MB) * _MB
    tot = total_bytes()
    if tot is None:
        return base
    return max(base, int(tot * config.INGEST_CRITICAL_FREE_FRACTION))


def critically_low() -> bool:
    avail = available_bytes()
    if avail is None:
        return False
    floor = critical_floor_bytes()
    return floor is not None and avail < floor


def _mb(n: Optional[int]) -> Optional[int]:
    return None if n is None else int(n / _MB)


def status_line() -> str:
    avail, tot = available_bytes(), total_bytes()
    if avail is None:
        return "memory guard disabled (psutil unavailable)"
    return (f"{_mb(avail)}MB free of {_mb(tot)}MB "
            f"(soft target {_mb(min_free_bytes())}MB, abort below "
            f"{_mb(critical_floor_bytes())}MB)")


def guard_or_abort(log: Optional[Callable[[str], None]] = None) -> bool:
    """Call inside a hot ingest loop.

    Ingest work is light (parse + CPU-embed; heavy artifacts stream to disk), so it
    cannot exhaust RAM on its own. We therefore ONLY act when free RAM is already
    CRITICALLY low (the machine is about to swap-thrash): gc + a brief bounded wait
    for a transient spike to clear, then raise :class:`MemoryGuardAbort` if it stays
    critical so the job fails cleanly instead of freezing the box.

    We deliberately do NOT block on the softer :func:`headroom_ok` target: when a
    large local model legitimately holds most of RAM that target is never met, and
    pausing at every checkpoint would make ingest look frozen. Below the soft target
    but not critical we PROCEED IMMEDIATELY.

    Returns True (proceed)."""
    if not critically_low():
        return True
    if log:
        log(f"[resources] memory critically low: {status_line()} — "
            f"pausing up to {config.INGEST_MEM_WAIT_S:.0f}s to recover")
    gc.collect()
    deadline = time.time() + config.INGEST_MEM_WAIT_S
    while time.time() < deadline:
        if not critically_low():
            if log:
                log(f"[resources] memory recovered: {status_line()}")
            return True
        time.sleep(1.0)
        gc.collect()
    raise MemoryGuardAbort(
        "ingest stopped to protect the system: free memory stayed critically "
        f"low ({status_line()}). Close other apps, stop the local model, or "
        "ingest fewer paths, then retry.")
