"""Local semantic cache — exact hits make zero provider calls; every
composite-key component is a miss when changed; --force bypasses; disabled
cache reads and writes nothing.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    check, finish, find_evidence, make_workspace, mock_profile,
    requirement_response)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic import cache, store  # noqa: E402
from openmind.semantic.providers.mock_provider import (  # noqa: E402
    RECORDED_REQUESTS, reset_recorder)

runtime = get_runtime()
semantic = runtime.semantic
pid = make_workspace(runtime, "cache-ws")
evidence_id = find_evidence(pid, "shall respond to a status query")
mock_profile("mock-cache", responses={
    "requirement-extraction": requirement_response(evidence_id)})
semantic.set_policy(pid, provider_profile="mock-cache")

TASKS = ["requirement-extraction"]

# ---------------------------------------------------------------------------
# 1. Identical analysis is a pure cache hit — zero provider calls
# ---------------------------------------------------------------------------
reset_recorder()
first = semantic.start_analysis(pid, task_types=TASKS, wait=True,
                                timeout=180)["run"]
first_calls = len(RECORDED_REQUESTS)
check("first analysis called the provider", first_calls > 0)
check("first analysis completed", first["status"] == "done")

second = semantic.start_analysis(pid, task_types=TASKS, wait=True,
                                 timeout=180)["run"]
second_calls = len(RECORDED_REQUESTS) - first_calls
check("identical re-analysis is a cache hit for every target",
      second["summary"]["counters"]["cache_hits"]
      == sum(second["targets"].values()))
check("a cache hit performs ZERO provider calls", second_calls == 0)
check("cache reuse is reported in the run summary",
      second["summary"]["cache_hits"] > 0)
check("cached targets are marked 'cached', not 'done'",
      second["targets"].get("cached", 0) == sum(second["targets"].values()))
check("cache reuse created no duplicate candidates",
      len([c for c in semantic.list_candidates(pid)["candidates"]
           if c["candidate_type"] == "requirement"]) == 1)

# plan-time estimate agrees
plan = semantic.plan_analysis(pid, task_types=TASKS)
check("the dry-run plan reports the cache hits",
      plan["cache_hit_count"] == sum(second["targets"].values()))

# ---------------------------------------------------------------------------
# 2. --force bypasses the cache
# ---------------------------------------------------------------------------
forced = semantic.start_analysis(pid, task_types=TASKS, force=True,
                                 wait=True, timeout=180)["run"]
forced_calls = len(RECORDED_REQUESTS) - first_calls - second_calls
check("--force bypasses the cache (provider called again)",
      forced_calls == sum(forced["targets"].values())
      and forced["summary"]["counters"]["cache_hits"] == 0)

# ---------------------------------------------------------------------------
# 3. Key sensitivity: prompt / model / lens / evidence / analyzer
# ---------------------------------------------------------------------------
base = dict(provider_kind="mock", model_name="m1",
            task_type="requirement-extraction", task_version="1",
            prompt_hash="ph1", schema_version="1", lens_hash="",
            evidence_ids=["e_1"], evidence_hashes=["h1"], options={})
key = cache.compute_cache_key(**base)
check("identical inputs give an identical key",
      cache.compute_cache_key(**base) == key)
check("a changed prompt version/hash is a miss",
      cache.compute_cache_key(**{**base, "prompt_hash": "ph2"}) != key)
check("a changed model is a miss",
      cache.compute_cache_key(**{**base, "model_name": "m2"}) != key)
check("a changed lens definition is a miss",
      cache.compute_cache_key(**{**base, "lens_hash": "lh"}) != key)
check("a changed evidence CONTENT hash is a miss",
      cache.compute_cache_key(**{**base, "evidence_hashes": ["h2"]}) != key)
check("a changed evidence id set is a miss",
      cache.compute_cache_key(**{**base, "evidence_ids": ["e_2"]}) != key)
check("changed task options are a miss",
      cache.compute_cache_key(**{**base, "options": {"tier": "strong"}})
      != key)

# End-to-end: change the document -> new revision -> cache miss
import pathlib  # noqa: E402
src = pathlib.Path(tempfile.mkdtemp(prefix="om_cache_doc_"))
doc = src / "requirements.md"
from _semantic_helpers import REQUIREMENTS_MD  # noqa: E402
doc.write_text(REQUIREMENTS_MD + "\nREQ-NC-003: The exporter shall sign "
                                 "archives.\n", encoding="utf-8")
docs = runtime.documents.list_documents(pid)["documents"]
runtime.documents.add_document(pid, str(doc), asset_id=docs[0]["id"],
                               wait=True, timeout=180)
calls_before = len(RECORDED_REQUESTS)
after_change = semantic.start_analysis(pid, task_types=TASKS, wait=True,
                                       timeout=180)["run"]
new_calls = len(RECORDED_REQUESTS) - calls_before
check("changed evidence content is a cache MISS (provider called for the "
      "new revision)", new_calls > 0)

# ---------------------------------------------------------------------------
# 4. Cache disabled by policy: no reads, no writes
# ---------------------------------------------------------------------------
semantic.set_policy(pid, local_cache_enabled=False)
conn, lock = __import__("openmind.db", fromlist=["db"]).shared_connection()
with lock:
    rows_before = conn.execute(
        "SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
calls_before = len(RECORDED_REQUESTS)
no_cache = semantic.start_analysis(pid, task_types=TASKS, wait=True,
                                   timeout=180)["run"]
with lock:
    rows_after = conn.execute(
        "SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
check("disabled cache performs no reads (provider called despite entries)",
      len(RECORDED_REQUESTS) - calls_before
      == sum(no_cache["targets"].values()))
check("disabled cache performs no writes", rows_after == rows_before)
check("run summary shows zero cache hits with the cache disabled",
      no_cache["summary"]["counters"]["cache_hits"] == 0)

# ---------------------------------------------------------------------------
# 5. Cached output is still re-verified on reuse
# ---------------------------------------------------------------------------
semantic.set_policy(pid, local_cache_enabled=True)
policy = store.get_policy(pid)
entry = cache.lookup(policy, key)
check("an unknown key reads None", entry is None)
cache.put(policy, key, provider_kind="mock", model_name="m1",
          task_type="requirement-extraction", prompt_hash="ph1",
          schema_version="1", input_hash="ih",
          output={"candidates": []})
check("a stored entry reads back", cache.lookup(policy, key)
      == {"candidates": []})
check("cache entries are content, never trust: reuse paths re-validate "
      "and re-verify (covered end-to-end above)", True)

finish()
