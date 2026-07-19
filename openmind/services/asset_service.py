"""Asset model use cases — read the canonical Asset/Revision/Segment/Evidence
store, and drive a single-file sync into it.

Exposed as ``runtime.assets`` and ``ServiceContainer.assets``, and shared by the
CLI, the FastAPI adapter and the MCP server, so the same queries are reachable
without constructing an HTTP request.

WORKSPACE SCOPING IS ENFORCED ON EVERY READ
-------------------------------------------
Every method takes a ``workspace_id`` and validates it first (raising
:class:`WorkspaceNotFound`), then reads through the workspace-scoped repository
queries — so an Asset/Revision/Segment/Evidence id from workspace A can never be
read through workspace B (it simply resolves to nothing and raises the typed
not-found). The service raises typed domain errors, never ``HTTPException``, so
every adapter maps them its own way.

EVIDENCE IS RECOVERED FROM THE IMMUTABLE SNAPSHOT, NOT A MODEL
-------------------------------------------------------------
:meth:`get_evidence` reconstructs the cited content from the immutable content
blob and reports, honestly and separately, whether the snapshot is available or
corrupt and whether the live source still matches, has changed, or is missing.
No running model is involved.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .. import config, content_store, db as db_module, machine, segmentation, walker
from ..domain.errors import (AssetNotFound, ContentCorruption, EvidenceNotFound,
                             InvalidRequest, RevisionNotFound, SegmentNotFound)
from ..domain.types import (Asset, AssetRevision, AssetState, AssetType,
                            Evidence, Segment)


class AssetService:
    """Read/query use cases over the canonical Asset model."""

    #: Hard upper bounds so no adapter can request an unbounded page.
    MAX_ASSET_LIMIT = 500
    MAX_SEGMENT_LIMIT = 1000
    MAX_REVISION_LIMIT = 500
    MAX_EVIDENCE_CHARS = 100_000

    def __init__(self, workspaces: Any, ingest: Any, repo: Any = None,
                 content: Any = None) -> None:
        self._workspaces = workspaces
        self._ingest = ingest
        self._repo: Any = repo if repo is not None else db_module
        self._content: Any = content if content is not None else content_store

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        # Raises WorkspaceNotFound for an unknown or deleting workspace, so every
        # asset read fails the same honest way a bad workspace id does elsewhere.
        return self._workspaces.get(workspace_id)

    @staticmethod
    def _bound(limit: int, hard: int) -> int:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = hard
        return max(1, min(limit, hard))

    # -- asset reads --------------------------------------------------------
    def list_assets(self, workspace_id: str, asset_type: Optional[str] = None,
                    state: Optional[str] = None, limit: int = 100,
                    offset: int = 0) -> Dict[str, Any]:
        """A bounded page of the workspace's assets plus the total count.

        ``asset_type`` / ``state`` are validated against the closed vocabularies;
        an unknown value is a caller error, not a silent empty page.
        """
        self._require_workspace(workspace_id)
        if asset_type is not None and asset_type not in AssetType.VALUES:
            raise InvalidRequest(
                f"unknown asset_type: {asset_type!r}",
                details={"asset_type": asset_type,
                         "allowed": sorted(AssetType.VALUES)})
        if state is not None and state not in AssetState.VALUES:
            raise InvalidRequest(
                f"unknown state: {state!r}",
                details={"state": state, "allowed": sorted(AssetState.VALUES)})
        limit = self._bound(limit, self.MAX_ASSET_LIMIT)
        offset = max(0, int(offset))
        rows = self._repo.list_assets(workspace_id, asset_type=asset_type,
                                      state=state, limit=limit, offset=offset)
        total = self._repo.count_assets(workspace_id, asset_type=asset_type,
                                        state=state)
        return {
            "workspace_id": workspace_id,
            "assets": [Asset.from_row(r).as_dict() for r in rows],
            "total": total, "limit": limit, "offset": offset,
            "count": len(rows),
        }

    def get_asset(self, workspace_id: str, asset_id: str) -> Dict[str, Any]:
        """An asset plus a summary of its current revision."""
        self._require_workspace(workspace_id)
        row = self._repo.get_asset(workspace_id, asset_id)
        if not row:
            raise AssetNotFound(asset_id, workspace_id=workspace_id)
        return self._asset_with_current(workspace_id, row)

    def get_asset_by_logical_key(self, workspace_id: str,
                                 logical_key: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = self._repo.find_asset_by_logical_key(workspace_id, logical_key)
        if not row:
            raise AssetNotFound(logical_key, workspace_id=workspace_id)
        return self._asset_with_current(workspace_id, row)

    def _asset_with_current(self, workspace_id: str,
                            row: Dict[str, Any]) -> Dict[str, Any]:
        out = Asset.from_row(row).as_dict()
        current = None
        if row.get("current_revision_id"):
            rev = self._repo.get_revision(workspace_id, row["current_revision_id"])
            if rev:
                current = AssetRevision.from_row(rev).as_dict()
                current["segment_count"] = self._repo.count_segments(
                    workspace_id, rev["id"])
        out["current_revision"] = current
        return out

    # -- revision reads -----------------------------------------------------
    def list_revisions(self, workspace_id: str, asset_id: str,
                       limit: int = 50) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if not self._repo.get_asset(workspace_id, asset_id):
            raise AssetNotFound(asset_id, workspace_id=workspace_id)
        limit = self._bound(limit, self.MAX_REVISION_LIMIT)
        rows = self._repo.list_revisions(workspace_id, asset_id, limit=limit)
        return {
            "workspace_id": workspace_id, "asset_id": asset_id,
            "revisions": [AssetRevision.from_row(r).as_dict() for r in rows],
            "count": len(rows), "limit": limit,
        }

    def get_revision(self, workspace_id: str,
                     revision_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = self._repo.get_revision(workspace_id, revision_id)
        if not row:
            raise RevisionNotFound(revision_id, workspace_id=workspace_id)
        out = AssetRevision.from_row(row).as_dict()
        out["segment_count"] = self._repo.count_segments(workspace_id, revision_id)
        return out

    # -- segment reads ------------------------------------------------------
    def list_segments(self, workspace_id: str, revision_id: str,
                      limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        if not self._repo.get_revision(workspace_id, revision_id):
            raise RevisionNotFound(revision_id, workspace_id=workspace_id)
        limit = self._bound(limit, self.MAX_SEGMENT_LIMIT)
        offset = max(0, int(offset))
        rows = self._repo.list_segments(workspace_id, revision_id, limit=limit,
                                        offset=offset)
        total = self._repo.count_segments(workspace_id, revision_id)
        # Attach each segment's evidence id (one scoped query) so callers can
        # follow a segment straight to `get_evidence`.
        ev_by_seg = self._repo.evidence_ids_for_revision(workspace_id, revision_id)
        segments = []
        for r in rows:
            d = Segment.from_row(r).as_dict()
            d["evidence_id"] = ev_by_seg.get(r["id"])
            segments.append(d)
        return {
            "workspace_id": workspace_id, "revision_id": revision_id,
            "segments": segments,
            "total": total, "count": len(rows), "limit": limit, "offset": offset,
        }

    def get_segment(self, workspace_id: str, segment_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        row = self._repo.get_segment(workspace_id, segment_id)
        if not row:
            raise SegmentNotFound(segment_id, workspace_id=workspace_id)
        out = Segment.from_row(row).as_dict()
        ev = self._repo.get_evidence_for_segment(workspace_id, segment_id)
        out["evidence_id"] = ev["id"] if ev else None
        return out

    # -- evidence reads -----------------------------------------------------
    def get_evidence(self, workspace_id: str, evidence_id: str,
                     max_chars: int = 4000) -> Dict[str, Any]:
        """Evidence with its content recovered from the IMMUTABLE snapshot, plus
        an honest report of snapshot integrity and whether the live source still
        matches. Never depends on a running model."""
        self._require_workspace(workspace_id)
        ev = self._repo.get_evidence(workspace_id, evidence_id)
        if not ev:
            raise EvidenceNotFound(evidence_id, workspace_id=workspace_id)
        max_chars = max(1, min(int(max_chars), self.MAX_EVIDENCE_CHARS))

        rev = self._repo.get_revision(workspace_id, ev["revision_id"])
        locator = ev.get("locator") or {}
        start = int(locator.get("startLine", 0) or 0)
        end = int(locator.get("endLine", 0) or 0)
        expected_hash = ev.get("content_hash", "")

        snapshot_status = "missing"
        content = ""
        blob_hash = (rev or {}).get("content_blob_hash", "")
        if rev and blob_hash:
            try:
                blob = self._content.get(workspace_id, blob_hash)
                snap_text = blob.decode("utf-8", "replace")
                recovered = segmentation.slice_lines(snap_text, start, end)
                if segmentation.hash_text_utf8(recovered) == expected_hash:
                    snapshot_status = "available"
                    content = recovered
                else:
                    # blob intact but the cited range no longer hashes as recorded
                    snapshot_status = "corrupt"
            except ContentCorruption:
                snapshot_status = "corrupt"

        current_status = self._current_source_status(
            workspace_id, locator.get("file", ""), start, end, expected_hash)

        truncated = len(content) > max_chars
        return {
            "id": ev["id"],
            "revision_id": ev["revision_id"],
            "segment_id": ev.get("segment_id"),
            "locator": locator,
            "content_hash": expected_hash,
            "excerpt": ev.get("excerpt", ""),
            "content": content[:max_chars],
            "truncated": truncated,
            "snapshot": {"status": snapshot_status},
            "current_source": {"status": current_status},
            "revision": None if not rev else {
                "id": rev["id"], "sequence": rev["sequence"],
                "status": rev["status"], "content_hash": rev["content_hash"],
                "content_blob_hash": blob_hash},
        }

    def _current_source_status(self, workspace_id: str, rel_file: str,
                               start: int, end: int, expected_hash: str) -> str:
        """Whether the live on-disk source still matches the cited range:
        ``matches`` / ``changed`` / ``missing``. Never reads outside the
        workspace source roots."""
        if not rel_file:
            return "missing"
        abspath = machine.from_rel(workspace_id, rel_file)
        # Defense in depth: never read a path that resolves outside the
        # workspace's source root, even if a locator were somehow crafted with a
        # traversal component. Locators are only ever produced from workspace-
        # relative keys of ingested files, so this should always hold — but the
        # guarantee ("no evidence cites a file outside the source roots") is
        # enforced here, not merely assumed.
        root = machine.project_root(workspace_id)
        if root:
            root_n = os.path.normcase(os.path.normpath(root))
            abs_n = os.path.normcase(os.path.normpath(abspath))
            if abs_n != root_n and not abs_n.startswith(root_n + os.sep):
                return "missing"
        if not abspath or not os.path.isfile(abspath):
            return "missing"
        try:
            text = walker.read_text(abspath)
        except Exception:
            return "missing"
        recovered = segmentation.slice_lines(text, start, end)
        return "matches" if segmentation.hash_text_utf8(recovered) == expected_hash \
            else "changed"

    # -- stats --------------------------------------------------------------
    def stats(self, workspace_id: str) -> Dict[str, Any]:
        """Aggregate Asset-model counts for the workspace."""
        self._require_workspace(workspace_id)
        out: Dict[str, Any] = {"workspace_id": workspace_id}
        out.update(self._repo.asset_stats(workspace_id))
        return out

    # -- single-file sync ---------------------------------------------------
    def sync_file(self, workspace_id: str, path: str, wait: bool = False,
                  timeout: float = 3600.0) -> Dict[str, Any]:
        """Ingest ONE existing file that lives under a registered source root.

        A directory is rejected (use ``openmind add`` to register a source path).
        An unsupported format is registered as an ``unsupported`` Asset — recorded
        honestly, never falsely reported as parsed — and NOT ingested. A supported
        file triggers a filtered ingest of just that file (which updates its Asset
        and RAG projection without rebuilding the whole-project artifacts).
        """
        self._require_workspace(workspace_id)
        raw = (path or "").strip()
        if not raw:
            raise InvalidRequest("path must not be empty", details={"field": "path"})
        abspath = walker.norm(os.path.abspath(os.path.expanduser(raw)))
        if os.path.isdir(abspath):
            raise InvalidRequest(
                "asset add takes a single file; use 'openmind add' to register a "
                "directory as a source path",
                details={"path": abspath})
        if not os.path.isfile(abspath):
            raise InvalidRequest(f"file not found: {abspath}",
                                 details={"path": abspath})

        root = machine.project_root(workspace_id)
        logical_key = machine.relativize(abspath, root)
        if not root or logical_key == abspath or machine._is_absolute(logical_key):
            raise InvalidRequest(
                "file is not under a registered workspace source root; add its "
                "directory with 'openmind add' first",
                details={"path": abspath, "source_root": root})

        ext = os.path.splitext(abspath)[1].lower()
        supported = (ext in config.INDEX_EXTENSIONS
                     and ext not in config.BINARY_EXTENSIONS
                     and self._within_size(abspath))
        title = logical_key.rsplit("/", 1)[-1]
        if not supported:
            asset = self._repo.upsert_asset(
                workspace_id, logical_key, asset_type=AssetType.classify(logical_key),
                title=title, source_path=logical_key, state=AssetState.UNSUPPORTED)
            return {
                "workspace_id": workspace_id, "logical_key": logical_key,
                "supported": False, "state": AssetState.UNSUPPORTED,
                "asset_id": asset["id"], "asset": asset, "job_id": None,
            }

        result = self._ingest.start(workspace_id, path=abspath, wait=wait,
                                    timeout=timeout)
        asset = self._repo.find_asset_by_logical_key(workspace_id, logical_key)
        out = {
            "workspace_id": workspace_id, "logical_key": logical_key,
            "supported": True, "job_id": result.get("job_id"),
            "waited": result.get("waited", False),
            "asset_id": asset["id"] if asset else None,
        }
        for k in ("status", "completed", "waited_seconds"):
            if k in result:
                out[k] = result[k]
        if asset:
            out["asset"] = self._asset_with_current(workspace_id, asset)
        return out

    @staticmethod
    def _within_size(abspath: str) -> bool:
        try:
            return os.path.getsize(abspath) <= config.MAX_FILE_BYTES
        except OSError:
            return False


__all__ = ["AssetService"]
