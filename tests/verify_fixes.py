"""Regression tests for the generic defects found by the adversarial review.

(Fix 3/4/5/9 covered the removed Kafka topology / generated-docs / service-catalog
tiers and were dropped with those features. Fix 1 — model-server reentrancy — and
Fix 2 — local-only host coercion — are generic and retained.)
"""
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
results = []
def check(n, c, d=""):
    results.append((n, bool(c))); print(("PASS " if c else "FAIL ")+n+(("  -- "+d) if d else ""))

from openmind import config, db, model_server

# ---- Fix 2: host coercion + 0.0.0.0 not local ----
import openmind.netguard as ng
check("Fix2 0.0.0.0 is NOT treated as local", not ng.is_local_host("0.0.0.0"))
check("Fix2 coerce_loopback rewrites 0.0.0.0 -> 127.0.0.1", config.coerce_loopback("0.0.0.0")=="127.0.0.1")
check("Fix2 coerce_loopback rewrites LAN IP -> 127.0.0.1", config.coerce_loopback("192.168.1.5")=="127.0.0.1")
check("Fix2 coerce_loopback keeps 127.0.0.1", config.coerce_loopback("127.0.0.1")=="127.0.0.1")
os.environ.setdefault("OPENMIND_DATA_DIR", "C:/work/open-mind/data_fixes")
db.init_db()
db.set_model_config({"port": 1})  # dead LLM port -> deterministic, fast, offline-safe
saved = db.set_model_config({"host":"0.0.0.0","port":7080})
check("Fix2 set_model_config persists coerced loopback host", saved["host"]=="127.0.0.1")
ms_args = model_server.ModelServer().build_args({"host":"0.0.0.0","model_path":"m.gguf"})
hi = ms_args.index("--host"); check("Fix2 build_args --host coerced to loopback", ms_args[hi+1]=="127.0.0.1")

# ---- Fix 1: ModelServer.start() reentrancy (no self-deadlock when 'loading') ----
ms = model_server.ModelServer(); ms._status = "loading"
box = {}
def call(): box["r"] = ms.start({"host":"127.0.0.1","port":59999,"model_path":""})
th = threading.Thread(target=call, daemon=True); th.start(); th.join(timeout=5)
check("Fix1 start() returns when already loading (no deadlock)", not th.is_alive() and box.get("r"))

print("\n"+f"{sum(1 for _,c in results if c)}/{len(results)} fix-checks passed")
sys.exit(0 if all(c for _,c in results) else 1)
