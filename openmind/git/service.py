"""GitService — the application service over the Git plane (spec §37).

Exposed as ``runtime.git`` / ``ServiceContainer.git``. Every method is
workspace-scoped and exposes only TYPED operations — there is deliberately no
"run an arbitrary git command" entry point. Baseline coherence is delegated to
:mod:`openmind.git.baseline`, which reads canonical facts through the knowledge
and traceability services (injected lazily to avoid an import cycle).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..domain.errors import InvalidRequest
from .baseline import BaselinePlanner
from .command import default_runner
from .errors import RepositoryNotFound
from .diff import DiffExtractor
from .refs import RefResolver, list_branches
from .repositories import (RepositoryDiscovery, resolve_root,
                           resolve_root_by_key)
from . import store


class GitService:
    """Use cases over local Git repositories and canonical Git baselines."""

    def __init__(self, workspaces: Any,
                 knowledge_provider: Optional[Callable[[], Any]] = None,
                 traceability_provider: Optional[Callable[[], Any]] = None,
                 ) -> None:
        self._workspaces = workspaces
        self._knowledge_provider = knowledge_provider
        self._traceability_provider = traceability_provider
        self._runner = default_runner()

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    def _require_repository(self, workspace_id: str,
                            repository_ref: str) -> Dict[str, Any]:
        """Resolve a repository by key OR id within the workspace."""
        rec = store.find_repository_by_key(workspace_id, repository_ref)
        if rec is None:
            rec = store.get_repository(workspace_id, repository_ref)
        if rec is None:
            raise RepositoryNotFound(repository_ref, workspace_id=workspace_id)
        return rec

    def _resolver(self, workspace_id: str, repository: Dict[str, Any]
                  ) -> RefResolver:
        root = resolve_root(workspace_id, repository)
        return RefResolver(root, self._runner,
                           repository_key=repository.get("repository_key", ""))

    # ======================================================================
    # Repositories
    # ======================================================================
    def discover_repositories(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        found = RepositoryDiscovery(workspace_id, self._runner).discover()
        if len(found) > config.GIT_MAX_REPOSITORIES_PER_WORKSPACE:
            found = found[:config.GIT_MAX_REPOSITORIES_PER_WORKSPACE]
        return {"workspace_id": workspace_id,
                "repositories": [self._repo_view(r) for r in found],
                "count": len(found)}

    def list_repositories(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rows = store.list_repositories(workspace_id)
        return {"workspace_id": workspace_id,
                "repositories": [self._repo_view(r) for r in rows],
                "count": len(rows)}

    def get_repository(self, workspace_id: str,
                       repository_ref: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rec = self._require_repository(workspace_id, repository_ref)
        return {"workspace_id": workspace_id, "repository": self._repo_view(rec)}

    def get_repository_status(self, workspace_id: str,
                              repository_ref: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rec = self._require_repository(workspace_id, repository_ref)
        root = resolve_root(workspace_id, rec)
        resolver = RefResolver(root, self._runner,
                               repository_key=rec.get("repository_key", ""))
        dx = DiffExtractor(root, self._runner)
        head = resolver.head_commit()
        symbolic = resolver.symbolic_head()
        clean = dx.is_worktree_clean()
        return {
            "workspace_id": workspace_id,
            "repository": self._repo_view(rec),
            "status": {
                "headCommit": head or "",
                "branch": symbolic.rsplit("/", 1)[-1] if symbolic else "",
                "detachedHead": symbolic is None,
                "clean": clean,
                "hasUnmerged": dx.has_unmerged(),
                "shallow": resolver.is_shallow(),
            },
        }

    # ======================================================================
    # Refs
    # ======================================================================
    def resolve_ref(self, workspace_id: str, repository_ref: str,
                    ref: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rec = self._require_repository(workspace_id, repository_ref)
        resolver = self._resolver(workspace_id, rec)
        commit = resolver.resolve_commit(ref)
        return {"workspace_id": workspace_id,
                "repository": rec.get("repository_key"),
                "ref": ref, "commit": commit, "tree": resolver.tree_of(commit)}

    def list_branches(self, workspace_id: str,
                      repository_ref: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        rec = self._require_repository(workspace_id, repository_ref)
        root = resolve_root(workspace_id, rec)
        branches = list_branches(root, self._runner)
        return {"workspace_id": workspace_id,
                "repository": rec.get("repository_key"),
                "branches": branches, "count": len(branches)}

    # ======================================================================
    # Baselines
    # ======================================================================
    def plan_baseline(self, workspace_id: str,
                      repository_ref: Optional[str] = None) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        repos = self._baseline_targets(workspace_id, repository_ref)
        plans = []
        for rec in repos:
            planner = BaselinePlanner(
                workspace_id, rec,
                knowledge_service=self._knowledge(),
                traceability_service=self._traceability(),
                runner=self._runner)
            report = planner.plan()
            plans.append({"repository": rec.get("repository_key"),
                          **report.to_dict()})
        return {"workspace_id": workspace_id, "plans": plans,
                "canCapture": all(p["coherent"] for p in plans) and bool(plans)}

    def capture_baseline(self, workspace_id: str,
                         repository_ref: Optional[str] = None, *,
                         actor: str = "") -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        repos = self._baseline_targets(workspace_id, repository_ref)
        captured = []
        blocked = []
        for rec in repos:
            planner = BaselinePlanner(
                workspace_id, rec,
                knowledge_service=self._knowledge(),
                traceability_service=self._traceability(),
                runner=self._runner)
            report = planner.plan()
            if not report.coherent:
                blocked.append({"repository": rec.get("repository_key"),
                                **report.to_dict()})
                continue
            f = report.facts
            existing = store.find_baseline_by_commit(
                workspace_id, rec["id"], f["commit_sha"])
            if existing and existing.get("knowledge_revision") == \
                    f.get("knowledge_revision"):
                # Idempotent: identical capture returns the existing baseline.
                captured.append({"repository": rec.get("repository_key"),
                                 "baseline": existing, "idempotent": True})
                continue
            baseline = store.insert_baseline(
                workspace_id, rec["id"],
                commit_sha=f["commit_sha"], tree_sha=f.get("tree_sha", ""),
                branch_name=f.get("branch_name", ""),
                head_ref=f.get("head_ref", ""),
                knowledge_revision=f.get("knowledge_revision", 0),
                traceability_run_id=f.get("traceability_run_id", ""),
                trace_policy_checksum=f.get("trace_policy_checksum", ""),
                graph_projector_version=f.get("graph_projector_version", ""),
                trace_engine_version=f.get("trace_engine_version", ""),
                asset_state_hash=f.get("asset_state_hash", ""),
                metadata={"actor": str(actor or "")[:200],
                          "traceability_snapshot_present":
                              f.get("traceability_snapshot_present", False),
                          "detached_head": f.get("detached_head", False)})
            captured.append({"repository": rec.get("repository_key"),
                             "baseline": baseline, "idempotent": False})
        return {"workspace_id": workspace_id, "captured": captured,
                "blocked": blocked,
                "ok": bool(captured) and not blocked}

    def get_baseline(self, workspace_id: str, baseline_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        baseline = store.get_baseline(workspace_id, baseline_id)
        if not baseline:
            raise RepositoryNotFound(baseline_id, workspace_id=workspace_id)
        return {"workspace_id": workspace_id, "baseline": baseline}

    def list_baselines(self, workspace_id: str,
                       repository_ref: Optional[str] = None) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        repo_id = None
        if repository_ref:
            repo_id = self._require_repository(workspace_id, repository_ref)["id"]
        rows = store.list_baselines(workspace_id, repo_id)
        return {"workspace_id": workspace_id, "baselines": rows,
                "count": len(rows)}

    # ======================================================================
    # internals
    # ======================================================================
    def _baseline_targets(self, workspace_id: str,
                          repository_ref: Optional[str]) -> List[Dict[str, Any]]:
        if repository_ref:
            return [self._require_repository(workspace_id, repository_ref)]
        rows = store.list_repositories(workspace_id)
        if not rows:
            # Auto-discover once so a first `baseline capture` works without a
            # separate discover step.
            rows = RepositoryDiscovery(workspace_id, self._runner).discover()
        if not rows:
            raise InvalidRequest(
                "no git repository found for this workspace; register a source "
                "path that lives in a git repository first",
                details={"workspace_id": workspace_id})
        return rows

    def _knowledge(self):
        if self._knowledge_provider is None:
            raise InvalidRequest("knowledge service is not available")
        return self._knowledge_provider()

    def _traceability(self):
        if self._traceability_provider is None:
            raise InvalidRequest("traceability service is not available")
        return self._traceability_provider()

    @staticmethod
    def _repo_view(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": rec["id"],
            "repositoryKey": rec["repository_key"],
            "relativeRoot": rec["relative_root"],
            "objectFormat": rec["object_format"],
            "isBare": rec["is_bare"],
            "defaultBranch": rec["default_branch"],
            "detachedHead": (rec.get("metadata") or {}).get("detached_head", False),
        }


__all__ = ["GitService"]
