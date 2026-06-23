"""Stage 5 — grounded Ask: the deterministic no-free-synthesis gate, the
no-grounding block, and glossary-first routing.

Runs on the in-process fallbacks (hashing embedder + numpy vector store).
"""
import hashlib
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import ask, glossary, structure, mapio  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def _h(t):
    return hashlib.sha1(t.encode()).hexdigest()


# --- the deterministic citation gate ---
g_bad = ask.check_grounding("The answer is here [3].", [{"n": 1}, {"n": 2}])
check("gate FLAGS a citation to a non-existent source", g_bad["flagged"] and g_bad["invalid_citations"] == [3])
g_good = ask.check_grounding("The answer is here [1].", [{"n": 1}])
check("gate PASSES an answer that cites only real sources", not g_good["flagged"])
g_unc = ask.check_grounding("This is a confident, uncited claim.", [{"n": 1}])
check("gate FLAGS substantive claims with no citation", g_unc["flagged"])
g_ns = ask.check_grounding("not supported by the indexed project", [{"n": 1}])
check("gate ALLOWS the deterministic 'not supported' answer to be uncited", not g_ns["flagged"])

# --- build a tiny project: structure + glossary (no code indexed) ---
pid = "t_grounding"
README = "# Demo\n\nIn-Sync Replicas (ISR) keep replicas consistent.\n"
sfiles = [("README.md", README, _h(README))]
mapio.save_structure(pid, structure.build_structure(sfiles, root=""))
mapio.save_glossary(pid, glossary.build_glossary(sfiles))

# --- NO-GROUNDING BLOCK: nothing relevant -> the model is never called ---
b = ask.build_grounded([pid], "what is the airspeed of an unladen swallow", [], k=5)
check("no-grounding block: messages is None (model not called)", b["messages"] is None)
check("no-grounding block: fallback says 'not supported by the indexed project'",
      ask.NOT_SUPPORTED in (b["fallback_answer"] or ""))
check("no-grounding block: source_count == 0", b["meta"]["source_count"] == 0)

# --- GLOSSARY-FIRST routing for an explicit definition question ---
b2 = ask.build_grounded([pid], "what does ISR mean?", [], k=5)
check("glossary-first: routed to a single glossary source", b2["sources"][0]["kind"] == "glossary")
check("glossary-first: the model gets the deterministic definition (ISR found)",
      "In-Sync Replicas" in b2["sources"][0]["body"])

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
