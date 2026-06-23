"""Stage 3b — structure-derived diagrams are valid, grounded, and honest about absence."""
import hashlib
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import structure, diagrams  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def _h(t):
    return hashlib.sha1(t.encode()).hexdigest()


A = "from .b import helper\ndef run():\n    return helper()\n"
B = "def helper():\n    return 1\n"
sfiles = [("pkg/a.py", A, _h(A)), ("pkg/b.py", B, _h(B))]
st = structure.build_structure(sfiles, root="")

ds = diagrams.all_diagrams(st)
check("emits exactly the three always-on structure diagrams", len(ds) == 3)
check("dependency diagram is non-empty valid Mermaid",
      not ds[0]["empty"] and ds[0]["text"].splitlines()[0].startswith("flowchart"))
check("dependency diagram references real file labels (a.py / b.py)",
      "a.py" in ds[0]["text"] and "b.py" in ds[0]["text"])
check("call/usage diagram is valid Mermaid", ds[1]["text"].splitlines()[0] == "flowchart LR")

dot = diagrams.dependency_diagram(st, fmt="dot")
check("DOT output opens with 'digraph' and closes with '}'",
      dot["text"].startswith("digraph") and dot["text"].strip().endswith("}"))

empty = {"modules": {}, "dependency_graph": {"edges": []}, "call_graph": {"edges": []},
         "module_graph": {"edges": []}, "entry_points": []}
check("HONEST ABSENCE: empty dependency graph -> empty=True with a stated reason",
      diagrams.dependency_diagram(empty)["empty"] and diagrams.dependency_diagram(empty)["note"])
check("markdown projection embeds fenced Mermaid blocks",
      "```mermaid" in diagrams.to_markdown(st))

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
