"""TraceabilityService — the application service over the Phase 6
traceability and conflict engine.

Exposed as ``runtime.traceability`` / ``ServiceContainer.traceability`` and
shared by the CLI, REST and (read-only subset) MCP adapters. Every method
validates the workspace first (typed :class:`WorkspaceNotFound`) and then
reads/writes only workspace-scoped state — a trace, gap or conflict of
workspace A resolves to nothing through workspace B.

WRITE DISCIPLINE
----------------
Trace refresh is derived analysis: it mints no Knowledge Revision. Policy
selection, gap governance, conflict scanning (when it changes anything),
conflict promotion and every conflict lifecycle decision are governance
writes: one graph transaction, one Human Decision with the caller-supplied
actor, one Knowledge Revision. No method here calls a semantic provider.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..domain.errors import InvalidRequest
from ..knowledge import store as kg
from ..knowledge.reconciliation import reconcile_graph_staleness
from ..knowledge.vocabularies import (DecisionTargetKind, DecisionType,
                                      RevisionAction)
from . import TRACE_ENGINE_VERSION, conflicts, engine, gaps, policies, \
    snapshots, store
from .errors import (GapNotFound, PolicyNotFound, TracePathNotFound,
                     TraceRootIneligible, TraceRunNotFound)
from .models import TraceabilityPolicy
from .vocabularies import (ConflictStatus, GapStatus, TraceStage)

MAX_NOTE_CHARS = 2_000
MAX_LIST_LIMIT = 500


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class TraceabilityService:
    """Use cases over formal traceability and governed conflicts."""

    def __init__(self, workspaces: Any, jobs: Any = None,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        self._ensure_worker = ensure_worker

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    @staticmethod
    def _bound(limit: Any, hard: int = MAX_LIST_LIMIT) -> int:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = hard
        return max(1, min(limit, hard))

    @staticmethod
    def _actor(actor: str) -> str:
        actor = str(actor or "").strip()
        if not actor:
            raise InvalidRequest("actor is required (never inferred)")
        return actor[:200]

    @staticmethod
    def _note(note: str) -> str:
        note = str(note or "").strip()
        if not note:
            raise InvalidRequest("note is required")
        if len(note) > MAX_NOTE_CHARS:
            raise InvalidRequest(
                f"note exceeds {MAX_NOTE_CHARS} characters",
                details={"chars": len(note)})
        return note

    def active_policy(self, workspace_id: str) -> TraceabilityPolicy:
        """The workspace's selected policy, or the conservative default."""
        row = store.get_workspace_policy(workspace_id)
        name = row["policy_name"] if row else policies.DEFAULT_POLICY_NAME
        return policies.resolve_policy(name)

    def _require_entity(self, workspace_id: str,
                        entity_id: str) -> Dict[str, Any]:
        from ..knowledge.errors import EntityNotFound
        entity = kg.get_entity(workspace_id, str(entity_id or "").strip())
        if not entity:
            raise EntityNotFound(entity_id, workspace_id=workspace_id)
        return entity

    # ======================================================================
    # Policies
    # ======================================================================
    def list_policies(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        selected = store.get_workspace_policy(workspace_id)
        return {
            "workspace_id": workspace_id,
            "policies": policies.list_policies(),
            "selected": selected,
            "default_policy": policies.DEFAULT_POLICY_NAME,
        }

    def get_policy(self, workspace_id: str,
                   name: Optional[str] = None) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if name:
            policy = policies.resolve_policy(name)
        else:
            policy = self.active_policy(workspace_id)
        selected = store.get_workspace_policy(workspace_id)
        return {
            "workspace_id": workspace_id,
            "policy": policy.as_dict(),
            "checksum": policy.checksum,
            "source": policy.source,
            "selected": selected,
            "is_active": (not name) or (
                selected or {}).get("policy_name") == policy.name or (
                not selected
                and policy.name == policies.DEFAULT_POLICY_NAME),
        }

    def validate_policy(self, workspace_id: str,
                        document: Any) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        report = policies.validate_policy_document(document)
        report["workspace_id"] = workspace_id
        return report

    def set_workspace_policy(self, workspace_id: str, *, policy_name: str,
                             actor: str, note: str,
                             source_command: str = "") -> Dict[str, Any]:
        """Select the workspace's active policy. A governance write: one
        Human Decision, one Knowledge Revision; existing active trace
        snapshots and paths are invalidated; an explicit refresh rebuilds."""
        self._require_workspace(workspace_id)
        actor = self._actor(actor)
        note = self._note(note)
        policy = policies.resolve_policy(policy_name)   # typed if unknown
        prior = store.get_workspace_policy(workspace_id)
        if prior and prior["policy_name"] == policy.name and \
                prior["policy_checksum"] == policy.checksum:
            return {"workspace_id": workspace_id, "unchanged": True,
                    "policy_name": policy.name,
                    "policy_checksum": policy.checksum,
                    "knowledge_revision":
                        kg.current_revision_number(workspace_id)}
        with kg.graph_transaction(
                workspace_id, action=RevisionAction.TRACE_POLICY_CHANGE,
                actor=actor,
                summary=f"traceability policy -> {policy.name}") as tx:
            store.set_workspace_policy_tx(
                tx, policy_name=policy.name, policy_source=policy.source,
                policy_checksum=policy.checksum)
            tx.insert_decision(
                decision_type=DecisionType.TRACE_POLICY_CHANGE,
                target_kind=DecisionTargetKind.WORKSPACE,
                target_id=workspace_id, actor=actor, note=note,
                before={"policy_name": (prior or {}).get("policy_name", ""),
                        "policy_checksum":
                            (prior or {}).get("policy_checksum", "")},
                after={"policy_name": policy.name,
                       "policy_checksum": policy.checksum},
                source_command=source_command)
            revision = tx.revision_number
        # Invalidate derived state; the graph itself is untouched.
        staled_paths = store.stale_all_paths(workspace_id)
        staled_snapshots = store.stale_current_snapshots(workspace_id)
        return {"workspace_id": workspace_id, "unchanged": False,
                "policy_name": policy.name,
                "policy_checksum": policy.checksum,
                "staled_paths": staled_paths,
                "staled_snapshots": staled_snapshots,
                "refresh_required": True,
                "knowledge_revision": revision}

    # ======================================================================
    # Refresh
    # ======================================================================
    def plan_refresh(self, workspace_id: str, *,
                     scope: Optional[Dict[str, Any]] = None,
                     force: bool = False) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        reconcile_graph_staleness(workspace_id)
        snapshots.reconcile_trace_staleness(workspace_id)
        policy = self.active_policy(workspace_id)
        return snapshots.plan_refresh(workspace_id, policy, scope=scope,
                                      force=force)

    def refresh(self, workspace_id: str, *,
                scope: Optional[Dict[str, Any]] = None,
                force: bool = False, wait: bool = True,
                timeout: float = 600.0) -> Dict[str, Any]:
        """Run a refresh. ``wait=True`` (default) runs synchronously in
        this process; ``wait=False`` enqueues a ``traceability_refresh``
        job on the shared worker."""
        self._require_workspace(workspace_id)
        reconcile_graph_staleness(workspace_id)
        snapshots.reconcile_trace_staleness(workspace_id)
        policy = self.active_policy(workspace_id)
        if wait:
            return snapshots.run_refresh(workspace_id, policy, scope=scope,
                                         force=force)
        from .. import jobs as jobs_engine
        job = jobs_engine.enqueue_traceability_refresh(
            workspace_id, scope=scope or {}, force=force)
        if self._ensure_worker:
            self._ensure_worker()
        return {"workspace_id": workspace_id, "queued": True, "job": job}

    def get_run(self, workspace_id: str, run_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        run = store.get_run(workspace_id, run_id)
        if not run:
            raise TraceRunNotFound(run_id, workspace_id=workspace_id)
        return run

    def list_runs(self, workspace_id: str, *,
                  status: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_runs(workspace_id, status=status,
                               limit=self._bound(limit),
                               offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "runs": rows,
                "count": len(rows)}

    # ======================================================================
    # Traces
    # ======================================================================
    def trace_requirement(self, workspace_id: str, entity_id: str, *,
                          include_stale: bool = False,
                          max_paths: int = engine.MAX_PATHS_PER_KIND
                          ) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        ctx = engine._WalkContext(workspace_id, policy,
                                  include_stale=include_stale)
        reasons = engine.root_eligibility(ctx, entity)
        if reasons:
            raise TraceRootIneligible(entity_id, reasons)
        result = engine.trace_root(workspace_id, entity, policy,
                                   include_stale=include_stale,
                                   max_paths_per_kind=max_paths, ctx=ctx)
        result["gaps"] = gaps.detect_root_gaps(policy, result)
        return result

    def trace_code(self, workspace_id: str, entity_id: str, *,
                   include_stale: bool = False) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        if entity["entity_type"] not in engine.CODE_TRACE_TYPES:
            raise InvalidRequest(
                f"entity type {entity['entity_type']!r} is not a code "
                f"trace subject (allowed: "
                f"{', '.join(sorted(engine.CODE_TRACE_TYPES))})")
        return engine.trace_code(workspace_id, entity, policy,
                                 include_stale=include_stale)

    def trace_test(self, workspace_id: str, entity_id: str, *,
                   include_stale: bool = False) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        entity = self._require_entity(workspace_id, entity_id)
        if entity["entity_type"] not in engine.TEST_TRACE_TYPES:
            raise InvalidRequest(
                f"entity type {entity['entity_type']!r} is not a test "
                f"trace subject (allowed: "
                f"{', '.join(sorted(engine.TEST_TRACE_TYPES))})")
        return engine.trace_test(workspace_id, entity, policy,
                                 include_stale=include_stale)

    def get_trace_path(self, workspace_id: str,
                       trace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        path = store.get_path(workspace_id, trace_id)
        if not path:
            raise TracePathNotFound(trace_id, workspace_id=workspace_id)
        return path

    # ======================================================================
    # Coverage
    # ======================================================================
    def get_coverage(self, workspace_id: str, *,
                     include_stale: bool = False) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        snapshot = store.latest_snapshot(workspace_id,
                                         current_only=not include_stale)
        if not snapshot:
            return {"workspace_id": workspace_id, "snapshot": None,
                    "status": "unknown",
                    "reason": ("no completed traceability refresh yet; "
                               "run `trace refresh` first")}
        return {"workspace_id": workspace_id, "snapshot": snapshot,
                "status": (snapshot.get("metrics") or {}).get("status",
                                                              "unknown")}

    def list_coverage_snapshots(self, workspace_id: str, *,
                                limit: int = 50) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_snapshots(workspace_id,
                                    limit=self._bound(limit))
        return {"workspace_id": workspace_id, "snapshots": rows,
                "count": len(rows)}

    # ======================================================================
    # Gaps
    # ======================================================================
    def list_gaps(self, workspace_id: str, *,
                  gap_type: Optional[str] = None,
                  status: Optional[str] = None,
                  root_entity_id: Optional[str] = None,
                  severity: Optional[str] = None,
                  limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_gaps(
            workspace_id, gap_type=gap_type, status=status,
            root_entity_id=root_entity_id, severity=severity,
            limit=self._bound(limit), offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "gaps": rows,
                "count": len(rows),
                "open_total": store.count_gaps(workspace_id,
                                               status=GapStatus.OPEN)}

    def get_gap(self, workspace_id: str, gap_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        gap = store.get_gap(workspace_id, gap_id)
        if not gap:
            raise GapNotFound(gap_id, workspace_id=workspace_id)
        return gap

    def _gap_governance(self, workspace_id: str, gap_id: str, *,
                        decision_type: str, new_status: str, actor: str,
                        note: str,
                        metadata_updates: Optional[Dict[str, Any]] = None,
                        allowed_from: Optional[set] = None,
                        extra_fields: Optional[Dict[str, Any]] = None,
                        source_command: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        note = self._note(note)
        gap = store.get_gap(workspace_id, gap_id)
        if not gap:
            raise GapNotFound(gap_id, workspace_id=workspace_id)
        if allowed_from is not None and gap["status"] not in allowed_from:
            from .errors import ConflictStateInvalid
            raise ConflictStateInvalid(
                f"cannot move gap from {gap['status']!r} to "
                f"{new_status!r}",
                details={"gap_id": gap_id, "status": gap["status"]})
        metadata = dict(gap.get("metadata") or {})
        metadata.update(metadata_updates or {})
        with kg.graph_transaction(
                workspace_id, action=RevisionAction.GAP_GOVERNANCE,
                actor=actor, summary=f"gap {new_status}") as tx:
            decision = tx.insert_decision(
                decision_type=decision_type,
                target_kind=DecisionTargetKind.GAP, target_id=gap_id,
                actor=actor, note=note,
                before={"status": gap["status"]},
                after={"status": new_status},
                source_command=source_command)
            fields: Dict[str, Any] = {"status": new_status,
                                      "metadata": metadata,
                                      "resolution_decision_id":
                                          decision["id"]}
            fields.update(extra_fields or {})
            store.update_gap_tx(tx, gap_id, **fields)
            revision = tx.revision_number
        return {"workspace_id": workspace_id,
                "gap": store.get_gap(workspace_id, gap_id),
                "knowledge_revision": revision}

    def resolve_gap(self, workspace_id: str, gap_id: str, *, actor: str,
                    note: str, engine_exception: str = "",
                    source_command: str = "") -> Dict[str, Any]:
        """Explicit resolution. Refused while the current engine still
        detects the gap, unless a documented engine-exception reason is
        supplied. (The normal path is automatic: a later refresh confirms
        the trace now exists and resolves the gap itself.)"""
        self._require_workspace(workspace_id)
        gap = store.get_gap(workspace_id, gap_id)
        if not gap:
            raise GapNotFound(gap_id, workspace_id=workspace_id)
        if not engine_exception:
            policy = self.active_policy(workspace_id)
            still = self._gap_still_detected(workspace_id, policy, gap)
            if still:
                raise InvalidRequest(
                    "the engine still detects this gap; fix the trace and "
                    "refresh, or supply --engine-exception with a "
                    "documented reason",
                    details={"gap_id": gap_id,
                             "gap_type": gap["gap_type"]})
        metadata = {"resolved_by": "explicit"}
        if engine_exception:
            metadata["engine_exception"] = str(engine_exception)[:500]
        return self._gap_governance(
            workspace_id, gap_id, decision_type=DecisionType.GAP_RESOLVE,
            new_status=GapStatus.RESOLVED, actor=actor, note=note,
            metadata_updates=metadata,
            allowed_from={GapStatus.OPEN, GapStatus.ACCEPTED},
            extra_fields={"resolved_at": _now()},
            source_command=source_command)

    def _gap_still_detected(self, workspace_id: str,
                            policy: TraceabilityPolicy,
                            gap: Dict[str, Any]) -> bool:
        root_id = gap["root_entity_id"]
        entity = kg.get_entity(workspace_id, root_id)
        if not entity:
            return False
        if gap["gap_type"].startswith("orphan-"):
            return False        # orphan gaps reconcile on refresh only
        ctx = engine._WalkContext(workspace_id, policy)
        if engine.root_eligibility(ctx, entity):
            return False
        result = engine.trace_root(workspace_id, entity, policy, ctx=ctx)
        detected = gaps.detect_root_gaps(policy, result)
        return any(g["detection_fingerprint"]
                   == gap["detection_fingerprint"] for g in detected)

    def accept_gap(self, workspace_id: str, gap_id: str, *, actor: str,
                   note: str, expires_at: str = "",
                   source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        metadata: Dict[str, Any] = {"accepted_by": actor,
                                    "accepted_at": _now()}
        if expires_at:
            metadata["acceptance_expires_at"] = str(expires_at)
        return self._gap_governance(
            workspace_id, gap_id, decision_type=DecisionType.GAP_ACCEPT,
            new_status=GapStatus.ACCEPTED, actor=actor, note=note,
            metadata_updates=metadata,
            allowed_from={GapStatus.OPEN},
            source_command=source_command)

    def dismiss_gap(self, workspace_id: str, gap_id: str, *, actor: str,
                    note: str, source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return self._gap_governance(
            workspace_id, gap_id, decision_type=DecisionType.GAP_DISMISS,
            new_status=GapStatus.DISMISSED, actor=actor, note=note,
            metadata_updates={"dismissed_reason": str(note)[:500]},
            allowed_from={GapStatus.OPEN, GapStatus.ACCEPTED},
            source_command=source_command)

    def reopen_gap(self, workspace_id: str, gap_id: str, *, actor: str,
                   note: str, source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return self._gap_governance(
            workspace_id, gap_id, decision_type=DecisionType.GAP_REOPEN,
            new_status=GapStatus.OPEN, actor=actor, note=note,
            allowed_from={GapStatus.RESOLVED, GapStatus.ACCEPTED,
                          GapStatus.DISMISSED},
            extra_fields={"resolved_at": None, "stale_at": None},
            source_command=source_command)

    # ======================================================================
    # Orphans
    # ======================================================================
    def find_orphan_requirements(self, workspace_id: str, *,
                                 limit: int = 500) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        return gaps.find_orphan_requirements(workspace_id, policy,
                                             limit=self._bound(limit))

    def find_orphan_code(self, workspace_id: str, *,
                         limit: int = 500) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        return gaps.find_orphan_code(workspace_id, policy,
                                     limit=self._bound(limit))

    def find_orphan_tests(self, workspace_id: str, *,
                          limit: int = 500) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        policy = self.active_policy(workspace_id)
        return gaps.find_orphan_tests(workspace_id, policy,
                                      limit=self._bound(limit))

    def find_orphan_documents(self, workspace_id: str, *,
                              limit: int = 500) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return gaps.find_orphan_documents(workspace_id,
                                          limit=self._bound(limit))

    # ======================================================================
    # Conflicts
    # ======================================================================
    def plan_conflict_scan(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        reconcile_graph_staleness(workspace_id)
        return conflicts.plan_scan(workspace_id)

    def scan_conflicts(self, workspace_id: str, *, actor: str = "",
                       wait: bool = True,
                       timeout: float = 600.0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        reconcile_graph_staleness(workspace_id)
        if wait:
            return conflicts.scan_conflicts(workspace_id,
                                            actor=str(actor or "")[:200])
        from .. import jobs as jobs_engine
        job = jobs_engine.enqueue_conflict_scan(
            workspace_id, actor=str(actor or "")[:200])
        if self._ensure_worker:
            self._ensure_worker()
        return {"workspace_id": workspace_id, "queued": True, "job": job}

    def list_conflicts(self, workspace_id: str, *,
                       status: Optional[str] = None,
                       category: Optional[str] = None,
                       origin: Optional[str] = None,
                       severity: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_conflicts(
            workspace_id, status=status, category=category, origin=origin,
            severity=severity, limit=self._bound(limit),
            offset=max(0, int(offset)))
        return {"workspace_id": workspace_id, "conflicts": rows,
                "count": len(rows),
                "open_total": store.count_conflicts(
                    workspace_id, status=ConflictStatus.OPEN),
                "knowledge_revision":
                    kg.current_revision_number(workspace_id)}

    def get_conflict(self, workspace_id: str,
                     conflict_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        conflict = store.get_conflict(workspace_id, conflict_id)
        if not conflict:
            from .errors import ConflictNotFound
            raise ConflictNotFound(conflict_id, workspace_id=workspace_id)
        return conflict

    # -- promotion ----------------------------------------------------------
    def plan_conflict_promotion(self, workspace_id: str,
                                candidate_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.plan_promotion(workspace_id, candidate_id)

    def promote_conflict_candidate(self, workspace_id: str,
                                   candidate_id: str, *, actor: str,
                                   note: str,
                                   source_command: str = ""
                                   ) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.promote_candidate(
            workspace_id, candidate_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)

    # -- lifecycle ----------------------------------------------------------
    def start_conflict_review(self, workspace_id: str, conflict_id: str, *,
                              actor: str, note: str,
                              source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.start_review(
            workspace_id, conflict_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)

    def accept_conflict_risk(self, workspace_id: str, conflict_id: str, *,
                             actor: str, note: str, expires_at: str = "",
                             follow_up: str = "",
                             source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.accept_risk(
            workspace_id, conflict_id, actor=self._actor(actor),
            note=self._note(note), expires_at=expires_at,
            follow_up=follow_up, source_command=source_command)

    def resolve_conflict(self, workspace_id: str, conflict_id: str, *,
                         actor: str, note: str, resolution_type: str,
                         evidence: Sequence[Dict[str, Any]] = (),
                         source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.resolve(
            workspace_id, conflict_id, actor=self._actor(actor),
            note=self._note(note), resolution_type=resolution_type,
            evidence=evidence, source_command=source_command)

    def dismiss_conflict(self, workspace_id: str, conflict_id: str, *,
                         actor: str, note: str,
                         source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.dismiss(
            workspace_id, conflict_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)

    def reopen_conflict(self, workspace_id: str, conflict_id: str, *,
                        actor: str, note: str,
                        source_command: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        return conflicts.reopen(
            workspace_id, conflict_id, actor=self._actor(actor),
            note=self._note(note), source_command=source_command)

    # ======================================================================
    # Staleness
    # ======================================================================
    def reconcile_staleness(self, workspace_id: str) -> Dict[str, Any]:
        """Graph reconciliation first (Phase 5), then the trace plane.
        Local, deterministic, model-free."""
        self._require_workspace(workspace_id)
        graph_result = reconcile_graph_staleness(workspace_id)
        trace_result = snapshots.reconcile_trace_staleness(workspace_id)
        return {"workspace_id": workspace_id,
                "graph": {"changed": graph_result.get("changed", False)},
                "trace": trace_result,
                "knowledge_revision":
                    kg.current_revision_number(workspace_id)}


__all__ = ["TraceabilityService", "MAX_NOTE_CHARS"]
