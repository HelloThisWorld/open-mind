"""Document use cases — append, query, outline, search and candidate association.

Exposed as ``runtime.documents`` and ``ServiceContainer.documents``, and shared
by the CLI, the FastAPI adapter and the MCP server, so the same operations are
reachable without constructing an HTTP request.

WORKSPACE SCOPING IS ENFORCED ON EVERY CALL
-------------------------------------------
Every method takes a ``workspace_id`` and validates it first (raising
:class:`WorkspaceNotFound`), then reads through workspace-scoped repository
queries — so a document Asset from workspace A can never be read through
workspace B. The service raises typed domain errors, never ``HTTPException``.

THE ABSOLUTE PATH STOPS HERE
----------------------------
:meth:`add_document` is the ONLY place that touches an absolute machine path. It
reads the bytes, snapshots them into the immutable content store, and enqueues a
job whose payload carries a blob hash and a filename. The path itself never
reaches the portable database, a job row, an Asset, a locator or an export.

WHAT THIS SERVICE DOES NOT DO
-----------------------------
It does not extract Requirements, Business Rules, Design Decisions or Acceptance
Criteria; it does not classify documents semantically; it does not infer
authority; and :meth:`find_related_candidates` returns OBSERVED MENTIONS that are
never persisted as relations. All of that is Phase 4.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from .. import config, content_store, db as db_module, document_rag, machine
from ..domain.errors import (AssetNotFound, InvalidRequest, JobFailed,
                             RevisionNotFound)
from ..domain.types import (Asset, AssetRevision, DocumentParse,
                            DocumentParseStatus, ImportStatus, Segment,
                            SourceKind)


class DocumentService:
    """Append and query enterprise documents in a workspace."""

    #: Hard upper bounds so no adapter can request an unbounded page.
    MAX_DOCUMENT_LIMIT = 500
    MAX_OUTLINE_LIMIT = 2000
    MAX_SEARCH_LIMIT = 50
    MAX_CANDIDATE_LIMIT = 100

    def __init__(self, workspaces: Any, jobs: Any, assets: Any,
                 ensure_worker: Optional[Callable[[], None]] = None,
                 repo: Any = None, content: Any = None) -> None:
        self._workspaces = workspaces
        self._jobs = jobs
        self._assets = assets
        self._ensure_worker = ensure_worker
        self._repo: Any = repo if repo is not None else db_module
        self._content: Any = content if content is not None else content_store

    # -- helpers ------------------------------------------------------------
    def _require_workspace(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.get(workspace_id)

    @staticmethod
    def _bound(limit: Any, hard: int) -> int:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = hard
        return max(1, min(limit, hard))

    def _read_local(self, path: str) -> Dict[str, Any]:
        """Read a local file for import. The absolute path is used HERE and
        nowhere else — nothing derived from it is stored."""
        raw = (path or "").strip()
        if not raw:
            raise InvalidRequest("path must not be empty",
                                 details={"field": "path"})
        abspath = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(abspath):
            raise InvalidRequest(
                "document add takes a single file; use 'openmind add' to "
                "register a directory as a source path",
                details={"path": abspath})
        if not os.path.isfile(abspath):
            raise InvalidRequest(f"file not found: {abspath}",
                                 details={"path": abspath})
        size = os.path.getsize(abspath)
        if size > config.DOCUMENT_MAX_BYTES:
            raise InvalidRequest(
                f"document is {size} bytes, above the "
                f"{config.DOCUMENT_MAX_BYTES}-byte limit "
                f"(OPENMIND_DOCUMENT_MAX_BYTES)",
                details={"size": size, "limit": config.DOCUMENT_MAX_BYTES})
        with open(abspath, "rb") as handle:
            data = handle.read()
        return {"data": data, "filename": os.path.basename(abspath),
                "size": size, "abspath": abspath}

    def _workspace_relative_key(self, workspace_id: str,
                                abspath: str) -> str:
        """The workspace-relative key when the file lives under a registered
        source root, else ``''`` (it is then an attachment)."""
        root = machine.project_root(workspace_id)
        if not root:
            return ""
        normalized = abspath.replace("\\", "/")
        relative = machine.relativize(normalized, root)
        if relative == normalized or machine._is_absolute(relative):
            return ""
        return relative

    # -- planning -----------------------------------------------------------
    def plan_import(self, workspace_id: str, path: str, *,
                    asset_id: str = "", logical_key: str = "",
                    new_asset: bool = False) -> Dict[str, Any]:
        """What importing this file WOULD do. Writes nothing at all.

        Backs ``document add --dry-run``, so a user can see a
        ``possible_revision`` collision before committing to anything.
        """
        from ..documents import intake, probe_bytes, registry

        self._require_workspace(workspace_id)
        read = self._read_local(path)
        content_hash = self._content.hash_bytes(read["data"])
        relative = self._workspace_relative_key(workspace_id, read["abspath"])

        decision = intake.plan(
            workspace_id, filename=read["filename"], content_hash=content_hash,
            requested_asset_id=asset_id, requested_logical_key=logical_key,
            new_asset=new_asset, workspace_relative_key=relative,
            repo=self._repo)

        found = probe_bytes(read["data"], filename=read["filename"])
        parser = None
        try:
            selected = registry.select(found)
            parser = getattr(selected, "name", None)
        except registry.AmbiguousParser:
            parser = None
        decision.update({
            "workspace_id": workspace_id,
            "filename": read["filename"],
            "size": read["size"],
            "source_kind": (SourceKind.FILE if relative
                            else SourceKind.ATTACHMENT),
            "probe": found.as_dict(),
            "parser": parser,
        })
        if parser is None and decision["status"] not in (
                ImportStatus.DUPLICATE, ImportStatus.UNSUPPORTED):
            decision["status"] = ImportStatus.UNSUPPORTED
            decision["reason"] = (
                f"no registered parser claims this file "
                f"({found.detected_media_type or 'unknown type'})")
        return decision

    # -- import -------------------------------------------------------------
    def add_document(self, workspace_id: str, path: str, *, asset_id: str = "",
                     logical_key: str = "", new_asset: bool = False,
                     version_label: str = "", wait: bool = False,
                     timeout: float = 3600.0,
                     dry_run: bool = False) -> Dict[str, Any]:
        """Append a document from anywhere on this machine.

        Stages the bytes as an immutable blob, then enqueues a
        ``document_ingest`` job whose payload names that blob — never the file's
        path. A ``duplicate`` or ``possible_revision`` decision returns WITHOUT
        creating a job, a revision or a vector entry.
        """
        plan = self.plan_import(workspace_id, path, asset_id=asset_id,
                                logical_key=logical_key, new_asset=new_asset)
        plan["dry_run"] = bool(dry_run)
        if dry_run:
            return plan
        if plan["status"] in (ImportStatus.DUPLICATE,
                              ImportStatus.POSSIBLE_REVISION,
                              ImportStatus.UNSUPPORTED):
            # Nothing is written for any of these. `duplicate` already exists,
            # `possible_revision` needs the user's decision, and `unsupported`
            # has nothing to parse.
            plan["job_id"] = None
            return plan

        read = self._read_local(path)
        staged = self._content.put(workspace_id, read["data"])
        payload = {
            "staged_blob_hash": staged,
            "original_filename": read["filename"],
            "requested_asset_id": asset_id or plan.get("asset_id", ""),
            "requested_logical_key": plan["logical_key"],
            "import_mode": plan["status"],
            "source_kind": plan["source_kind"],
            "version_label": version_label,
            "parser_options": {},
        }
        if wait and self._ensure_worker is not None:
            self._ensure_worker()
        job = self._jobs.enqueue_document_ingest(workspace_id, payload)

        result = dict(plan)
        result.update({"job_id": job["job_id"], "job": job, "waited": False,
                       "staged_blob_hash": staged})
        if not wait:
            return result

        outcome = self._jobs.wait_for_terminal(job["job_id"], timeout=timeout)
        result.update({"waited": True, "job": outcome.job,
                       "status_job": outcome.status,
                       "completed": outcome.completed,
                       "waited_seconds": round(outcome.waited_seconds, 3)})
        report = (outcome.job or {}).get("progress", {}).get("import_report")
        if report:
            result["import_report"] = report
        if not outcome.completed:
            result["error"] = {
                "code": "job_failed",
                "message": (f"document import job {job['job_id']} did not "
                            f"complete (status: {outcome.status})"),
            }
        return result

    # -- reads --------------------------------------------------------------
    def list_documents(self, workspace_id: str, *, status: Optional[str] = None,
                       parser: Optional[str] = None, state: str = "active",
                       limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """A bounded page of the workspace's document Assets.

        "Document" means the Asset's CURRENT revision has a parse record — a
        recorded fact, not a guess from the file extension.
        """
        self._require_workspace(workspace_id)
        if status is not None and status not in DocumentParseStatus.VALUES:
            raise InvalidRequest(
                f"unknown parse status: {status!r}",
                details={"status": status,
                         "allowed": sorted(DocumentParseStatus.VALUES)})
        limit = self._bound(limit, self.MAX_DOCUMENT_LIMIT)
        offset = max(0, int(offset or 0))
        rows = self._repo.list_document_assets(
            workspace_id, status=status, parser=parser,
            state=(state or None), limit=limit, offset=offset)
        total = self._repo.count_document_assets(
            workspace_id, status=status, parser=parser, state=(state or None))
        return {"workspace_id": workspace_id, "documents": rows, "total": total,
                "count": len(rows), "limit": limit, "offset": offset,
                "filters": {"status": status, "parser": parser, "state": state}}

    def get_document(self, workspace_id: str, asset_id: str) -> Dict[str, Any]:
        """One document: its Asset, current Revision and parse summary."""
        self._require_workspace(workspace_id)
        asset = self._repo.get_asset(workspace_id, asset_id)
        if not asset:
            raise AssetNotFound(asset_id, workspace_id=workspace_id)
        revision_id = asset.get("current_revision_id") or ""
        parse = (self._repo.get_document_parse(workspace_id, revision_id)
                 if revision_id else None)
        if not parse:
            raise AssetNotFound(
                asset_id, workspace_id=workspace_id)
        revision = self._repo.get_revision(workspace_id, revision_id)
        index = self._repo.get_document_index(workspace_id, asset_id) or {}
        out = Asset.from_row(asset).as_dict()
        out["current_revision"] = (AssetRevision.from_row(revision).as_dict()
                                   if revision else None)
        if out["current_revision"]:
            out["current_revision"]["segment_count"] = \
                self._repo.count_segments(workspace_id, revision_id)
        out["parse"] = DocumentParse.from_row(parse).as_dict()
        out["index"] = {"chunk_count": len(index.get("chunk_ids") or []),
                        "revision_id": index.get("revision_id", ""),
                        "updated_at": index.get("updated_at", "")}
        return out

    def get_outline(self, workspace_id: str, revision_id: str,
                    limit: int = 500) -> Dict[str, Any]:
        """A bounded STRUCTURAL outline of one document revision.

        Deliberately not the content: each entry is a block's type, heading path,
        locator and a short preview, so an agent can navigate to the part it
        wants and then fetch exactly that Evidence.
        """
        self._require_workspace(workspace_id)
        revision = self._repo.get_revision(workspace_id, revision_id)
        if not revision:
            raise RevisionNotFound(revision_id, workspace_id=workspace_id)
        parse = self._repo.get_document_parse(workspace_id, revision_id)
        if not parse:
            raise RevisionNotFound(revision_id, workspace_id=workspace_id)
        limit = self._bound(limit, self.MAX_OUTLINE_LIMIT)
        segments = self._repo.list_segments(workspace_id, revision_id,
                                            limit=limit)
        total = self._repo.count_segments(workspace_id, revision_id)
        evidence = self._repo.evidence_ids_for_revision(workspace_id,
                                                        revision_id)
        entries: List[Dict[str, Any]] = []
        for row in segments:
            record = Segment.from_row(row)
            metadata = record.metadata or {}
            entries.append({
                "segment_id": record.id,
                "evidence_id": evidence.get(record.id, ""),
                "block_key": record.segment_key,
                "block_type": record.segment_type,
                "ordinal": record.ordinal,
                "parent_key": metadata.get("parent_key", ""),
                "heading_path": metadata.get("heading_path", []),
                "level": metadata.get("level"),
                "content_mode": record.content_mode,
                "indexable": bool(metadata.get("indexable", True)),
                "symbol": record.symbol,
                "preview": self._preview(workspace_id, row),
            })
        return {
            "workspace_id": workspace_id, "revision_id": revision_id,
            "asset_id": revision.get("asset_id", ""),
            "parse": {"parser_name": parse["parser_name"],
                      "parser_version": parse["parser_version"],
                      "status": parse["status"], "title": parse["title"],
                      "coverage": parse["coverage"]},
            "outline": entries, "total": total, "count": len(entries),
            "limit": limit,
        }

    #: An outline preview is a NAVIGATION aid, not content. Kept short so a
    #: 2000-entry outline cannot become a full document dump.
    PREVIEW_CHARS = 120

    def _preview(self, workspace_id: str, segment_row: Dict[str, Any]) -> str:
        record = self._repo.get_evidence_for_segment(workspace_id,
                                                     segment_row["id"])
        text = (record or {}).get("excerpt", "")
        return " ".join(text.split())[:self.PREVIEW_CHARS]

    # -- retrieval ----------------------------------------------------------
    def search(self, workspace_id: str, query: str, limit: int = 20, *,
               asset_type: Optional[str] = None, parser: Optional[str] = None,
               block_type: Optional[str] = None,
               logical_key: Optional[str] = None,
               include_removed: bool = False) -> Dict[str, Any]:
        """Document-only retrieval. Bounded, evidence-cited, exact-ID-preserving."""
        self._require_workspace(workspace_id)
        if not (query or "").strip():
            raise InvalidRequest("query must not be empty",
                                 details={"field": "query"})
        return document_rag.search(
            workspace_id, query, limit=self._bound(limit, self.MAX_SEARCH_LIMIT),
            asset_type=asset_type, parser=parser, block_type=block_type,
            logical_key=logical_key, include_removed=include_removed)

    def search_knowledge(self, workspace_id: str, query: str, *,
                         code_limit: int = 12,
                         document_limit: int = 12) -> Dict[str, Any]:
        """Code and document candidates, returned separately. Never asserts a
        relationship between the two sides."""
        self._require_workspace(workspace_id)
        if not (query or "").strip():
            raise InvalidRequest("query must not be empty",
                                 details={"field": "query"})
        return document_rag.search_knowledge(
            [workspace_id], query,
            code_limit=self._bound(code_limit, self.MAX_SEARCH_LIMIT),
            document_limit=self._bound(document_limit, self.MAX_SEARCH_LIMIT))

    def find_related_candidates(self, workspace_id: str, asset_id: str,
                                limit: int = 30) -> Dict[str, Any]:
        """Deterministic candidate associations for one document.

        Every result is an OBSERVED MENTION labelled ``status: "candidate"``.
        Nothing is persisted, and no result claims the document implements,
        refines, verifies or contradicts its target.
        """
        from ..documents import candidates

        self._require_workspace(workspace_id)
        if not self._repo.get_asset(workspace_id, asset_id):
            raise AssetNotFound(asset_id, workspace_id=workspace_id)
        return candidates.find_candidates(
            workspace_id, asset_id,
            limit=self._bound(limit, self.MAX_CANDIDATE_LIMIT),
            repo=self._repo)

    # -- stats --------------------------------------------------------------
    def stats(self, workspace_id: str) -> Dict[str, Any]:
        self._require_workspace(workspace_id)
        out: Dict[str, Any] = {"workspace_id": workspace_id}
        out.update(self._repo.document_stats(workspace_id))
        return out


__all__ = ["DocumentService"]
