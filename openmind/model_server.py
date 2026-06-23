"""Local llama-server lifecycle manager (Invariant 11).

Status is derived by CONTINUOUS reconciliation against ground truth — never a
one-shot latch. Every status read re-probes /health and re-checks the managed
process, so the displayed state always converges to the truth and recovers on
its own (loading -> ready, error -> ready) within one poll interval.

State machine:
  not_started — never launched and nothing answering.
  stopped     — we stopped it (or an attached external server went away).
  starting    — our process just launched, not yet accepting connections.
  loading     — server up but the model is still loading (port refuses or 503).
  ready       — /health returns 200.
  error       — the managed process exited/crashed (with exit code + log tail).

A connection refusal while the process is alive (or during the startup grace) is
loading/starting, NOT error. Error is reserved for an actually-dead process.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from . import config, db, netguard

# Genuine OOM / load-failure phrases (used ONLY to enrich the error reason after
# a crash — never to set status). Intentionally precise: bare "ggml_vulkan"
# appears in normal Vulkan startup logs and must NOT be treated as a failure.
_OOM_MARKERS = (
    "out of memory", "failed to allocate", "vk_error_out_of_device_memory",
    "cuda out of memory", "cudamalloc failed", "ggml_vulkan: failed to allocate",
    "failed to load model", "error loading model", "unable to load model",
    "insufficient memory", "device lost", "errorout of memory",
)
_GRACE_SECONDS = 3.0  # below this, a not-yet-listening managed proc is "starting"
_RECONCILE_TTL = 2.0  # reuse the last health-probe result within this window so
                      # frequent status() polls don't each pay a blocking probe


class ModelServer:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._log: Deque[str] = deque(maxlen=600)
        self._status = "not_started"
        self._error = ""
        self._exit_code: Optional[int] = None
        self._attached = False
        self._intentional_stop = False
        self._crashed = False
        self._ever_started = False
        self._spawn_ts: Optional[float] = None
        self._oom = False
        self._lock = threading.RLock()       # reentrant: status()->log_tail()
        self._cfg: Dict[str, Any] = {}
        self._last_probe_ts = 0.0            # throttle the health probe
        self._last_probe_code: Optional[int] = None

    # -- logging -----------------------------------------------------------
    def _append(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        with self._lock:
            self._log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        low = line.lower()
        if any(m in low for m in _OOM_MARKERS):
            # remember as a likely failure REASON; do NOT latch status here —
            # status becomes error only if/when the process actually exits.
            self._oom = True
            self._error = line.strip()

    def log_tail(self, n: int = 300) -> List[str]:
        with self._lock:
            return list(self._log)[-n:]

    # -- arg building ------------------------------------------------------
    def build_args(self, cfg: Dict[str, Any]) -> List[str]:
        exe = cfg.get("llama_server_path") or "llama-server"
        args: List[str] = [exe]
        if cfg.get("model_path"):
            args += ["--model", cfg["model_path"]]
        args += ["-ngl", str(cfg.get("n_gpu_layers", 999))]
        args += ["-c", str(cfg.get("ctx_size", 32768))]
        args += ["--parallel", str(cfg.get("parallel", 1))]
        args += ["--host", config.coerce_loopback(str(cfg.get("host", "127.0.0.1")))]
        args += ["--port", str(cfg.get("port", 7080))]
        args += ["--threads", str(cfg.get("threads", 12))]
        if cfg.get("flash_attn", True):
            args += ["-fa", "on"]
        if cfg.get("cache_type_k"):
            args += ["-ctk", str(cfg["cache_type_k"])]
        if cfg.get("cache_type_v"):
            args += ["-ctv", str(cfg["cache_type_v"])]
        if cfg.get("jinja", True):
            args += ["--jinja"]
        extra = cfg.get("extra_args", "").strip()
        if extra:
            try:
                args += shlex.split(extra, posix=False)
            except Exception:
                args += extra.split()
        return args

    # -- health probe (local only, via netguard) ---------------------------
    def _probe(self, cfg: Dict[str, Any], timeout: float = 1.5) -> Optional[int]:
        host, port = cfg.get("host", "127.0.0.1"), cfg.get("port", 7080)
        try:
            r = netguard.guarded_request("GET", f"http://{host}:{port}/health", timeout=timeout)
            return r.status_code
        except Exception:
            return None

    def is_serving(self, cfg: Dict[str, Any]) -> bool:
        return self._probe(cfg, timeout=1.5) is not None

    # -- lifecycle ---------------------------------------------------------
    def start(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = cfg or db.get_model_config()
        self._cfg = cfg
        self._ever_started = True
        self._intentional_stop = False
        self._crashed = False
        self._error = ""
        self._exit_code = None
        self._oom = False
        self._last_probe_ts = 0.0   # force a fresh probe right after (re)start

        # ATTACH-don't-duplicate: if something already serves the port, attach.
        code = self._probe(cfg, timeout=1.5)
        if code is not None:
            self._attached = True
            self._proc = None
            self._spawn_ts = time.time()
            self._append(f"[attach] Found existing server on "
                         f"{cfg.get('host')}:{cfg.get('port')} (HTTP {code}); attaching.")
            return self.status()

        # else spawn a managed process
        exe = cfg.get("llama_server_path") or "llama-server"
        if exe != "llama-server" and not os.path.exists(exe):
            self._crashed = True
            self._error = f"llama-server not found: {exe}"
            self._append(f"[error] {self._error}")
            return self.status()
        if cfg.get("model_path") and not os.path.exists(cfg["model_path"]):
            self._crashed = True
            self._error = f"model file not found: {cfg['model_path']}"
            self._append(f"[error] {self._error}")
            return self.status()

        args = self.build_args(cfg)
        self._append(f"[spawn] {' '.join(args)}")
        self._attached = False
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=creationflags,
            )
            self._spawn_ts = time.time()
        except Exception as exc:
            self._proc = None
            self._crashed = True
            self._error = f"failed to spawn: {exc}"
            self._append(f"[error] {self._error}")
            return self.status()

        threading.Thread(target=self._reader, daemon=True).start()
        return self.status()

    def _reader(self) -> None:
        """Stream stdout/stderr into the ring buffer. Status is NOT set here —
        the reconciler decides state from the live process + probe."""
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                self._append(line)
        except Exception:
            pass

    # -- reconciliation (the source of truth) ------------------------------
    def _reconcile(self, force: bool = False) -> None:
        cfg = self._cfg or db.get_model_config()
        proc = self._proc

        # 1) a managed process that has EXITED -> stopped (intentional) or crash.
        #    Cheap (no network) and ALWAYS evaluated, so an exit/crash is detected
        #    at once even between throttled probes.
        if proc is not None and proc.poll() is not None:
            code = proc.poll()
            self._exit_code = code
            self._proc = None  # allow future probes/restarts to recover
            if self._intentional_stop:
                self._status = "stopped"
            else:
                self._crashed = True
                if not self._error or self._error.startswith("["):
                    self._error = f"llama-server exited (code {code})"
                self._append(f"[exit] process exited with code {code}")
                self._status = "error"
            return

        # 2) probe the configured endpoint (ground truth for readiness). The probe
        # is the ONLY blocking step here, and status()/refresh() are polled by the
        # UI every ~1-2s, so we THROTTLE it — reuse the last probe result within a
        # short window — which is what stopped the whole UI feeling sluggish. The
        # cheap state checks (above + below) stay fresh. Short timeout: a live local
        # llama-server answers /health in well under a second; a dead/wrong port
        # must not stall the poll for the full default timeout.
        now = time.time()
        if force or (now - self._last_probe_ts) >= _RECONCILE_TTL:
            self._last_probe_code = self._probe(cfg, timeout=0.6)
            self._last_probe_ts = now
        code = self._last_probe_code
        if code == 200:
            self._status = "ready"
            self._error = ""
            self._crashed = False
            return
        if code is not None:                       # answered but not 200 (loading model)
            self._status = "loading"
            self._crashed = False
            return

        # 3) nothing answering
        if self._proc is not None:                 # our process is alive, not listening yet
            elapsed = time.time() - (self._spawn_ts or time.time())
            self._status = "starting" if elapsed < _GRACE_SECONDS else "loading"
            return
        if self._crashed:                          # crashed and still nothing serving
            self._status = "error"
            return
        if self._attached:                         # attached external server went away
            self._attached = False
            self._status = "stopped"
            return
        self._status = "stopped" if self._ever_started else "not_started"

    def refresh(self) -> None:
        self._reconcile()

    def stop(self) -> Dict[str, Any]:
        self._intentional_stop = True
        self._crashed = False
        self._error = ""
        self._last_probe_ts = 0.0   # reflect the stop on the next status()
        if self._attached and self._proc is None:
            self._append("[stop] Detaching from externally-started server "
                         "(not owned by Open Mind; not killed).")
            self._attached = False
            self._status = "stopped"
            return self.status()
        proc = self._proc
        if proc and proc.poll() is None:
            self._append("[stop] Terminating llama-server.")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as exc:
                self._append(f"[stop] error: {exc}")
        self._proc = None
        self._status = "stopped"
        return self.status()

    def restart(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.stop()
        time.sleep(0.5)
        return self.start(cfg or db.get_model_config())

    # -- status ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        self._reconcile()
        cfg = self._cfg or db.get_model_config()
        host, port = cfg.get("host", "127.0.0.1"), cfg.get("port", 7080)
        elapsed = 0
        if self._status in ("starting", "loading") and self._spawn_ts:
            elapsed = int(time.time() - self._spawn_ts)
        return {
            "status": self._status,
            "ready": self._status == "ready",
            "attached": self._attached,
            "pid": self._proc.pid if self._proc else None,
            "error": self._error if self._status == "error" else "",
            "exit_code": self._exit_code if self._status == "error" else None,
            "oom": self._oom and self._status == "error",
            "elapsed": elapsed,
            "base_url": f"http://{host}:{port}/v1",
            "model_path": cfg.get("model_path", ""),
            "host": host,
            "port": port,
            "log_tail": self.log_tail(),
        }


# module singleton
server = ModelServer()
