"""OverlayService — the application service over the Git Overlay plane (spec §37).

Exposed as ``runtime.overlays`` / ``ServiceContainer.overlays``. Orchestrates the
full pipeline (validate baseline coherence -> build files/segments/evidence ->
project graph deltas -> requirement/test/trace/gap impact -> conflict impact ->
risk -> report) and every read/lifecycle operation, all workspace-scoped and
all read-only with respect to Git and the canonical Base Workspace.

The build runs synchronously here (``create``/``refresh`` with wait semantics);
it makes zero provider calls and zero canonical writes.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..domain.errors import InvalidRequest
from ..git import store as git_store
from ..git.errors import (BaseCommitMismatch, BaselineNotCaptured,
                          RepositoryNotFound)
from ..git.repositories import resolve_root
from ..git.vocabularies import OverlayKind, OverlayState, require
from . import store as ovl_store
from . import report as report_mod
from .builder import OverlayBuilder, RepoPlan
from .conflict_impact import ConflictImpactAnalyzer
from .projector import OverlayProjector
from .risk import RiskClassifier
from .trace_impact import TraceImpactAnalyzer


class OverlayService:
    """Use cases over isolated Git overlays."""

    def __init__(self, workspaces: Any, jobs: Any, git_service: Any,
                 knowledge_provider: Optional[Callable[[], Any]] = None,
                 traceability_provider: Optional[Callable[[], Any]] = None,
                 ensure_worker: Optional[Callable[[], None]] = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        self._git = git_service
        self._knowledge_provider = knowledge_provider
        self._traceability_provider = traceability_provider
        self._ensure_worker = ensure_worker

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    def _require_overlay(self, workspace_id: str,
                         overlay_id: str) -> Dict[str, Any]:
        overlay = ovl_store.get_overlay(workspace_id, overlay_id)
        if not overlay:
            raise InvalidRequest(f"overlay not found: {overlay_id!r}",
                                 details={"overlay_id": overlay_id,
                                          "workspace_id": workspace_id})
        return overlay

    def _knowledge(self):
        return self._knowledge_provider() if self._knowledge_provider else None

    def _traceability(self):
        return self._traceability_provider() if self._traceability_provider else None

    # ======================================================================
    # Planning & creation
    # ======================================================================
    def plan_overlay(self, workspace_id: str, *, kind: str,
                     repositories: List[Dict[str, str]]) -> Dict[str, Any]:
        """Validate a proposed overlay without creating it. Reports every
        repository/ref validation error (atomic — spec §33)."""
        self._require_workspace(workspace_id)
        kind = require(OverlayKind, kind, field="overlay kind")
        resolved, errors = self._resolve_plans(workspace_id, kind, repositories)
        return {"workspace_id": workspace_id, "kind": kind,
                "repositories": [self._plan_view(p) for p in resolved],
                "errors": errors, "valid": not errors and bool(resolved)}

    def create_overlay(self, workspace_id: str, *, kind: str,
                       repositories: List[Dict[str, str]], name: str = "",
                       options: Optional[Dict[str, Any]] = None,
                       allow_partial_repositories: bool = False
                       ) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        kind = require(OverlayKind, kind, field="overlay kind")
        options = dict(options or {})
        resolved, errors = self._resolve_plans(workspace_id, kind, repositories)
        if errors and not allow_partial_repositories:
            raise InvalidRequest(
                "overlay plan has repository/ref errors; no overlay created",
                details={"errors": errors})
        if not resolved:
            raise InvalidRequest("no valid repository in overlay plan",
                                 details={"errors": errors})
        base_kr, base_policy, base_run = self._base_coherence(workspace_id,
                                                              resolved)
        overlay = ovl_store.create_overlay(
            workspace_id, overlay_kind=kind, name=name,
            base_knowledge_revision=base_kr, base_policy_checksum=base_policy,
            base_traceability_run_id=base_run, options=options,
            state=OverlayState.BUILDING)
        return self._build(workspace_id, overlay, resolved, kind, options,
                           partial=bool(errors), errors=errors)

    def refresh_overlay(self, workspace_id: str, overlay_id: str
                        ) -> Dict[str, Any]:
        overlay = self._require_overlay(workspace_id, overlay_id)
        opts = overlay.get("options", {})
        kind = overlay["overlay_kind"]
        repositories = opts.get("repositories", [])
        resolved, errors = self._resolve_plans(workspace_id, kind, repositories)
        if not resolved:
            raise InvalidRequest("overlay has no resolvable repositories",
                                 details={"errors": errors})
        base_kr, base_policy, base_run = self._base_coherence(workspace_id,
                                                              resolved)
        # Base moved => stale, not silently reinterpreted (spec §31).
        if base_kr != overlay["base_knowledge_revision"]:
            ovl_store.set_state(workspace_id, overlay_id, OverlayState.STALE)
            return {**self.get_overlay(workspace_id, overlay_id),
                    "refreshed": False, "reason": "base_knowledge_revision_moved"}
        return self._build(workspace_id, overlay, resolved, kind,
                           opts, partial=bool(errors), errors=errors,
                           refreshing=True)

    # -- the build ----------------------------------------------------------
    def _build(self, workspace_id: str, overlay: Dict[str, Any],
               plans: List[RepoPlan], kind: str, options: Dict[str, Any], *,
               partial: bool = False, errors: Optional[List] = None,
               refreshing: bool = False) -> Dict[str, Any]:
        overlay_id = overlay["id"]
        # Source hash for incrementality (spec §31). The injected 'repositories'
        # bookkeeping key is metadata, not a semantic option, so it is excluded
        # from the hash — otherwise a refresh (which carries it in options)
        # would never match the initial build (which does not).
        hash_options = {k: v for k, v in options.items()
                        if k != "repositories"}
        detector_versions = self._detector_versions()
        temp_builder = OverlayBuilder(workspace_id, overlay_id,
                                      overlay["overlay_revision"] + 1)
        # For working-tree, worktree_hash is filled during build; compute a
        # pre-hash pass so a no-op refresh is detectable.
        pre_source = temp_builder.source_hash(
            plans, kind=kind,
            base_knowledge_revision=overlay["base_knowledge_revision"],
            base_policy_checksum=overlay["base_policy_checksum"],
            projector_version=self._projector_version(),
            trace_engine_version=self._trace_engine_version(),
            detector_versions=detector_versions, options=hash_options)
        if refreshing and kind != OverlayKind.WORKING_TREE \
                and pre_source == overlay.get("source_hash") \
                and overlay["state"] == OverlayState.READY:
            return {**self.get_overlay(workspace_id, overlay_id),
                    "refreshed": False, "reason": "unchanged"}

        revision = ovl_store.next_revision(workspace_id, overlay_id)
        # Clear prior file/segment/evidence + derived data for a clean rebuild.
        self._clear_build(overlay_id)
        builder = OverlayBuilder(workspace_id, overlay_id, revision)
        from .builder import BuildResult
        result = BuildResult()
        # persist repositories + files/segments/evidence
        for plan in plans:
            builder.build_repo(plan, kind=kind, result=result)
        # recompute source hash including any worktree hashes now known
        source_hash = builder.source_hash(
            plans, kind=kind,
            base_knowledge_revision=overlay["base_knowledge_revision"],
            base_policy_checksum=overlay["base_policy_checksum"],
            projector_version=self._projector_version(),
            trace_engine_version=self._trace_engine_version(),
            detector_versions=detector_versions, options=hash_options)

        # Projection + impact (all read-only wrt canonical).
        overlay_repos = ovl_store.list_overlay_repositories(overlay_id)
        proj = OverlayProjector(workspace_id, overlay_id).project(overlay_repos)
        knowledge = self._knowledge()
        traceability = self._traceability()
        req_summary = {}
        if traceability is not None and knowledge is not None:
            try:
                req_summary = TraceImpactAnalyzer(
                    workspace_id, overlay_id, knowledge_service=knowledge,
                    traceability_service=traceability).analyze()
            except Exception as exc:
                result.warn(f"trace impact partial: {exc}")
        conflict_summary = {}
        try:
            conflict_summary = ConflictImpactAnalyzer(
                workspace_id, overlay_id).analyze()
        except Exception as exc:
            result.warn(f"conflict impact partial: {exc}")
        risk = RiskClassifier(workspace_id, overlay_id,
                              knowledge_service=knowledge).classify()

        # Persist summary + report.
        options_with_repos = dict(options)
        options_with_repos["repositories"] = [self._plan_repo_spec(p, kind)
                                              for p in plans]
        summary = {
            "files": result.file_count, "segments": result.segment_count,
            "evidence": result.evidence_count,
            "entityDeltas": proj.get("entity_deltas", 0),
            "requirementImpact": req_summary, "conflictImpact": conflict_summary,
            "overallRisk": risk["overallRisk"], "partial": partial or result.partial,
            "warnings": result.warnings,
        }
        ovl_store.update_overlay(workspace_id, overlay_id,
                                 source_hash=source_hash, options=options_with_repos,
                                 summary=summary)
        overlay = ovl_store.get_overlay(workspace_id, overlay_id)
        report = report_mod.build_report(workspace_id, overlay, risk=risk)
        rhash = report_mod.report_hash(report)
        markdown = report_mod.render_markdown(report)
        from .. import content_store
        md_hash = content_store.put(workspace_id, markdown.encode("utf-8"))
        ovl_store.save_report(overlay_id, overlay_revision=revision,
                              report_schema_version=report["schemaVersion"],
                              report=report, report_hash=rhash,
                              markdown_blob_hash=md_hash)
        ovl_store.set_state(workspace_id, overlay_id, OverlayState.READY)
        return {**self.get_overlay(workspace_id, overlay_id),
                "refreshed": True, "report_hash": rhash,
                "summary": summary}

    def _clear_build(self, overlay_id: str) -> None:
        """Remove prior files/segments/evidence/derived data for a rebuild.
        Only overlay data — never canonical."""
        from .. import db
        conn, lock = db.shared_connection()
        with lock:
            for table in ("git_overlay_evidence", "git_overlay_segments",
                          "git_overlay_files", "git_overlay_repositories"):
                conn.execute(f"DELETE FROM {table} WHERE overlay_id=?",
                             (overlay_id,))
            conn.commit()
        ovl_store.clear_derived(overlay_id)

    # ======================================================================
    # Reads
    # ======================================================================
    def list_overlays(self, workspace_id: str, *, state: Optional[str] = None
                      ) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = ovl_store.list_overlays(workspace_id, state=state)
        return {"workspace_id": workspace_id,
                "overlays": [self._overlay_view(o) for o in rows],
                "count": len(rows)}

    def get_overlay(self, workspace_id: str, overlay_id: str) -> Dict[str, Any]:
        overlay = self._require_overlay(workspace_id, overlay_id)
        return {"workspace_id": workspace_id,
                "overlay": self._overlay_view(overlay)}

    def list_overlay_files(self, workspace_id: str, overlay_id: str, *,
                           change_type: Optional[str] = None) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        files = ovl_store.list_files(overlay_id, change_type=change_type)
        return {"overlay_id": overlay_id,
                "files": [report_mod._file_view(f) for f in files],
                "count": len(files)}

    def get_overlay_file(self, workspace_id: str, overlay_id: str,
                         file_id: str) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        f = ovl_store.get_file(overlay_id, file_id)
        if not f:
            raise InvalidRequest(f"overlay file not found: {file_id!r}")
        segments = ovl_store.list_segments(overlay_id, overlay_file_id=file_id)
        return {"overlay_id": overlay_id, "file": report_mod._file_view(f),
                "changedRanges": f.get("changed_ranges", {}),
                "segments": [{"side": s["side"], "changeClass": s["change_class"],
                              "symbol": s["symbol"], "startLine": s["start_line"],
                              "endLine": s["end_line"],
                              "segmentType": s["segment_type"]}
                             for s in segments]}

    def get_overlay_evidence(self, workspace_id: str, overlay_id: str,
                             evidence_id: str) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        ev = ovl_store.get_evidence(overlay_id, evidence_id)
        if not ev:
            raise InvalidRequest(f"overlay evidence not found: {evidence_id!r}")
        return {"overlay_id": overlay_id, "evidence": {
            "id": ev["id"], "side": ev["side"], "locator": ev["locator"],
            "excerpt": ev["excerpt"], "contentHash": ev["content_hash"]}}

    def get_impact_report(self, workspace_id: str, overlay_id: str, *,
                          overlay_revision: Optional[int] = None
                          ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rep = ovl_store.latest_report(overlay_id,
                                      overlay_revision=overlay_revision)
        if not rep:
            raise InvalidRequest("no report for this overlay yet")
        return {"overlay_id": overlay_id, "reportHash": rep["report_hash"],
                "report": rep["report"]}

    def get_diff_summary(self, workspace_id: str, overlay_id: str
                         ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        files = ovl_store.list_files(overlay_id)
        return {"overlay_id": overlay_id,
                "fileSummary": report_mod._file_summary(files),
                "changedFiles": [report_mod._file_view(f) for f in files]}

    def list_impacted_requirements(self, workspace_id: str, overlay_id: str
                                   ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rows = [t for t in ovl_store.list_trace_impacts(overlay_id)
                if t["impact_type"] != "test-impacted"]
        return {"overlay_id": overlay_id,
                "requirements": [report_mod._req_view(t) for t in rows],
                "count": len(rows)}

    def list_impacted_tests(self, workspace_id: str, overlay_id: str
                            ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rows = [t for t in ovl_store.list_trace_impacts(overlay_id)
                if t["impact_type"] == "test-impacted"]
        return {"overlay_id": overlay_id,
                "tests": [report_mod._test_view(t) for t in rows],
                "count": len(rows)}

    def list_trace_impacts(self, workspace_id: str, overlay_id: str
                           ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rows = ovl_store.list_trace_impacts(overlay_id)
        return {"overlay_id": overlay_id,
                "traceImpacts": [report_mod._trace_view(t) for t in rows],
                "count": len(rows)}

    def list_gap_impacts(self, workspace_id: str, overlay_id: str
                         ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rows = ovl_store.list_trace_impacts(overlay_id)
        return {"overlay_id": overlay_id,
                "gapImpact": report_mod._gap_impact(rows)}

    def list_conflict_impacts(self, workspace_id: str, overlay_id: str
                              ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        rows = ovl_store.list_conflict_impacts(overlay_id)
        return {"overlay_id": overlay_id,
                "conflictImpacts": [report_mod._conflict_view(c) for c in rows],
                "count": len(rows)}

    def search_overlay(self, workspace_id: str, overlay_id: str, query: str, *,
                       limit: int = 10) -> Dict[str, Any]:
        from .search import search_overlay
        self._require_overlay(workspace_id, overlay_id)
        return search_overlay(workspace_id, overlay_id, query, limit=limit)

    # ======================================================================
    # Lifecycle
    # ======================================================================
    def close_overlay(self, workspace_id: str, overlay_id: str) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        ovl_store.set_state(workspace_id, overlay_id, OverlayState.CLOSED)
        self._drop_collections(workspace_id, overlay_id)
        return self.get_overlay(workspace_id, overlay_id)

    def abandon_overlay(self, workspace_id: str, overlay_id: str
                        ) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        ovl_store.set_state(workspace_id, overlay_id, OverlayState.ABANDONED)
        self._drop_collections(workspace_id, overlay_id)
        return self.get_overlay(workspace_id, overlay_id)

    def delete_overlay(self, workspace_id: str, overlay_id: str) -> Dict[str, Any]:
        self._require_overlay(workspace_id, overlay_id)
        self._drop_collections(workspace_id, overlay_id)
        ok = ovl_store.delete_overlay(workspace_id, overlay_id)
        return {"overlay_id": overlay_id, "deleted": ok}

    def reconcile_overlay(self, workspace_id: str, overlay_id: str, *,
                          actor: str = "", note: str = "") -> Dict[str, Any]:
        from .reconcile import reconcile
        overlay = self._require_overlay(workspace_id, overlay_id)
        return reconcile(workspace_id, overlay, git_service=self._git,
                         knowledge=self._knowledge(),
                         traceability=self._traceability(),
                         actor=actor, note=note)

    def export_impact_packet(self, workspace_id: str, overlay_id: str,
                             output_dir: str) -> Dict[str, Any]:
        from .packet import export_packet
        self._require_overlay(workspace_id, overlay_id)
        return export_packet(workspace_id, overlay_id, output_dir)

    # ======================================================================
    # internals
    # ======================================================================
    def _drop_collections(self, workspace_id: str, overlay_id: str) -> None:
        try:
            from .search import drop_overlay_collections
            drop_overlay_collections(workspace_id, overlay_id)
        except Exception:
            pass

    def _resolve_plans(self, workspace_id: str, kind: str,
                       repositories: List[Dict[str, str]]):
        resolved: List[RepoPlan] = []
        errors: List[Dict[str, str]] = []
        builder = OverlayBuilder(workspace_id, "plan", 0)
        for spec in repositories or []:
            repo_ref = spec.get("repository") or spec.get("repository_key") or "git:."
            try:
                rec = git_store.find_repository_by_key(workspace_id, repo_ref) \
                    or git_store.get_repository(workspace_id, repo_ref)
                if not rec:
                    # attempt discovery once
                    from ..git.repositories import RepositoryDiscovery
                    RepositoryDiscovery(workspace_id).discover()
                    rec = git_store.find_repository_by_key(workspace_id, repo_ref)
                if not rec:
                    raise RepositoryNotFound(repo_ref, workspace_id=workspace_id)
                plan = builder.resolve_repo_plan(
                    rec, kind=kind, base_ref=spec.get("base", ""),
                    head_ref=spec.get("head", ""),
                    target_branch=spec.get("target_branch", spec.get("base", "")))
                resolved.append(plan)
            except Exception as exc:
                errors.append({"repository": repo_ref,
                               "error": getattr(exc, "message", str(exc)),
                               "code": getattr(exc, "code", "error")})
        return resolved, errors

    def _base_coherence(self, workspace_id: str, plans: List[RepoPlan]):
        """Resolve base knowledge revision + policy checksum from the captured
        baseline, verifying the base commit matches (spec §10)."""
        knowledge_revision = 0
        policy_checksum = ""
        run_id = ""
        for plan in plans:
            repo = plan.repository
            baseline = git_store.latest_baseline(workspace_id, repo["id"])
            if not baseline:
                raise BaselineNotCaptured(
                    f"no baseline captured for {repo['repository_key']}; run "
                    f"`git baseline capture` first",
                    details={"repository": repo["repository_key"]})
            base_commit = plan.merge_base_commit or plan.base_commit
            # For branch/PR the target-branch tip should equal the baseline
            # commit; for commit-range the base commit should. A mismatch means
            # the canonical graph does not correspond to this base.
            if baseline["commit_sha"] and base_commit and \
                    baseline["commit_sha"] != base_commit and \
                    baseline["commit_sha"] != plan.base_commit:
                raise BaseCommitMismatch(
                    f"overlay base commit {base_commit[:12]} does not match the "
                    f"captured baseline {baseline['commit_sha'][:12]} for "
                    f"{repo['repository_key']}",
                    details={"repository": repo["repository_key"],
                             "baseCommit": base_commit,
                             "baselineCommit": baseline["commit_sha"]})
            knowledge_revision = max(knowledge_revision,
                                     baseline["knowledge_revision"])
            policy_checksum = policy_checksum or baseline["trace_policy_checksum"]
            run_id = run_id or baseline["traceability_run_id"]
        return knowledge_revision, policy_checksum, run_id

    def _projector_version(self) -> str:
        try:
            from ..knowledge import GRAPH_PROJECTOR_VERSION
            return GRAPH_PROJECTOR_VERSION
        except Exception:
            return ""

    def _trace_engine_version(self) -> str:
        try:
            from ..traceability import TRACE_ENGINE_VERSION
            return TRACE_ENGINE_VERSION
        except Exception:
            return ""

    def _detector_versions(self) -> str:
        try:
            from ..traceability.detectors import ALL_DETECTORS
            return ",".join(sorted(f"{d.name}:{d.version}"
                                   for d in ALL_DETECTORS))
        except Exception:
            return ""

    @staticmethod
    def _plan_repo_spec(plan: RepoPlan, kind: str) -> Dict[str, str]:
        return {"repository": plan.repository.get("repository_key", ""),
                "base": plan.base_ref, "head": plan.head_ref,
                "target_branch": plan.target_branch}

    @staticmethod
    def _plan_view(plan: RepoPlan) -> Dict[str, Any]:
        return {"repository": plan.repository.get("repository_key", ""),
                "baseCommit": plan.base_commit, "headCommit": plan.head_commit,
                "mergeBase": plan.merge_base_commit,
                "branch": plan.branch_name, "targetBranch": plan.target_branch}

    @staticmethod
    def _overlay_view(overlay: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": overlay["id"], "name": overlay.get("name", ""),
            "kind": overlay["overlay_kind"], "state": overlay["state"],
            "overlayRevision": overlay["overlay_revision"],
            "baseKnowledgeRevision": overlay["base_knowledge_revision"],
            "basePolicyChecksum": overlay["base_policy_checksum"],
            "sourceHash": overlay["source_hash"],
            "summary": overlay.get("summary", {}),
            "createdAt": overlay["created_at"], "readyAt": overlay.get("ready_at"),
        }


__all__ = ["OverlayService"]
