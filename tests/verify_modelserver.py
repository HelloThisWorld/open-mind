"""Acceptance for model-server status: continuous sync, accurate state machine,
surfaced crash reason, auto-recovery, no stale latch (decoupled from ingest)."""
import os, sys, socket, subprocess, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)
from openmind.model_server import ModelServer

results = []
def check(n, c, d=""):
    results.append((n, bool(c))); print(("PASS " if c else "FAIL ")+n+(("  -- "+d) if d else ""))

PY = sys.executable
HEALTH = ("import sys,time\n"
          "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
          "time.sleep(float(sys.argv[1]))\n"
          "class H(BaseHTTPRequestHandler):\n"
          " def do_GET(s): s.send_response(200); s.end_headers(); s.wfile.write(b'ok')\n"
          " def log_message(s,*a): pass\n"
          "HTTPServer(('127.0.0.1',int(sys.argv[2])),H).serve_forever()\n")
CRASH = ("import sys\nprint('ggml_vulkan: failed to allocate 99999 MB', flush=True)\nsys.exit(7)\n")
SLEEP = "import time; time.sleep(60)"

_procs = []
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def spawn(*args):
    p = subprocess.Popen([PY, "-c", *args], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    _procs.append(p); return p

def poll_until(ms, want, timeout=8.0):
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        st = ms.status()["status"]; seen.append(st)
        if st == want:
            return True, seen
        time.sleep(0.4)
    return False, seen

try:
    # --- not_started: nothing launched -> NOT error ---
    # point at a definitely-dead port so the probe reliably fails (independent of
    # any real llama-server that may be running on the default port in this env)
    ms = ModelServer(); ms._cfg = {"host": "127.0.0.1", "port": free_port()}
    st = ms.status()
    check("not_started when nothing launched (not error)", st["status"] == "not_started")

    # --- starting: managed proc alive, port not answering, within grace ---
    dead = free_port()
    ms = ModelServer(); ms._cfg = {"host": "127.0.0.1", "port": dead}
    ms._ever_started = True; ms._spawn_ts = time.time(); ms._proc = spawn(SLEEP)
    check("starting: alive proc, no listener, within grace", ms.status()["status"] == "starting")

    # --- loading: alive proc, still no listener, past grace (model loading) ---
    ms._spawn_ts = time.time() - 5
    s2 = ms.status()
    check("loading: alive proc past grace, connection refused is NOT error",
          s2["status"] == "loading" and s2["elapsed"] >= 5)

    # --- loading -> ready convergence with NO manual refresh, never false error ---
    port = free_port()
    ms = ModelServer(); ms._cfg = {"host": "127.0.0.1", "port": port}
    ms._ever_started = True; ms._spawn_ts = time.time(); ms._proc = spawn(HEALTH, "1.5", str(port))
    seq = []
    for _ in range(25):
        st = ms.status()["status"]; seq.append(st)
        if st == "ready":
            break
        time.sleep(0.3)
    check("loading->ready converges automatically (no manual refresh, never false error)",
          "ready" in seq and "error" not in seq, f"seq={seq}")

    # --- crash: process exits -> error WITH exit code + stderr tail ---
    ms = ModelServer(); ms._cfg = {"host": "127.0.0.1", "port": free_port()}
    ms._ever_started = True; ms._spawn_ts = time.time(); ms._proc = spawn(CRASH)
    threading.Thread(target=ms._reader, daemon=True).start()
    ms._proc.wait(timeout=5); time.sleep(0.3)
    st = ms.status()
    tail = "\n".join(st["log_tail"])
    check("crash -> error with exit code", st["status"] == "error" and st["exit_code"] == 7,
          f"status={st['status']} code={st['exit_code']}")
    check("crash -> reason + stderr tail surfaced", bool(st["error"]) and "ggml_vulkan" in tail)
    check("crash -> OOM flagged", st["oom"] is True)

    # --- auto-recovery: after error, a server appears -> flips to ready ---
    p2 = free_port()
    ms._cfg["port"] = p2
    hp = spawn(HEALTH, "0", str(p2))
    ok, seen = poll_until(ms, "ready", timeout=6)
    check("error -> ready auto-recovery within poll interval", ok, f"seq={seen}")

    # --- start() ATTACHES to an already-serving port (no duplicate) ---
    p3 = free_port(); spawn(HEALTH, "0", str(p3)); time.sleep(0.6)
    ms = ModelServer()
    ms.start({"host": "127.0.0.1", "port": p3, "model_path": "", "llama_server_path": "llama-server"})
    st = ms.status()
    check("start attaches to running server (ready, no managed pid)",
          st["status"] == "ready" and st["attached"] and st["pid"] is None)

    # --- decouple: status is never 'error' merely because no model runs ---
    ms = ModelServer(); ms._cfg = {"host": "127.0.0.1", "port": free_port()}
    check("no-model state is stopped/not_started, never a false error",
          ms.status()["status"] in ("not_started", "stopped"))
finally:
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass

print(f"\n{sum(1 for _, c in results if c)}/{len(results)} checks passed")
sys.exit(0 if all(c for _, c in results) else 1)
