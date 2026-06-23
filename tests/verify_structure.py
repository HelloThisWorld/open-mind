"""Stage 1 (detection) + Stage 2 (structure) — generic, no-false-zero, incremental.

Runs with no heavy dependencies (pure detect/structure on in-memory files).
"""
import hashlib
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import detect, structure  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def _h(t):
    return hashlib.sha1(t.encode()).hexdigest()


A = ("from .b import helper\n"
     "import os\n\n"
     "def main():\n"
     "    return helper()\n\n"
     "if __name__ == '__main__':\n"
     "    main()\n")
B = ("def helper():\n"
     "    return 42\n\n"
     "class Thing:\n"
     "    def go(self):\n"
     "        return helper()\n")
README = "# Demo\n\nIn-Sync Replicas (ISR) keep replicas consistent.\n"
REQ = "fastapi\nnumpy>=1.0\n"

files = [("pkg/a.py", A), ("pkg/b.py", B), ("README.md", README), ("requirements.txt", REQ)]
sfiles = [(p, t, _h(t)) for p, t in files]

# --- Stage 1 detection ---
det = detect.detect(files, root="")
check("detects Python as primary language", det["primary_language"] == "Python")
check("infers 'application' from the main()/__main__ entry point", det["kind"] == "application")
check("detects pip build system from requirements.txt", "pip" in det["build_systems"])
check("grounds a 'web' stack cue in the fastapi dependency",
      any(c["category"] == "web" and "fastapi" in c["dependency"] for c in det["stack_cues"]))

# --- Stage 2 structure ---
st = structure.build_structure(sfiles, root="")
s = st["stats"]
check("NO false-zero: file_count > 0 on a valid repo", s["file_count"] > 0)
check("definitions found (main, helper, Thing, go)", s["definition_count"] >= 4)
check("internal dependency edge a.py -> b.py resolved",
      any(e["from"] == "pkg/a.py" and e["to"] == "pkg/b.py" for e in st["dependency_graph"]["edges"]))
check("cross-file call edge a.py -> b.py (helper) found",
      any(e["from"] == "pkg/a.py" and e["to"] == "pkg/b.py" and e["symbol"] == "helper"
          for e in st["call_graph"]["edges"]))
check("entry point 'main' detected", any(e["kind"] == "main" for e in st["entry_points"]))
check("external dependency (os) recorded, not resolved internally",
      any(x["module"] == "os" for x in st["dependency_graph"]["external"]))
check("get_definition resolves a known symbol", structure.get_definition(st, "helper")["found"])
check("get_definition reports not-found for an unknown symbol",
      not structure.get_definition(st, "no_such_symbol_xyz")["found"])

# --- incremental ---
st2 = structure.build_structure(sfiles, root="", prior=st)
check("incremental: unchanged inputs re-scan 0 files", st2["stats"]["sources_scanned"] == 0)
B2 = B + "\ndef extra():\n    return 1\n"
sfiles2 = [("pkg/a.py", A, _h(A)), ("pkg/b.py", B2, _h(B2)),
           ("README.md", README, _h(README)), ("requirements.txt", REQ, _h(REQ))]
st3 = structure.build_structure(sfiles2, root="", prior=st)
check("incremental: editing one file re-scans exactly 1 file",
      st3["stats"]["sources_scanned"] == 1)

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
