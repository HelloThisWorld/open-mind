"""Stage 5 — grounded Ask: glossary-first routing and the deterministic
"not found" path (never a guessed definition).

NOTE: earlier revisions also tested a free-synthesis citation gate
(ask.check_grounding) and a hard no-grounding block (messages=None +
fallback_answer). Both were removed from the product — an output citation
verifier for model answers is a README roadmap item. This suite covers the
deterministic grounding contracts that ARE implemented today: glossary-first
routing, verbatim definition delivery, the honest miss, and honest empty
source assembly.

Runs on the in-process fallbacks (hashing embedder + numpy vector store).
"""
import hashlib
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from openmind import ask, glossary, structure, mapio  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def _h(t):
    return hashlib.sha1(t.encode()).hexdigest()


def _user_content(b):
    return "\n".join(m["content"] for m in b["messages"] if m["role"] == "user")


# --- build a tiny project: structure + glossary (no code indexed) ---
pid = "t_grounding"
README = "# Demo\n\nIn-Sync Replicas (ISR) keep replicas consistent.\n"
sfiles = [("README.md", README, _h(README))]
mapio.save_structure(pid, structure.build_structure(sfiles, root=""))
mapio.save_glossary(pid, glossary.build_glossary(sfiles))

# --- GLOSSARY-FIRST routing for an explicit definition question ---
b = ask.build_grounded([pid], "what does ISR mean?", [], k=5)
check("glossary-first: routed to a single glossary source",
      len(b["sources"]) == 1 and b["sources"][0]["kind"] == "glossary")
check("glossary-first: the model gets the deterministic definition (ISR found)",
      "In-Sync Replicas" in b["sources"][0]["body"])
check("glossary-first: meta marks the deterministic hit route",
      b["meta"].get("glossary_route") == "hit")

# --- HONEST MISS: explicit definition question, no entry -> never a guess ---
b2 = ask.build_grounded([pid], "what does ZKQ stand for?", [], k=5)
check("honest miss: routed to the deterministic not-found glossary source",
      b2["meta"].get("glossary_route") == "miss"
      and b2["sources"][0]["kind"] == "glossary")
check("honest miss: the source states there is no authoritative definition",
      "no authoritative definition" in b2["sources"][0]["body"])
check("honest miss: the prompt forbids guessing a definition",
      "Do NOT guess" in _user_content(b2))

# --- NON-TERM question with nothing indexed: honest empty source assembly ---
b3 = ask.build_grounded([pid], "what is the airspeed of an unladen swallow", [], k=5)
check("no sources: source_count == 0 (nothing fabricated)",
      b3["meta"]["source_count"] == 0)
check("no sources: the prompt tells the model the source list is empty",
      "(no sources retrieved)" in _user_content(b3))

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
