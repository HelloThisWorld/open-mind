"""Model-driven lens induction (strong tier, policy-gated, budgeted).

One bounded provider request per induction run: the deterministic sample
plan's evidence goes in as ``untrustedContent``, the inventory as structural
context, and the ONLY acceptable answer is the closed lens schema. The
proposal is validated twice — schema + safe patterns first, then the
deterministic whole-corpus validation — and stored as ``provisional``
regardless of how good it looks. Approval and activation are explicit human
verbs on the service; nothing here can activate anything.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from ... import db
from .. import context as context_mod
from .. import store
from ..errors import ProviderResponseValidationError
from ..models import ModelTier, SemanticRequest, SemanticRunStatus, StructuredSchema
from ..policy import authorize, effective_budgets
from ..prompt_texts.lens_induction_v1 import LENS_INDUCTION
from ..prompts import assert_packet_shape
from ..providers import registry
from ..tasks import require_task
from ..usage import BudgetTracker, estimate_cost
from .models import LENS_SCHEMA_VERSION, lens_json_schema, validate_lens_definition
from .sampling import build_sample_plan
from .validation import validate_lens

import hashlib
import json


def _induction_packet(workspace_id: str,
                      plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    untrusted = []
    for sample in plan["samples"]:
        for ev in sample["evidence"]:
            text = context_mod.resolve_evidence_text(workspace_id,
                                                     ev["evidence_id"])
            if not text:
                continue
            untrusted.append({
                "evidenceId": ev["evidence_id"],
                "locator": {"logicalKey": sample["logical_key"],
                            "category": sample["category"]},
                "text": text[:2_000],
            })
    if not untrusted:
        return None
    packet = {
        "task": "project-lens-induction",
        "allowedEvidenceIds": [e["evidenceId"] for e in untrusted],
        "context": {"inventory": plan["inventory"]},
        "untrustedContent": untrusted,
    }
    assert_packet_shape(packet)
    return packet


def execute_induction(workspace_id: str, run_id: str, *,
                      provider_transport: Any = None) -> Dict[str, Any]:
    """The job body: one strong-tier request -> one provisional lens."""
    task = require_task("project-lens-induction")
    run = store.get_run(workspace_id, run_id)
    if not run:
        raise ProviderResponseValidationError(
            f"induction run not found: {run_id!r}")
    policy = store.get_policy(workspace_id)
    auth = authorize(workspace_id, task_type=task.task_type,
                     profile_name=run.get("provider_profile") or None,
                     policy=policy)
    profile = auth["profile"]
    provider = registry.get_provider(profile.kind)
    store.update_run(run_id, status=SemanticRunStatus.RUNNING,
                     started_at=db.now(), provider_kind=profile.kind)

    plan = build_sample_plan(workspace_id)
    packet = _induction_packet(workspace_id, plan)
    if packet is None:
        store.update_run(run_id, status=SemanticRunStatus.FAILED,
                         error="workspace has no sampleable evidence",
                         finished_at=db.now())
        return {"status": SemanticRunStatus.FAILED,
                "error": "workspace has no sampleable evidence"}

    tracker = BudgetTracker(workspace_id,
                            effective_budgets(policy, run.get("budget")))
    try:
        tracker.precheck(estimated_input_tokens=int(plan["estimated_tokens"]),
                         max_output_tokens=task.max_output_tokens,
                         tier=ModelTier.STRONG)
    except Exception as exc:
        store.update_run(run_id, status=SemanticRunStatus.FAILED,
                         error=str(exc)[:500], finished_at=db.now())
        raise

    request = SemanticRequest(
        request_id=f"sqr_{uuid.uuid4().hex[:12]}",
        workspace_id=workspace_id, analysis_run_id=run_id,
        task_type=task.task_type, model_tier=ModelTier.STRONG,
        system_instructions=LENS_INDUCTION,
        input_packet=packet, schema_name="project-lens",
        schema_version=LENS_SCHEMA_VERSION,
        prompt_version=task.prompt_version,
        max_output_tokens=task.max_output_tokens, timeout=profile.timeout,
        idempotency_key=f"{run_id}:induction",
        classification=auth["decision"]["classification"])
    schema = StructuredSchema(name="project-lens",
                              version=LENS_SCHEMA_VERSION,
                              json_schema=lens_json_schema(), strict=True)
    try:
        response = provider.generate_structured(
            request, schema, profile, transport=provider_transport)
    except Exception as exc:
        store.update_run(run_id, status=SemanticRunStatus.FAILED,
                         error=str(exc)[:500], finished_at=db.now())
        raise

    cost, currency, cost_source = estimate_cost(
        profile.kind, response.model, response.input_tokens,
        response.output_tokens, response.cached_tokens)
    store.record_usage({
        "run_id": run_id, "request_id": request.request_id,
        "provider_profile": profile.name, "provider_kind": profile.kind,
        "model_name": response.model, "task_type": task.task_type,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cached_tokens": response.cached_tokens,
        "estimated_cost": cost, "currency": currency,
        "cost_source": cost_source, "latency_ms": response.latency_ms,
        "retry_count": response.retry_count, "status": "ok",
        "response_hash": response.raw_response_hash,
    })
    tracker.record(tier=ModelTier.STRONG,
                   estimated_input_tokens=int(plan["estimated_tokens"]),
                   input_tokens=response.input_tokens,
                   output_tokens=response.output_tokens,
                   estimated_cost=cost)

    # -- validate the proposal --------------------------------------------
    normalized, errors, warnings = validate_lens_definition(
        response.structured_output, source="induced")
    sent_ids = set(packet["allowedEvidenceIds"])
    cited = set(normalized.get("sampleEvidenceIds") or [])
    invalid_citations = sorted(cited - sent_ids)
    if invalid_citations:
        errors.append(f"lens cites {len(invalid_citations)} evidence id(s) "
                      f"that were not part of the sample")
    if errors:
        # A schema-invalid proposal is NOT stored as a lens; the run records
        # the bounded failure and the provider output is discarded.
        store.update_run(
            run_id, status=SemanticRunStatus.FAILED,
            error=("induced lens failed validation: "
                   + "; ".join(errors))[:500],
            summary={"errors": errors[:20], "warnings": warnings[:20]},
            finished_at=db.now())
        return {"status": SemanticRunStatus.FAILED, "errors": errors}

    validation = validate_lens(workspace_id, normalized, source="induced")
    input_hash = hashlib.sha256(json.dumps(
        packet["allowedEvidenceIds"], sort_keys=True).encode()).hexdigest()
    lens_id = store.insert_lens(workspace_id, {
        "name": normalized.get("name") or f"induced-{run_id[-6:]}",
        "source": "induced",
        "status": "provisional",
        "schema_version": LENS_SCHEMA_VERSION,
        "definition": normalized,
        "validation": {
            "result": validation["result"], "errors": validation["errors"],
            "warnings": validation["warnings"],
            "metrics": validation["metrics"]},
        "provider_profile": profile.name,
        "model_name": response.model,
        "prompt_version": task.prompt_version,
        "input_hash": input_hash,
    })
    summary = {
        "lens_id": lens_id,
        "validation_result": validation["result"],
        "sample_count": plan["sample_count"],
        "estimated_tokens": plan["estimated_tokens"],
        "usage": store.usage_totals(run_id),
    }
    store.update_run(run_id, status=SemanticRunStatus.DONE, summary=summary,
                     finished_at=db.now(), model_name=response.model)
    return {"status": SemanticRunStatus.DONE, **summary}


__all__ = ["execute_induction"]
