"""Acceptance test for the capability router (openmind/router.py).

The deterministic floor must ALWAYS pick a valid capability without a model, route
obvious intents correctly, and — when a model is consulted — accept its answer ONLY
if it is a known capability (graceful degradation otherwise).

Run:  python tests/verify_router.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)
from openmind import router  # noqa: E402

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    ok = bool(cond)
    print(("PASS" if ok else "FAIL"), "-", name, ("" if ok else f"   >> {detail}"))
    _passed += ok
    _failed += (not ok)


# --- deterministic floor routes obvious intents (no model) ---
cases = [
    ("ISR", "glossary"),
    ("what does ACL mean?", "glossary"),
    ("who calls SocketServer", "structure"),
    ("show the dependency graph for clients", "structure"),
    ("how does message compression work", "search"),
    ("find retry backoff logic", "search"),
]
for q, expect in cases:
    r = router.route(q, use_model=False)
    check(f"route({q!r}) -> {expect}", r["capability"] == expect,
          f"got {r['capability']}")
    check(f"  decided deterministically for {q!r}", r["decided_by"] == "deterministic")
    check(f"  capability is valid for {q!r}", r["capability"] in router.CAPABILITIES)

# --- empty / junk still yields a valid capability (never crashes) ---
for q in ("", "   ", "??!!"):
    r = router.route(q, use_model=False)
    check(f"route({q!r}) yields a valid capability", r["capability"] in router.CAPABILITIES)

# --- graceful degradation: a model that returns an off-spec label is rejected,
#     the deterministic floor stands. We stub _model_route to simulate it. ---
_orig = router._model_route
try:
    router._model_route = lambda q: "totally-bogus-capability"
    r = router.route("who calls Foo", use_model=True)
    check("off-spec model label is rejected -> deterministic floor stands",
          r["capability"] == "structure" and "deterministic" in r["decided_by"],
          f"{r}")
    router._model_route = lambda q: "glossary"
    r = router.route("how does X work", use_model=True)
    check("a VALID model label is accepted (and overrides the floor)",
          r["capability"] == "glossary" and r["decided_by"] == "model",
          f"{r}")
finally:
    router._model_route = _orig

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
