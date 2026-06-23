"""Headline — accumulating memory embeds verified Q&A and recalls the similar one.

Runs on the in-process fallbacks (hashing embedder + numpy vector store).
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import memory  # noqa: E402

_results = []


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


pid = "t_memory"
m1 = memory.save_memory(pid, {"question": "how do I commit consumer offsets in kafka",
                              "answer": "call commitSync on the consumer.",
                              "tags": ["kafka", "from-ask"]})
memory.save_memory(pid, {"question": "what is a red-black tree",
                         "answer": "a self-balancing binary search tree."})

check("save_memory mints an id and stores it", bool(m1.get("id")))

hits = memory.search_memory([pid], "committing kafka consumer offsets", k=2)
check("recall returns both saved memories", len(hits) == 2)
check("the relevant (kafka offsets) memory is recalled first",
      "kafka" in hits[0]["question"].lower())
check("recall is ranked by similarity (top >= second)",
      hits[0]["similarity"] >= hits[1]["similarity"])

allm = memory.list_memory([pid])
check("list_memory returns the saved set", len(allm) == 2)

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
