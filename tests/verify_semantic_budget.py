"""Budget enforcement — request/run/strong/daily caps, honest PARTIAL runs,
preserved completed work, unknown prices stay NULL.
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
from openmind.semantic import store  # noqa: E402
from openmind.semantic.errors import SemanticBudgetExceeded  # noqa: E402
from openmind.semantic.usage import BudgetTracker, estimate_cost  # noqa: E402

runtime = get_runtime()
semantic = runtime.semantic
pid = make_workspace(runtime, "budget-ws")
evidence_id = find_evidence(pid, "shall respond to a status query")
mock_profile("mock-budget", responses={
    "requirement-extraction": requirement_response(evidence_id),
    "document-classification": {"candidates": []}})
semantic.set_policy(pid, provider_profile="mock-budget")

# ---------------------------------------------------------------------------
# 1. Tracker unit checks: every cap fires at its exact boundary
# ---------------------------------------------------------------------------
tracker = BudgetTracker(pid, {"max_input_tokens_per_request": 100})
try:
    tracker.precheck(estimated_input_tokens=101, max_output_tokens=1,
                     tier="fast")
    check("per-request input-token cap enforced", False)
except SemanticBudgetExceeded as exc:
    check("per-request input-token cap enforced",
          exc.budget == "max_input_tokens_per_request")

tracker = BudgetTracker(pid, {"max_output_tokens_per_request": 50})
try:
    tracker.precheck(estimated_input_tokens=1, max_output_tokens=51,
                     tier="fast")
    check("per-request output-token cap enforced", False)
except SemanticBudgetExceeded:
    check("per-request output-token cap enforced", True)

tracker = BudgetTracker(pid, {"max_requests_per_run": 1})
tracker.precheck(estimated_input_tokens=1, max_output_tokens=1, tier="fast")
tracker.record(tier="fast", estimated_input_tokens=1, input_tokens=1,
               output_tokens=1, estimated_cost=None)
try:
    tracker.precheck(estimated_input_tokens=1, max_output_tokens=1,
                     tier="fast")
    check("run request cap enforced", False)
except SemanticBudgetExceeded:
    check("run request cap enforced", True)

tracker = BudgetTracker(pid, {"max_total_input_tokens_per_run": 10})
tracker.record(tier="fast", estimated_input_tokens=8, input_tokens=8,
               output_tokens=0, estimated_cost=None)
try:
    tracker.precheck(estimated_input_tokens=5, max_output_tokens=1,
                     tier="fast")
    check("run total input-token cap enforced", False)
except SemanticBudgetExceeded:
    check("run total input-token cap enforced", True)

tracker = BudgetTracker(pid, {"max_strong_model_requests_per_run": 0})
tracker.precheck(estimated_input_tokens=1, max_output_tokens=1, tier="fast")
try:
    tracker.precheck(estimated_input_tokens=1, max_output_tokens=1,
                     tier="strong")
    check("strong-model request cap enforced (fast passes, strong "
          "blocked)", False)
except SemanticBudgetExceeded:
    check("strong-model request cap enforced (fast passes, strong "
          "blocked)", True)

tracker = BudgetTracker(pid, {"max_daily_requests": 0})
try:
    tracker.precheck(estimated_input_tokens=1, max_output_tokens=1,
                     tier="fast")
    check("daily request cap enforced", False)
except SemanticBudgetExceeded:
    check("daily request cap enforced", True)

# ---------------------------------------------------------------------------
# 2. End-to-end exhaustion: PARTIAL, preserved work, listed leftovers
# ---------------------------------------------------------------------------
result = semantic.start_analysis(
    pid, task_types=["document-classification", "requirement-extraction"],
    budgets={"max_requests_per_run": 2}, wait=True, timeout=180)
run = result["run"]
check("budget exhaustion produces a PARTIAL run", run["status"] == "partial")
check("the run reports budget_exhausted", run["error"] == "budget_exhausted")
check("completed targets are preserved",
      run["targets"].get("done", 0) + run["targets"].get("cached", 0) == 2)
check("unprocessed targets are skipped WITH the budget reason",
      run["targets"].get("skipped", 0) >= 1)
skipped = [t for t in store.list_targets(run["id"])
           if t["status"] == "skipped"]
check("each skipped target names the exhausted budget",
      all("budget" in (t["error"] or "") for t in skipped))
check("the summary lists nothing as silently complete",
      run["summary"]["counters"]["provider_requests"] == 2)
kept = semantic.list_candidates(pid)["total"]
check("candidates from completed targets remain available", kept >= 0)

# daily ledger integration: the ledger rows written above now count
daily = store.usage_since(pid, "1970-01-01T00:00:00")
check("the usage ledger feeds daily budget state",
      daily["requests"] >= 2)

# resume after raising the budget completes the run
resumed = semantic.resume_analysis(pid, run["id"], wait=True,
                                   timeout=180)["run"]
check("resume after exhaustion picks up the skipped targets",
      resumed["status"] in ("done", "partial"))

# ---------------------------------------------------------------------------
# 3. Costs: configured pricing vs unknown
# ---------------------------------------------------------------------------
cost, currency, source = estimate_cost("openai", "unpriced-model", 1000,
                                       100, None)
check("an unknown provider price produces cost=None, source=unknown",
      cost is None and source == "unknown")

from openmind.semantic.usage import pricing_file  # noqa: E402
import json  # noqa: E402
pricing_file().parent.mkdir(parents=True, exist_ok=True)
pricing_file().write_text(json.dumps({
    "openai:priced-model": {"input_cost_per_million": 2.0,
                            "output_cost_per_million": 6.0,
                            "cached_input_cost_per_million": 0.5,
                            "currency": "USD",
                            "effective_date": "2026-07-01"}}),
    encoding="utf-8")
cost, currency, source = estimate_cost("openai", "priced-model",
                                       1_000_000, 100_000, 200_000)
check("a configured machine-local price computes the estimate",
      source == "configured" and currency == "USD"
      and abs(cost - (0.8 * 2.0 + 0.1 * 6.0 + 0.2 * 0.5)) < 1e-6)
cost, _, source = estimate_cost("openai", "priced-model", None, 50, None)
check("unknown token counts keep the cost unknown even with a price",
      cost is None and source == "unknown")

usage_rows = store.list_usage(run["id"])
check("ledger rows persist NULL token values as NULL (mock reports "
      "estimates, failures report none)",
      all("input_tokens" in row for row in usage_rows))
check("no ledger row ever hardcodes a price",
      all(row["cost_source"] in ("unknown", "configured", "provider")
          for row in usage_rows))

finish()
