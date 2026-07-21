"""LensService — the application service for Adaptive Project Lenses.

Exposed as ``runtime.lenses`` / ``ServiceContainer.lenses``. The lifecycle it
enforces (spec §26/§28/§29):

* built-in lenses are VIRTUAL projections of Template Profiles, listable per
  workspace and materialized into a row only on activation;
* organization lens files are listable globally but usable only after an
  explicit per-workspace IMPORT (which snapshots definition + checksum);
* induced lenses are created ``provisional`` by the induction job and can
  never skip a step: deterministic validation, explicit ``approve``, explicit
  ``activate`` — in that order, each a separate human verb;
* at most one ACTIVE lens per workspace; activating another supersedes it
  back to its earned status; activation influences semantic planning only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...domain.errors import InvalidRequest
from .. import store
from ..errors import LensInvalid, LensNotFound
from ..models import LensSource, LensStatus, SemanticRunStatus
from ..policy import authorize, effective_budgets
from . import adapter, registry as org_registry
from .models import LENS_SCHEMA_VERSION
from .sampling import build_sample_plan
from .validation import validate_lens


class LensService:
    """Use cases over Project Lenses."""

    def __init__(self, workspaces: Any, jobs: Any,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        self._ensure_worker = ensure_worker

    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    # -- listing ------------------------------------------------------------
    def list_lenses(self, workspace_id: str,
                    source: Optional[str] = None) -> Dict[str, Any]:
        """Workspace rows + virtual built-ins + organization files. Virtual
        entries carry ``stored: false``; a stored row of the same name wins
        (it is the workspace's snapshot)."""
        self._require_workspace(workspace_id)
        rows = store.list_lenses(workspace_id, source=source)
        stored_names = {(r["source"], r["name"]) for r in rows}
        out: List[Dict[str, Any]] = [dict(r, stored=True) for r in rows]
        if source in (None, LensSource.BUILTIN):
            for record in adapter.builtin_lens_records():
                if (LensSource.BUILTIN, record["name"]) not in stored_names:
                    out.append(record)
        if source in (None, LensSource.ORGANIZATION):
            for record in org_registry.list_organization_lenses():
                if (LensSource.ORGANIZATION, record["name"]) \
                        not in stored_names:
                    out.append(record)
        return {"workspace_id": workspace_id, "lenses": out,
                "count": len(out)}

    def get_lens(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_lens(workspace_id, lens_id)
        if row:
            row["stored"] = True
            return row
        virtual = adapter.get_builtin_lens(lens_id)
        if virtual:
            return virtual
        raise LensNotFound(f"lens not found: {lens_id!r}",
                           details={"lens_id": lens_id})

    def get_active_lens(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        self._require_workspace(workspace_id)
        return store.get_active_lens(workspace_id)

    # -- organization import / export ---------------------------------------
    def import_organization_lens(self, workspace_id: str,
                                 name: str) -> Dict[str, Any]:
        """Snapshot one organization lens FILE into this workspace, with its
        checksum, validated against THIS workspace's corpus."""
        self._require_workspace(workspace_id)
        record = org_registry.get_organization_lens(name)
        if record is None:
            raise LensNotFound(
                f"organization lens not found: {name!r} "
                f"(directory: {org_registry.lenses_dir()})",
                details={"name": name})
        if record["validation"]["result"] == "invalid":
            raise LensInvalid(
                f"organization lens {name!r} is invalid and cannot be "
                f"imported: " + "; ".join(record["validation"]["errors"][:5]),
                details={"errors": record["validation"]["errors"]})
        validation = validate_lens(workspace_id, record["definition"],
                                   source=LensSource.ORGANIZATION)
        status = (LensStatus.VALIDATED
                  if validation["result"] != "invalid"
                  else LensStatus.PROVISIONAL)
        lens_id = store.insert_lens(workspace_id, {
            "name": record["name"],
            "organization_key": f"{record['file']}#{record['checksum'][:16]}",
            "source": LensSource.ORGANIZATION,
            "status": status,
            "schema_version": LENS_SCHEMA_VERSION,
            "definition": validation["normalized"],
            "validation": {k: validation[k]
                           for k in ("result", "errors", "warnings",
                                     "metrics")},
        })
        return self.get_lens(workspace_id, lens_id)

    def export_lens(self, workspace_id: str, lens_id: str,
                    path: str = "") -> Dict[str, Any]:
        """The lens's definition document, optionally written to *path* as
        JSON (an organization-lens file another machine can load)."""
        lens = self.get_lens(workspace_id, lens_id)
        definition = lens.get("definition") or {}
        out: Dict[str, Any] = {"lens_id": lens.get("id"),
                               "name": lens.get("name"),
                               "definition": definition}
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(definition, indent=2,
                                         sort_keys=True) + "\n",
                              encoding="utf-8")
            out["written_to"] = str(target)
        return out

    # -- induction ----------------------------------------------------------
    def plan_induction(self, workspace_id: str,
                       provider_profile: str = "") -> Dict[str, Any]:
        """The deterministic sample plan + the policy verdict. No provider
        call and nothing stored."""
        self._require_workspace(workspace_id)
        plan = build_sample_plan(workspace_id)
        from ..errors import SemanticError
        try:
            auth = authorize(workspace_id,
                             task_type="project-lens-induction",
                             profile_name=provider_profile or None)
            plan["policy_result"] = {"allowed": True, **auth["decision"]}
        except SemanticError as exc:
            plan["policy_result"] = {"allowed": False, "code": exc.code,
                                     "reason": exc.message}
        return plan

    def start_induction(self, workspace_id: str, *,
                        provider_profile: str = "", wait: bool = False,
                        timeout: float = 3600.0) -> Dict[str, Any]:
        """Gate, create the induction run, enqueue the job."""
        self._require_workspace(workspace_id)
        policy = store.get_policy(workspace_id)
        auth = authorize(workspace_id, task_type="project-lens-induction",
                         profile_name=provider_profile or None,
                         policy=policy)
        profile = auth["profile"]
        run = store.create_run(
            workspace_id, run_type="lens-induction", scope={},
            provider_profile=profile.name, provider_kind=profile.kind,
            model_tier="strong", task_set=["project-lens-induction"],
            task_version="1", prompt_set_version="1",
            analyzer_version="", input_hash="",
            budget=effective_budgets(policy),
            status=SemanticRunStatus.QUEUED)
        if self._ensure_worker:
            self._ensure_worker()
        from ... import jobs as jobs_engine
        job = jobs_engine.enqueue_lens_induction(workspace_id, {
            "analysis_run_id": run["id"], "workspace_id": workspace_id,
            "provider_profile": profile.name,
        })
        result: Dict[str, Any] = {"workspace_id": workspace_id,
                                  "run_id": run["id"],
                                  "job_id": job["job_id"], "waited": False}
        if wait:
            outcome = self._jobs.wait_for_terminal(job["job_id"],
                                                   timeout=timeout)
            result["waited"] = True
            result["job_status"] = outcome.status
            result["completed"] = outcome.completed
            run_after = store.get_run(workspace_id, run["id"])
            result["run"] = run_after
            lens_id = ((run_after or {}).get("summary") or {}).get("lens_id")
            if lens_id:
                result["lens"] = self.get_lens(workspace_id, lens_id)
        return result

    # -- lifecycle ----------------------------------------------------------
    def validate(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        """(Re)run deterministic validation; store the report; promote a
        not-invalid ``provisional`` lens to ``validated``."""
        lens = self._stored_lens(workspace_id, lens_id)
        validation = validate_lens(workspace_id, lens["definition"],
                                   source=lens["source"])
        report = {k: validation[k]
                  for k in ("result", "errors", "warnings", "metrics")}
        fields: Dict[str, Any] = {"validation": report}
        if validation["result"] != "invalid" and \
                lens["status"] == LensStatus.PROVISIONAL:
            fields["status"] = LensStatus.VALIDATED
        store.update_lens(workspace_id, lens_id, **fields)
        return self.get_lens(workspace_id, lens_id)

    def approve(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        """Explicit human approval. Only a lens whose CURRENT deterministic
        validation is not ``invalid`` can be approved (spec §28)."""
        lens = self._stored_lens(workspace_id, lens_id)
        if lens["status"] in (LensStatus.ACTIVE,):
            raise InvalidRequest("lens is already active",
                                 details={"lens_id": lens_id})
        validation = validate_lens(workspace_id, lens["definition"],
                                   source=lens["source"])
        report = {k: validation[k]
                  for k in ("result", "errors", "warnings", "metrics")}
        if validation["result"] == "invalid":
            store.update_lens(workspace_id, lens_id, validation=report)
            raise LensInvalid(
                "an invalid lens cannot be approved: "
                + "; ".join(validation["errors"][:5]),
                details={"lens_id": lens_id,
                         "errors": validation["errors"]})
        from ... import db as db_module
        store.update_lens(workspace_id, lens_id, validation=report,
                          status=LensStatus.APPROVED,
                          approved_at=db_module.now())
        return self.get_lens(workspace_id, lens_id)

    def reject(self, workspace_id: str, lens_id: str,
               reason: str = "") -> Dict[str, Any]:
        lens = self._stored_lens(workspace_id, lens_id)
        validation = dict(lens.get("validation") or {})
        if reason:
            validation["rejection_reason"] = str(reason)[:500]
        store.update_lens(workspace_id, lens_id,
                          status=LensStatus.REJECTED, validation=validation)
        return self.get_lens(workspace_id, lens_id)

    def activate(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        """Activation rules (spec §28): built-in — always; organization —
        valid; induced — valid AND explicitly approved. One active lens per
        workspace; the previous one is superseded back to its earned
        status. Activation influences semantic planning only."""
        self._require_workspace(workspace_id)
        row = store.get_lens(workspace_id, lens_id)
        if row is None and str(lens_id).startswith(
                adapter.BUILTIN_ID_PREFIX):
            row = self._materialize_builtin(workspace_id, lens_id)
        if row is None:
            raise LensNotFound(f"lens not found: {lens_id!r}",
                               details={"lens_id": lens_id})
        lens_id = row["id"]

        if row["source"] == LensSource.INDUCED:
            if row["status"] not in (LensStatus.APPROVED,):
                raise LensInvalid(
                    "an induced lens must be explicitly approved before "
                    "activation",
                    details={"lens_id": lens_id, "status": row["status"]})
        if row["source"] in (LensSource.ORGANIZATION, LensSource.INDUCED):
            validation = validate_lens(workspace_id, row["definition"],
                                       source=row["source"])
            if validation["result"] == "invalid":
                raise LensInvalid(
                    "an invalid lens cannot be activated: "
                    + "; ".join(validation["errors"][:5]),
                    details={"lens_id": lens_id,
                             "errors": validation["errors"]})

        current = store.get_active_lens(workspace_id)
        if current and current["id"] != lens_id:
            store.update_lens(workspace_id, current["id"],
                              status=self._deactivated_status(current))
        store.update_lens(workspace_id, lens_id, status=LensStatus.ACTIVE)
        return self.get_lens(workspace_id, lens_id)

    def deactivate(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        lens = self._stored_lens(workspace_id, lens_id)
        if lens["status"] != LensStatus.ACTIVE:
            raise InvalidRequest("lens is not active",
                                 details={"lens_id": lens_id,
                                          "status": lens["status"]})
        store.update_lens(workspace_id, lens_id,
                          status=self._deactivated_status(lens))
        return self.get_lens(workspace_id, lens_id)

    # -- internals ----------------------------------------------------------
    def _stored_lens(self, workspace_id: str, lens_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = store.get_lens(workspace_id, lens_id)
        if not row:
            raise LensNotFound(f"lens not found: {lens_id!r} (built-in "
                               f"lenses must be activated to gain a stored "
                               f"lifecycle)", details={"lens_id": lens_id})
        return row

    @staticmethod
    def _deactivated_status(lens: Dict[str, Any]) -> str:
        return (LensStatus.APPROVED if lens.get("approved_at")
                else LensStatus.VALIDATED)

    def _materialize_builtin(self, workspace_id: str,
                             virtual_id: str) -> Optional[Dict[str, Any]]:
        record = adapter.get_builtin_lens(virtual_id)
        if record is None:
            return None
        existing = store.find_lens_by_name(workspace_id, record["name"],
                                           source=LensSource.BUILTIN)
        if existing:
            return existing
        lens_id = store.insert_lens(workspace_id, {
            "name": record["name"], "source": LensSource.BUILTIN,
            "status": LensStatus.VALIDATED,
            "schema_version": LENS_SCHEMA_VERSION,
            "definition": record["definition"],
            "validation": record["validation"],
            "organization_key": "",
        })
        return store.get_lens(workspace_id, lens_id)


__all__ = ["LensService"]
