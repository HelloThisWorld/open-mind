"""Usage accounting, machine-local pricing and budget enforcement.

PRICING IS CONFIGURATION, NEVER CODE
------------------------------------
Provider prices change too fast to be facts in source. An OPTIONAL
machine-local file, ``<OPENMIND_MACHINE_DIR>/pricing.json``, may declare
per-model rates::

    {"openai:my-model": {"input_cost_per_million": 1.0,
                          "output_cost_per_million": 3.0,
                          "cached_input_cost_per_million": 0.25,
                          "currency": "USD",
                          "effective_date": "2026-07-01"}}

With a matching entry, ``estimated_cost`` is computed and ``cost_source`` is
``configured``. Without one — or with unknown token counts — the cost is
``None`` and the source ``unknown``. A ``provider``-reported cost would win
if an API ever returned one; none of the supported APIs does today.

BUDGETS ARE CHECKED THREE TIMES
-------------------------------
1. at PLAN time (the dry-run reports what would exceed);
2. immediately BEFORE each provider request (:meth:`BudgetTracker.precheck`
   raises :class:`SemanticBudgetExceeded` and nothing is sent);
3. AFTER usage returns (:meth:`BudgetTracker.record` — actuals may exceed
   the estimate; the NEXT precheck then stops the run).

Exhaustion is an orderly stop, not an error state: the runner preserves
completed work, marks the run ``partial`` with ``budget_exhausted`` and the
unprocessed targets, and never reports it complete.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple

from .. import machine
from . import store
from .errors import SemanticBudgetExceeded
from .models import CostSource, ModelTier


def pricing_file():
    return machine.MACHINE_DIR / "pricing.json"


def load_pricing() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(pricing_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def estimate_cost(provider_kind: str, model_name: str,
                  input_tokens: Optional[int], output_tokens: Optional[int],
                  cached_tokens: Optional[int]
                  ) -> Tuple[Optional[float], str, str]:
    """(estimated_cost, currency, cost_source). None/''/unknown when no
    reliable price exists — an unknown cost is never reported as zero."""
    entry = load_pricing().get(f"{provider_kind}:{model_name}")
    if not entry or input_tokens is None or output_tokens is None:
        return None, "", CostSource.UNKNOWN
    try:
        in_rate = float(entry.get("input_cost_per_million") or 0.0)
        out_rate = float(entry.get("output_cost_per_million") or 0.0)
        cached_rate = entry.get("cached_input_cost_per_million")
        currency = str(entry.get("currency") or "USD")
        cached = int(cached_tokens or 0)
        uncached_input = max(0, int(input_tokens) - cached)
        cost = (uncached_input * in_rate + int(output_tokens) * out_rate) / 1e6
        if cached_rate is not None and cached:
            cost += cached * float(cached_rate) / 1e6
        return round(cost, 6), currency, CostSource.CONFIGURED
    except (TypeError, ValueError):
        return None, "", CostSource.UNKNOWN


def _today_start() -> str:
    return time.strftime("%Y-%m-%dT00:00:00", time.localtime())


class BudgetTracker:
    """Enforces one run's budgets against live counters + the day's ledger.

    Counters use ESTIMATES before a call and ACTUALS after; when a provider
    reports no token numbers the estimate stands in (counting unknown as
    zero would let an unmetered provider burn through every cap unnoticed).
    """

    def __init__(self, workspace_id: str, budgets: Dict[str, Any]) -> None:
        self.workspace_id = workspace_id
        self.budgets = dict(budgets or {})
        self.requests = 0
        self.strong_requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0.0
        self.cost_known = True
        daily = store.usage_since(workspace_id, _today_start())
        self._daily_requests = int(daily.get("requests") or 0)
        self._daily_input = int(daily.get("input_tokens") or 0)
        self._daily_output = int(daily.get("output_tokens") or 0)

    # -- helpers ------------------------------------------------------------
    def _cap(self, key: str) -> Optional[float]:
        value = self.budgets.get(key)
        return None if value is None else float(value)

    def _exceeds(self, key: str, value: float) -> bool:
        cap = self._cap(key)
        return cap is not None and value > cap

    def _blow(self, key: str, message: str, **details: Any) -> None:
        raise SemanticBudgetExceeded(
            f"{message} (budget {key})", budget=key,
            details={**details, "cap": self.budgets.get(key)})

    # -- the three checkpoints ---------------------------------------------
    def precheck(self, *, estimated_input_tokens: int,
                 max_output_tokens: int, tier: str) -> None:
        """Raise BEFORE a provider request would exceed any cap. Nothing has
        been sent when this fires."""
        if self._exceeds("max_input_tokens_per_request",
                         estimated_input_tokens):
            self._blow("max_input_tokens_per_request",
                       f"request input estimate {estimated_input_tokens} "
                       f"tokens exceeds the per-request cap")
        if self._exceeds("max_output_tokens_per_request", max_output_tokens):
            self._blow("max_output_tokens_per_request",
                       f"request output bound {max_output_tokens} exceeds "
                       f"the per-request cap")
        if self._exceeds("max_requests_per_run", self.requests + 1):
            self._blow("max_requests_per_run",
                       f"run request count would exceed the cap")
        if tier == ModelTier.STRONG and self._exceeds(
                "max_strong_model_requests_per_run", self.strong_requests + 1):
            self._blow("max_strong_model_requests_per_run",
                       "strong-model request count would exceed the cap")
        if self._exceeds("max_total_input_tokens_per_run",
                         self.input_tokens + estimated_input_tokens):
            self._blow("max_total_input_tokens_per_run",
                       "run input-token total would exceed the cap")
        if self._exceeds("max_total_output_tokens_per_run",
                         self.output_tokens + max_output_tokens):
            self._blow("max_total_output_tokens_per_run",
                       "run output-token total would exceed the cap")
        if self._exceeds("max_daily_requests", self._daily_requests + 1):
            self._blow("max_daily_requests",
                       "daily request count would exceed the cap")
        if self._exceeds("max_daily_input_tokens",
                         self._daily_input + estimated_input_tokens):
            self._blow("max_daily_input_tokens",
                       "daily input-token total would exceed the cap")
        if self._exceeds("max_daily_output_tokens",
                         self._daily_output + max_output_tokens):
            self._blow("max_daily_output_tokens",
                       "daily output-token total would exceed the cap")
        cap = self._cap("max_estimated_cost_per_run")
        if cap is not None and self.cost_known and self.estimated_cost > cap:
            self._blow("max_estimated_cost_per_run",
                       f"estimated run cost {self.estimated_cost} already "
                       f"exceeds the cap")

    def record(self, *, tier: str, estimated_input_tokens: int,
               input_tokens: Optional[int], output_tokens: Optional[int],
               estimated_cost: Optional[float]) -> None:
        """Add one completed request's ACTUALS (falling back to the estimate
        where the provider reported nothing)."""
        self.requests += 1
        self._daily_requests += 1
        if tier == ModelTier.STRONG:
            self.strong_requests += 1
        actual_in = (int(input_tokens) if input_tokens is not None
                     else int(estimated_input_tokens))
        actual_out = int(output_tokens) if output_tokens is not None else 0
        self.input_tokens += actual_in
        self._daily_input += actual_in
        self.output_tokens += actual_out
        self._daily_output += actual_out
        if estimated_cost is None:
            # One unpriced request makes the RUN total unknowable; the cost
            # cap then can only fire on what was known before.
            self.cost_known = self.cost_known and (
                self._cap("max_estimated_cost_per_run") is None)
        else:
            self.estimated_cost = round(self.estimated_cost
                                        + float(estimated_cost), 6)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "strong_requests": self.strong_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": (self.estimated_cost if self.cost_known
                               else None),
            "budgets": dict(self.budgets),
        }


__all__ = ["BudgetTracker", "estimate_cost", "load_pricing", "pricing_file"]
