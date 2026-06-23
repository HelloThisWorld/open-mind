"""Live machine-load snapshot (CPU / RAM / GPU-VRAM) for the UI status widget.

Every reading is taken on THIS machine only — there is no network egress here
(Invariant 1). CPU/RAM come from ``psutil`` (a graceful fallback applies when it
is not installed). GPU/VRAM is detected VENDOR-AGNOSTICALLY, best-effort, in
priority order:

  1. ``nvidia-smi``            (NVIDIA — util + VRAM used/total)
  2. ``rocm-smi``             (AMD ROCm, mostly Linux — VRAM used/total + util)
  3. Windows perf counters    (any vendor — ``GPU Adapter Memory \\ Dedicated
                               Usage`` for live used + the registry
                               ``qwMemorySize`` for true total)

If none of these yields anything, ``gpus`` is empty and the UI shows an honest
"no GPU detected" / "VRAM unavailable" — a number is NEVER fabricated. The GPU
probe can spawn a subprocess, so its result is cached briefly to keep the
frequently-polled snapshot cheap.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

try:  # psutil is optional — the snapshot degrades gracefully without it.
    import psutil  # type: ignore
    # Prime the non-blocking sampler so the FIRST real read returns a delta
    # (psutil.cpu_percent(interval=None) reports usage since the previous call).
    psutil.cpu_percent(interval=None)
except Exception:  # pragma: no cover - environment-dependent
    psutil = None  # type: ignore

_GB = 1024 ** 3       # bytes  -> GiB
_MIB_GB = 1024.0      # MiB    -> GiB


def _round(x: Optional[float], n: int = 1) -> Optional[float]:
    return None if x is None else round(float(x), n)


def _cpu() -> Dict[str, Any]:
    cores = os.cpu_count() or 0
    if psutil is None:
        return {"available": False, "percent": None, "cores": cores}
    try:
        return {"available": True,
                "percent": _round(psutil.cpu_percent(interval=None)),
                "cores": cores}
    except Exception:
        return {"available": False, "percent": None, "cores": cores}


def _ram() -> Dict[str, Any]:
    if psutil is None:
        return {"available": False, "percent": None,
                "used_gb": None, "total_gb": None}
    try:
        vm = psutil.virtual_memory()
        return {"available": True,
                "percent": _round(vm.percent),
                "used_gb": _round((vm.total - vm.available) / _GB),
                "total_gb": _round(vm.total / _GB)}
    except Exception:
        return {"available": False, "percent": None,
                "used_gb": None, "total_gb": None}


def _vram_pct(used: Optional[float], total: Optional[float]) -> Optional[float]:
    return round(used / total * 100, 1) if (used and total) else None


def _gpus_nvidia() -> List[Dict[str, Any]]:
    """NVIDIA GPUs via nvidia-smi; empty list when unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    proc = subprocess.run(
        [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=2.0,
    )
    if proc.returncode != 0:
        return []
    gpus: List[Dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            util = float(parts[1])
            used = float(parts[2]) / _MIB_GB
            total = float(parts[3]) / _MIB_GB
        except ValueError:
            continue
        gpus.append({"name": parts[0], "util_percent": _round(util),
                     "vram_used_gb": _round(used), "vram_total_gb": _round(total),
                     "vram_percent": _vram_pct(used, total), "source": "nvidia-smi"})
    return gpus


def _gpus_rocm() -> List[Dict[str, Any]]:
    """AMD GPUs via rocm-smi (ROCm, mostly Linux). JSON output is parsed
    leniently because key names vary across ROCm versions."""
    exe = shutil.which("rocm-smi")
    if not exe:
        return []
    proc = subprocess.run(
        [exe, "--showproductname", "--showmeminfo", "vram", "--showuse", "--json"],
        capture_output=True, text=True, timeout=2.5,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    data = json.loads(proc.stdout)
    gpus: List[Dict[str, Any]] = []
    for card, info in data.items():
        if not isinstance(info, dict) or not card.lower().startswith("card"):
            continue
        name = (info.get("Card series") or info.get("Card model")
                or info.get("Card SKU") or "AMD GPU")
        used = total = util = None
        for k, v in info.items():
            kl = k.lower()
            try:
                if "vram total memory" in kl:
                    total = float(v) / _GB
                elif "vram total used memory" in kl or kl.endswith("vram used"):
                    used = float(v) / _GB
                elif "gpu use (%)" in kl:
                    util = float(v)
            except (TypeError, ValueError):
                continue
        if used is None and total is None:
            continue
        gpus.append({"name": name, "util_percent": _round(util),
                     "vram_used_gb": _round(used), "vram_total_gb": _round(total),
                     "vram_percent": _vram_pct(used, total), "source": "rocm-smi"})
    return gpus


# PowerShell probe: vendor-agnostic live dedicated VRAM usage (perf counter) +
# true total VRAM (registry qwMemorySize, which is 64-bit and NOT subject to the
# Win32_VideoController.AdapterRAM 32-bit cap that under-reports >4 GiB cards).
_WIN_GPU_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$name=(Get-CimInstance Win32_VideoController | Where-Object { $_.Name } | Select-Object -First 1 -ExpandProperty Name)
$reg=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*' |
  Where-Object { $_.'HardwareInformation.qwMemorySize' } |
  Sort-Object { [int64]$_.'HardwareInformation.qwMemorySize' } -Descending | Select-Object -First 1
$total=$null; if ($reg) { $total=[int64]$reg.'HardwareInformation.qwMemorySize'; if (-not $name) { $name=$reg.DriverDesc } }
$used=$null
try { $c=Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction Stop
      $used=[int64](($c.CounterSamples | Measure-Object -Property CookedValue -Maximum).Maximum) } catch {}
[pscustomobject]@{ name=$name; total=$total; used=$used } | ConvertTo-Json -Compress
"""


def _gpus_windows() -> List[Dict[str, Any]]:
    """Any-vendor GPU on Windows via performance counters + registry (no NVIDIA
    or ROCm tools required). Reports whatever it can; honest partial info is fine
    (e.g. used without total) and never fabricated."""
    if platform.system() != "Windows":
        return []
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return []
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", _WIN_GPU_PS],
        capture_output=True, text=True, timeout=6.0,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return []
    info = json.loads(out)
    total = info.get("total")
    used = info.get("used")
    total_gb = _round(float(total) / _GB) if total else None
    used_gb = _round(float(used) / _GB) if used not in (None, "", 0) else None
    if used_gb is None and total_gb is None:
        return []
    return [{"name": info.get("name") or "GPU", "util_percent": None,
             "vram_used_gb": used_gb, "vram_total_gb": total_gb,
             "vram_percent": _vram_pct(used_gb, total_gb), "source": "windows-counter"}]


def _detect_gpus() -> List[Dict[str, Any]]:
    """First detector that yields a GPU wins (NVIDIA -> ROCm -> Windows)."""
    for detector in (_gpus_nvidia, _gpus_rocm, _gpus_windows):
        try:
            gpus = detector()
        except Exception:
            gpus = []
        if gpus:
            return gpus
    return []


# The GPU probe spawns a subprocess (nvidia-smi / rocm-smi / PowerShell) that can
# take ~1-2s. To keep the frequently-polled snapshot instant, the probe is run on
# a background thread and its result cached; ``snapshot()`` NEVER blocks on it —
# it returns the last reading immediately and refreshes when the cache goes stale.
_GPU_TTL = 5.0
_gpu_cache: Dict[str, Any] = {"t": -1e9, "data": [], "probed": False}
_gpu_lock = threading.Lock()
_gpu_refreshing = False


def _refresh_gpus() -> None:
    global _gpu_refreshing
    try:
        data = _detect_gpus()
        _gpu_cache["data"] = data
        _gpu_cache["t"] = time.monotonic()
        _gpu_cache["probed"] = True
    finally:
        _gpu_refreshing = False


def _gpus() -> List[Dict[str, Any]]:
    """Return the cached GPU reading immediately, kicking off a non-blocking
    background refresh when it has gone stale."""
    global _gpu_refreshing
    if time.monotonic() - _gpu_cache["t"] >= _GPU_TTL:
        with _gpu_lock:
            if not _gpu_refreshing:
                _gpu_refreshing = True
                threading.Thread(target=_refresh_gpus, name="sysload-gpu",
                                 daemon=True).start()
    return _gpu_cache["data"]


def snapshot() -> Dict[str, Any]:
    """One non-blocking machine-load reading for the UI to poll."""
    gpus = _gpus()
    cpu = _cpu()
    ram = _ram()
    return {
        "available": bool(cpu["available"] or ram["available"] or gpus),
        "psutil": psutil is not None,
        "cpu": cpu,
        "ram": ram,
        "gpus": gpus,
        "gpu_available": bool(gpus),
        "gpu_probed": _gpu_cache["probed"],
    }
