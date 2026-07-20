"""Parsed document → Asset / Revision / Segments / Evidence / vectors.

THE ORDERING IS THE DESIGN
--------------------------
A current document Revision must NEVER point at partial Segments or Evidence, and
a failed import must leave no half-written state. So:

1. Read the immutable staged blob (the bytes are already snapshotted).
2. Select a parser and parse into memory.
3. Store each block's exact text as its own content-addressed blob.
4. Pre-mint the asset / revision / segment / evidence ids.
5. Build the document vector chunks (their metadata carries those ids).
6. Upsert the chunks — idempotent, because a chunk id is derived from the
   CONTENT, not from the run.
7. Commit Asset + Revision + Segments + Evidence + ``document_parses`` +
   ``document_index`` in ONE transaction.
8. Remove the PREVIOUS revision's chunks, now that the new ones are current.

Steps 6 and 7 are ordered vector-first deliberately. If the transaction fails
after the upsert, the failure is honest, no partial current Revision exists, and
a retry produces the SAME chunk ids and simply overwrites them — so the only
residue is a set of unreferenced chunks, which ``document_index`` makes
reconcilable. The reverse order would leave a current Revision whose chunks were
never written, which no reader could detect.

Ids are pre-minted (step 4) precisely so step 6 can happen before step 7 while
still writing ``document_index`` inside the transaction.

Step 8 is last for the same reason: deleting the old chunks before the new
revision is current would empty a document out of search during the window, and
if the commit then failed the document would have vanished from retrieval
entirely.

CONTENT MODE AND EVIDENCE
-------------------------
Every stored block becomes a Segment carrying ``content_blob_hash`` (the exact
represented text) plus one Evidence row citing its portable locator. That is what
lets historical document Evidence be recovered WITHOUT rerunning a parser — see
``docs/v2/phase-3-document-ingestion.md`` §7.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from .. import content_store, db as db_module
from ..domain.types import (AssetState, AssetType, DocumentParseStatus,
                            RevisionStatus, SourceKind)
from .models import DocumentBlock, ParsedDocument

#: The stored Evidence excerpt is a bounded preview; the authoritative full text
#: is recovered from the block blob at read time. Matches the code path's
#: ``segmentation.EXCERPT_STORE_MAX`` so both kinds of Evidence bound the same way.
EXCERPT_STORE_MAX = 1200

#: Chunk metadata values Chroma must be able to store as scalars.
_HEADING_SEPARATOR = " > "


def chunk_id(logical_key: str, content_hash: str, block_key: str) -> str:
    """A stable, CONTENT-derived chunk id.

    Derived from the document's identity plus the revision's content hash, never
    from the run: a retry after a failed commit produces exactly the same ids and
    simply overwrites them, which is what makes the vector upsert idempotent.

    Two different revisions of one document get different ids (their content
    hashes differ), so the predecessor's chunks stay individually removable. A
    revert back to earlier content deliberately reuses that content's ids — the
    same bytes are the same chunks.
    """
    raw = f"{logical_key}|{content_hash}|{block_key}"
    return "d_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]


def build_segments(workspace_id: str, parsed: ParsedDocument,
                   logical_key: str,
                   content: Any = None) -> List[Dict[str, Any]]:
    """Segment drafts (each with an Evidence draft) for every parsed block.

    Also WRITES each block's exact text into the content store, because the
    segment's ``content_blob_hash`` has to name a blob that already exists by the
    time the revision is committed — that blob is what makes historical document
    Evidence recoverable without rerunning a parser.

    Segment and Evidence ids are pre-minted here so the vector chunks can carry
    them before the transaction runs.
    """
    store = content if content is not None else content_store
    drafts: List[Dict[str, Any]] = []
    for block in parsed.blocks:
        text = block.text or ""
        data = text.encode("utf-8", "replace")
        blob_hash = store.put(workspace_id, data)
        content_hash = block.content_hash
        drafts.append({
            "id": db_module.new_id("s_"),
            "segment_key": block.block_key,
            "segment_type": block.block_type,
            "ordinal": block.ordinal,
            # Document blocks have no source LINE range of their own: their
            # position is expressed by the locator, which is format-specific.
            # A text-range locator does carry lines, so they are mirrored here
            # for the callers that already know how to read those columns.
            "start_line": _int_or_none(block.locator.get("startLine")),
            "end_line": _int_or_none(block.locator.get("endLine")),
            "symbol": _symbol_of(block),
            "content_hash": content_hash,
            "content_mode": block.content_mode,
            "content_blob_hash": blob_hash,
            "metadata": _segment_metadata(block),
            "evidence": {
                "id": db_module.new_id("e_"),
                "locator": dict(block.locator),
                "excerpt": text[:EXCERPT_STORE_MAX],
                "content_hash": content_hash,
            },
        })
    return drafts


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _symbol_of(block: DocumentBlock) -> str:
    """A short human handle for the block, used in listings and citations.

    The innermost heading is the most useful one — it is what a reader would say
    the block is "under" — falling back to the block's own key.
    """
    if block.heading_path:
        return block.heading_path[-1][:200]
    return block.block_key[:200]


def _segment_metadata(block: DocumentBlock) -> Dict[str, Any]:
    meta = dict(block.metadata)
    meta["indexable"] = block.indexable
    if block.parent_key:
        meta["parent_key"] = block.parent_key
    if block.heading_path:
        meta["heading_path"] = list(block.heading_path)
    return meta


def build_chunks(workspace_id: str, asset_id: str, revision_id: str,
                 content_hash: str, parsed: ParsedDocument, logical_key: str,
                 asset_type: str,
                 segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Vector chunks for the indexable blocks only.

    Structural containers (the document root, an empty section, a table wrapper
    whose rows carry the content) are stored as Segments but not embedded —
    indexing them would return the same text twice for one query and dilute
    retrieval with scaffolding.

    The embedded document text is prefixed with a small structural header, the
    same idea the code chunker uses: it puts the document title and heading path
    into the embedding, so a query about a section finds its paragraphs.
    """
    chunks: List[Dict[str, Any]] = []
    title = parsed.title or logical_key
    by_key = {s["segment_key"]: s for s in segments}
    for block in parsed.blocks:
        if not block.indexable or not block.text.strip():
            continue
        draft = by_key.get(block.block_key)
        if draft is None:
            # A block with no segment draft cannot be cited, and an uncitable
            # chunk is worse than a missing one.
            continue
        heading_path = _HEADING_SEPARATOR.join(block.heading_path)
        header = (f"// document: {logical_key}\n"
                  f"// title: {title}\n"
                  f"// section: {heading_path or '(top level)'}\n"
                  f"// block: {block.block_type}\n")
        locator = block.locator or {}
        chunks.append({
            "id": chunk_id(logical_key, content_hash, block.block_key),
            "document": header + "\n" + block.text,
            "metadata": {
                "workspace_id": workspace_id,
                "asset_id": asset_id,
                "revision_id": revision_id,
                "segment_id": draft["id"],
                "evidence_id": (draft.get("evidence") or {}).get("id", ""),
                "logical_key": logical_key,
                "title": title,
                "asset_type": asset_type,
                "block_type": block.block_type,
                "block_key": block.block_key,
                "heading_path": heading_path,
                "parser_name": parsed.parser_name,
                "content_hash": block.content_hash,
                "content_mode": block.content_mode,
                # Only the locator fields that are meaningful for this format
                # get a value; the rest stay empty rather than zero, so a filter
                # on `page` cannot accidentally match a spreadsheet row.
                "page": _int_or_none(locator.get("page")) or 0,
                "sheet": str(locator.get("sheet") or ""),
                "json_pointer": str(locator.get("pointer") or ""),
                "locator_kind": str(locator.get("kind") or ""),
                "start_line": _int_or_none(locator.get("startLine")) or 0,
                "end_line": _int_or_none(locator.get("endLine")) or 0,
            },
        })
    return chunks


def document_parse_record(parsed: ParsedDocument) -> Dict[str, Any]:
    """The ``document_parses`` row for this parse."""
    return {
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "schema_version": parsed.schema_version,
        "status": parsed.status,
        "title": parsed.title,
        "media_type": parsed.media_type,
        "metadata": parsed.metadata.as_dict(),
        "warnings": [w.as_dict() for w in parsed.warnings],
        "unsupported_content": [u.as_dict() for u in parsed.unsupported_content],
        "coverage": dict(parsed.coverage),
        "structure_hash": parsed.structure_hash(),
    }


def revision_metadata(parsed: ParsedDocument,
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "parser": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "parse_status": parsed.status,
    }
    if parsed.warnings:
        meta["parse_warnings"] = len(parsed.warnings)
    meta.update(extra or {})
    return meta


def resolve_version_label(parsed: ParsedDocument,
                          requested: str = "") -> Tuple[str, str]:
    """``(label, source)``.

    An explicit caller-supplied label always wins. Otherwise ONLY a documented
    metadata field is used (OpenAPI ``info.version``, the DOCX core ``revision``
    property, a named PDF version field). Nothing is ever inferred from prose or
    from a filename pattern — that is Phase 4 territory and guessing it here
    would put an unverifiable claim into immutable history.
    """
    if (requested or "").strip():
        return requested.strip(), "explicit"
    label = (parsed.metadata.version_label or "").strip()
    return (label, "document-metadata") if label else ("", "")


def commit_document(workspace_id: str, logical_key: str, *, parsed: ParsedDocument,
                    content: bytes, blob_hash: str, title: str,
                    asset_type: str = "", source_kind: str = SourceKind.FILE,
                    source_path: str = "", version_label: str = "",
                    asset_metadata: Optional[Dict[str, Any]] = None,
                    source_commit: str = "", repo: Any = None,
                    store: Any = None,
                    embed: Any = None) -> Dict[str, Any]:
    """Persist one parsed document and project it into the vector store.

    Returns a report: ``{asset_id, revision, revision_created, asset_created,
    reactivated, segments_created, evidence_created, blocks_indexed,
    chunk_ids, replaced_chunk_ids}``.

    A parse whose status is not usable (``unsupported`` / ``failed`` /
    ``needs-ocr`` / ``encrypted``) is NOT committed as a content revision here —
    the caller decides how to record it, because "we could not read this" is a
    different fact from "this document is empty".
    """
    repository = repo if repo is not None else db_module
    if not parsed.usable:
        raise ValueError(
            f"refusing to commit a document whose parse status is "
            f"{parsed.status!r}; only {sorted(DocumentParseStatus.USABLE)} "
            f"produce a content revision")

    resolved_type = asset_type or AssetType.classify(logical_key)
    if resolved_type == AssetType.UNKNOWN:
        resolved_type = AssetType.DOCUMENT

    # An existing Asset keeps its id; a new one gets a pre-minted id so the
    # chunk metadata can name it before the transaction runs.
    existing = repository.find_asset_by_logical_key(workspace_id, logical_key)
    asset_id = existing["id"] if existing else db_module.new_id("a_")
    current_revision_id = (existing or {}).get("current_revision_id") or ""
    unchanged = bool(existing and current_revision_id
                     and _current_content_hash(repository, workspace_id,
                                               existing) == blob_hash)

    segments = build_segments(workspace_id, parsed, logical_key)
    label, label_source = resolve_version_label(parsed, version_label)
    parse_record = document_parse_record(parsed)

    report: Dict[str, Any] = {
        "version_label": label, "version_label_source": label_source,
        "parse_status": parsed.status, "blocks": len(parsed.blocks),
        "blocks_indexed": 0, "chunk_ids": [], "replaced_chunk_ids": [],
    }

    if unchanged:
        # Identical content: commit_revision creates no new revision. It still
        # runs, because it owns reactivating an Asset that was removed and has
        # reappeared.
        result = repository.commit_revision(
            workspace_id, logical_key, asset_type=resolved_type, title=title,
            source_path=source_path, content_hash=blob_hash,
            content_size=len(content), content_blob_hash=blob_hash,
            segments=[], media_type=parsed.media_type, source_kind=source_kind,
            source_commit=source_commit, asset_state=AssetState.ACTIVE)
        report.update(result)
        # A REMOVED document's chunks were deleted and its index row dropped, so
        # "unchanged" is not the same as "already indexed". Without this, a
        # reappeared document would be active in the database and invisible to
        # search — the worst kind of failure, because nothing looks wrong.
        index = repository.get_document_index(workspace_id, asset_id) or {}
        if index.get("revision_id") != current_revision_id:
            report.update(_reindex_current(
                repository, workspace_id, asset_id, current_revision_id,
                blob_hash, parsed, logical_key, resolved_type, store, embed))
        return report

    revision_id = db_module.new_id("r_")
    chunks = build_chunks(workspace_id, asset_id, revision_id, blob_hash,
                          parsed, logical_key, resolved_type, segments)
    prior = repository.get_document_index(workspace_id, asset_id) or {}
    prior_ids = list(prior.get("chunk_ids") or [])

    # Vector upsert BEFORE the transaction: a failure here leaves nothing
    # committed, and a failure AFTER it leaves only unreferenced chunks that the
    # same ids will overwrite on the next attempt.
    if store is not None and chunks:
        embed_fn = embed if embed is not None else _default_embed
        documents = [c["document"] for c in chunks]
        store.upsert(ids=[c["id"] for c in chunks], embeddings=embed_fn(documents),
                     documents=documents,
                     metadatas=[c["metadata"] for c in chunks])

    result = repository.commit_revision(
        workspace_id, logical_key, asset_type=resolved_type, title=title,
        source_path=source_path, content_hash=blob_hash,
        content_size=len(content), content_blob_hash=blob_hash,
        segments=segments, media_type=parsed.media_type,
        source_kind=source_kind, source_commit=source_commit,
        revision_status=RevisionStatus.UNKNOWN, version_label=label,
        revision_metadata=revision_metadata(
            parsed, {"version_label_source": label_source} if label else None),
        asset_metadata=asset_metadata, asset_state=AssetState.ACTIVE,
        document_parse=parse_record,
        document_chunk_ids=[c["id"] for c in chunks],
        asset_id=asset_id, revision_id=revision_id)
    report.update(result)

    # Only NOW is the predecessor's projection removed: doing it earlier would
    # empty the document out of search during the window, and a failed commit
    # would have removed it permanently.
    live = {c["id"] for c in chunks}
    stale = [i for i in prior_ids if i not in live]
    if store is not None and stale:
        store.delete(ids=stale)

    report["blocks_indexed"] = len(chunks)
    report["chunk_ids"] = [c["id"] for c in chunks]
    report["replaced_chunk_ids"] = stale
    return report


def _current_content_hash(repo: Any, workspace_id: str,
                          asset: Dict[str, Any]) -> str:
    revision = repo.get_revision(workspace_id, asset.get("current_revision_id"))
    return (revision or {}).get("content_hash", "")


def _reindex_current(repo: Any, workspace_id: str, asset_id: str,
                     revision_id: str, content_hash: str,
                     parsed: ParsedDocument, logical_key: str, asset_type: str,
                     store: Any, embed: Any) -> Dict[str, Any]:
    """Rebuild the vector projection for an EXISTING revision.

    Used when the content is unchanged but the projection is not there —
    a document that was removed and reappeared, or a crash between the vector
    upsert and the index write. The segment and evidence ids come from the
    database (this revision was committed long ago), not from fresh drafts, so
    every chunk still cites the Evidence that already exists.
    """
    stored = repo.list_segments(workspace_id, revision_id, limit=5000)
    evidence = repo.evidence_ids_for_revision(workspace_id, revision_id)
    drafts = [{"segment_key": row["segment_key"], "id": row["id"],
               "evidence": {"id": evidence.get(row["id"], "")}}
              for row in stored]
    chunks = build_chunks(workspace_id, asset_id, revision_id, content_hash,
                          parsed, logical_key, asset_type, drafts)
    if store is not None and chunks:
        embed_fn = embed if embed is not None else _default_embed
        documents = [c["document"] for c in chunks]
        store.upsert(ids=[c["id"] for c in chunks], embeddings=embed_fn(documents),
                     documents=documents,
                     metadatas=[c["metadata"] for c in chunks])
    repo.upsert_document_index(workspace_id, asset_id, revision_id,
                               [c["id"] for c in chunks])
    return {"blocks_indexed": len(chunks),
            "chunk_ids": [c["id"] for c in chunks], "reindexed": True}


def _default_embed(documents: List[str]) -> Any:
    from .. import embeddings
    return embeddings.embed(documents)


__all__ = [
    "EXCERPT_STORE_MAX", "chunk_id", "build_segments", "build_chunks",
    "document_parse_record", "revision_metadata", "resolve_version_label",
    "commit_document",
]
