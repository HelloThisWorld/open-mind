"""Canonical Git Baseline coherence and capture (spec §10).

A baseline pins one repository commit to the canonical Base Workspace state
(Knowledge Revision, Trace Policy checksum, projector/engine versions, and a
hash of the current Asset-Revision set) so an overlay can be computed against a
graph that actually corresponds to that commit. Capture is refused for an
incoherent state — a dirty worktree is never silently baselined.

This module reads canonical facts through the runtime's knowledge and
traceability services; it does not import their stores directly beyond the
Asset-state hash, keeping the coherence policy in one place.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import machine
from .command import GitCommandRunner, default_runner
from .diff import DiffExtractor
from .errors import BaselineDirty, BaseKnowledgeRevisionMissing
from .refs import RefResolver
from .repositories import resolve_root


@dataclass
class CoherenceReport:
    """The outcome of checking whether a baseline may be captured."""
    coherent: bool = False
    reasons: List[str] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.coherent = False

    def to_dict(self) -> Dict[str, Any]:
        return {"coherent": self.coherent, "reasons": list(self.reasons),
                "facts": dict(self.facts)}


def asset_state_hash(workspace_id: str) -> str:
    """A deterministic hash of the current Asset-Revision set — the canonical
    content anchor a baseline records. Empty string when the graph is empty."""
    try:
        from ..knowledge import store as kstore
        rev_ids = sorted(kstore.current_revision_ids(workspace_id))
    except Exception:
        rev_ids = []
    if not rev_ids:
        return ""
    h = hashlib.sha256()
    for rid in rev_ids:
        h.update(rid.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class BaselinePlanner:
    """Gathers coherence facts for one repository in one workspace."""

    def __init__(self, workspace_id: str, repository: Dict[str, Any], *,
                 knowledge_service, traceability_service,
                 runner: Optional[GitCommandRunner] = None) -> None:
        self.workspace_id = workspace_id
        self.repository = repository
        self.knowledge = knowledge_service
        self.traceability = traceability_service
        self.runner = runner or default_runner()

    def plan(self) -> CoherenceReport:
        report = CoherenceReport(coherent=True)
        # 1. repository locally available
        try:
            root = resolve_root(self.workspace_id, self.repository)
        except Exception as exc:  # RepositoryUnavailable
            report.block("repository_not_available_locally")
            report.facts["error"] = str(getattr(exc, "message", exc))
            return report
        report.facts["repository_key"] = self.repository.get("repository_key")

        # 2. HEAD resolves to a commit
        resolver = RefResolver(root, self.runner,
                               repository_key=self.repository.get("repository_key", ""))
        head = resolver.head_commit()
        if not head:
            report.block("head_does_not_resolve")
            return report
        report.facts["commit_sha"] = head
        report.facts["tree_sha"] = resolver.tree_of(head)
        symbolic = resolver.symbolic_head()
        report.facts["branch_name"] = (symbolic.rsplit("/", 1)[-1]
                                       if symbolic else "")
        report.facts["head_ref"] = symbolic or "HEAD"
        report.facts["detached_head"] = symbolic is None

        # 3. worktree + index clean (spec §10.2)
        dx = DiffExtractor(root, self.runner)
        try:
            if dx.has_unmerged():
                report.block("baseline_dirty")
                report.facts["dirty_reason"] = "unmerged_entries"
            elif not dx.is_worktree_clean():
                report.block("baseline_dirty")
                report.facts["dirty_reason"] = "worktree_or_index_dirty"
        except Exception:
            report.block("worktree_status_unavailable")

        # 4. canonical graph facts
        try:
            rev = self.knowledge.get_current_revision(self.workspace_id)
            report.facts["knowledge_revision"] = rev.get("knowledge_revision", 0)
        except Exception:
            report.facts["knowledge_revision"] = 0
        if not report.facts.get("knowledge_revision"):
            report.block("base_knowledge_revision_missing")
        try:
            stats = self.knowledge.get_stats(self.workspace_id)
            report.facts["graph_projector_version"] = \
                (stats.get("projection") or {}).get("projector_version", "")
        except Exception:
            report.facts["graph_projector_version"] = ""

        # 5. traceability facts (a snapshot may legitimately be absent — spec
        #    §10.10 — in which case the baseline records that it is absent).
        try:
            policy = self.traceability.active_policy(self.workspace_id)
            report.facts["trace_policy_checksum"] = getattr(policy, "checksum", "")
        except Exception:
            report.facts["trace_policy_checksum"] = ""
        try:
            from ..traceability import TRACE_ENGINE_VERSION
            report.facts["trace_engine_version"] = TRACE_ENGINE_VERSION
        except Exception:
            report.facts["trace_engine_version"] = ""
        try:
            coverage = self.traceability.get_coverage(self.workspace_id)
            snap = coverage.get("snapshot")
            report.facts["traceability_run_id"] = (snap or {}).get("run_id", "")
            report.facts["traceability_snapshot_present"] = bool(snap)
        except Exception:
            report.facts["traceability_run_id"] = ""
            report.facts["traceability_snapshot_present"] = False

        report.facts["asset_state_hash"] = asset_state_hash(self.workspace_id)
        return report


__all__ = ["CoherenceReport", "BaselinePlanner", "asset_state_hash"]
