"""The normalized document model every parser produces.

ONE SHAPE, MANY FORMATS
-----------------------
A Markdown heading, a DOCX table row, a PDF page block, an XLSX cell range and
an OpenAPI operation are wildly different things in their source formats. They
become the same thing here — a :class:`DocumentBlock` with a stable key, an
ordinal, a parent, a heading path, text, a content mode and a portable locator —
so exactly one persistence path, one vector projection and one evidence resolver
serve every format. A parser uses only the block types its format actually has;
nothing is invented to fill the vocabulary out.

HONESTY IS STRUCTURAL, NOT EDITORIAL
------------------------------------
Three fields exist purely so the model cannot overstate what was extracted:

``status``
    ``partial`` when a limit truncated the parse, ``needs-ocr`` when a PDF has no
    extractable text, ``encrypted``, ``unsupported`` and ``failed`` — never a
    silent empty success.
``warnings`` / ``unsupported_content``
    What was dropped and why, with counts. An embedded image or a macro is
    RECORDED as unsupported content rather than ignored.
``content_mode``
    ``verbatim`` only when ``text`` is an exact textual representation of the
    source. A synthesized table rendering or a serialized spreadsheet row is
    ``derived`` — misreporting either as verbatim would make an Evidence
    citation claim the document literally said something it did not.

DETERMINISM
-----------
Parsing the same bytes twice must produce byte-identical output, because a
Revision's ``structure_hash`` is computed over it and a re-parse that silently
differs would look like a document change. Block keys are derived from position
and structure (never from a counter that depends on iteration order of a set,
and never from a timestamp or a random id).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..domain.types import (ContentMode, DocumentBlockType, DocumentParseStatus)

#: Version of THIS normalized shape. Bumped when the block/document contract
#: changes, independently of any parser's own ``version``.
DOCUMENT_SCHEMA_VERSION = "1.0"


@dataclass
class DocumentProbe:
    """What is known about a candidate document BEFORE any parser runs.

    Deliberately richer than a filename: selecting a parser from the extension
    alone is how a renamed archive gets handed to a parser that will happily read
    it. ``magic`` carries signature facts (``zip``, ``pdf``, ``ole2``, ``bom``,
    ``utf8``) and, for ZIP containers, ``zip_members`` — the member names that
    identify an OOXML package (``word/document.xml`` for DOCX,
    ``xl/workbook.xml`` for XLSX). A parser for a ZIP-based format must check
    package structure, not the suffix.
    """
    filename: str = ""
    extension: str = ""
    declared_media_type: str = ""
    detected_media_type: str = ""
    size: int = 0
    magic: Dict[str, Any] = field(default_factory=dict)
    #: A bounded prefix of the bytes, for text sniffing. Never the whole file.
    head: bytes = b""

    @property
    def is_zip_package(self) -> bool:
        return bool(self.magic.get("zip"))

    def has_zip_member(self, name: str) -> bool:
        return name in set(self.magic.get("zip_members") or ())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename, "extension": self.extension,
            "declared_media_type": self.declared_media_type,
            "detected_media_type": self.detected_media_type,
            "size": self.size,
            # zip_members can be long; report the count plus the identifying
            # names a reader actually needs.
            "magic": {k: v for k, v in self.magic.items() if k != "zip_members"},
            "zip_member_count": len(self.magic.get("zip_members") or ()),
        }


@dataclass
class DocumentLimits:
    """The resource envelope one parse runs inside.

    Defaults come from :mod:`openmind.config` (each overridable by an
    ``OPENMIND_*`` environment variable); a caller may tighten them per parse.
    Hitting any of these produces ``partial`` + a warning, never a silent cut.
    """
    max_bytes: int = 25_000_000
    max_blocks: int = 20_000
    max_block_chars: int = 20_000
    pdf_max_pages: int = 2_000
    docx_max_paragraphs: int = 50_000
    docx_max_tables: int = 2_000
    xlsx_max_sheets: int = 100
    xlsx_max_rows_per_sheet: int = 20_000
    xlsx_max_cells: int = 500_000
    csv_max_rows: int = 50_000
    zip_max_members: int = 2_000
    zip_max_total_bytes: int = 400_000_000
    zip_max_member_bytes: int = 100_000_000
    zip_max_ratio: int = 200

    @classmethod
    def from_config(cls) -> "DocumentLimits":
        from .. import config
        return cls(
            max_bytes=config.DOCUMENT_MAX_BYTES,
            max_blocks=config.DOCUMENT_MAX_BLOCKS,
            max_block_chars=config.DOCUMENT_MAX_BLOCK_CHARS,
            pdf_max_pages=config.PDF_MAX_PAGES,
            docx_max_paragraphs=config.DOCX_MAX_PARAGRAPHS,
            docx_max_tables=config.DOCX_MAX_TABLES,
            xlsx_max_sheets=config.XLSX_MAX_SHEETS,
            xlsx_max_rows_per_sheet=config.XLSX_MAX_ROWS_PER_SHEET,
            xlsx_max_cells=config.XLSX_MAX_CELLS,
            csv_max_rows=config.CSV_MAX_ROWS,
            zip_max_members=config.ZIP_MAX_MEMBERS,
            zip_max_total_bytes=config.ZIP_MAX_TOTAL_BYTES,
            zip_max_member_bytes=config.ZIP_MAX_MEMBER_BYTES,
            zip_max_ratio=config.ZIP_MAX_RATIO,
        )


@dataclass
class DocumentParseContext:
    """Everything a parser needs besides the bytes.

    ``logical_key`` is the PORTABLE document key that goes into every locator —
    never an absolute path. ``filename`` is the original name, used for titles
    and diagnostics only.
    """
    logical_key: str
    filename: str = ""
    workspace_id: str = ""
    declared_media_type: str = ""
    limits: DocumentLimits = field(default_factory=DocumentLimits)
    parser_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentParseWarning:
    """One thing the parser could not do fully, named precisely enough to act on.

    ``code`` is machine-readable and stable (``limit-exceeded``,
    ``decode-fallback``, ``dialect-uncertain``, ``unsupported-syntax``, ...);
    ``detail`` carries the numbers (which limit, observed vs allowed).
    """
    code: str
    message: str
    locator: Optional[Dict[str, Any]] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.locator:
            out["locator"] = dict(self.locator)
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class UnsupportedContent:
    """Content that exists in the source and was deliberately NOT extracted —
    an embedded image, a macro, a chart. Recorded with a count so a reader can
    tell "this document has 12 images we did not read" from "this document has
    no images"."""
    kind: str
    count: int = 1
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "count": self.count}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class DocumentMetadata:
    """Deterministic document properties, read from the file's own metadata.

    ``version_label`` is populated ONLY from an explicit, documented field
    (OpenAPI ``info.version``, the DOCX core ``revision`` property). It is never
    inferred from prose or a filename pattern.
    """
    author: str = ""
    created: str = ""
    modified: str = ""
    producer: str = ""
    language: str = ""
    version_label: str = ""
    page_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "author": self.author, "created": self.created,
            "modified": self.modified, "producer": self.producer,
            "language": self.language, "version_label": self.version_label,
            "page_count": self.page_count,
        }
        # Drop empties so a metadata-less document does not read as a document
        # whose author is the empty string.
        out = {k: v for k, v in out.items() if v not in ("", 0)}
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


@dataclass
class DocumentBlock:
    """One structural unit of a parsed document."""
    block_key: str
    block_type: str
    ordinal: int
    text: str = ""
    parent_key: str = ""
    heading_path: List[str] = field(default_factory=list)
    content_mode: str = ContentMode.VERBATIM
    locator: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    indexable: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "block_key": self.block_key, "block_type": self.block_type,
            "ordinal": self.ordinal, "parent_key": self.parent_key,
            "heading_path": list(self.heading_path), "text": self.text,
            "content_mode": self.content_mode, "locator": dict(self.locator),
            "metadata": dict(self.metadata), "indexable": self.indexable,
        }

    @property
    def content_hash(self) -> str:
        """SHA-256 of the exact represented text, in the same UTF-8 encoding the
        block's content blob is stored in — so the stored Segment/Evidence hash
        is recomputable from that blob."""
        return hashlib.sha256(self.text.encode("utf-8", "replace")).hexdigest()


@dataclass
class ParsedDocument:
    """The complete, normalized result of parsing one document's bytes."""
    parser_name: str
    parser_version: str
    status: str = DocumentParseStatus.PARSED
    schema_version: str = DOCUMENT_SCHEMA_VERSION
    title: str = ""
    media_type: str = ""
    metadata: DocumentMetadata = field(default_factory=DocumentMetadata)
    blocks: List[DocumentBlock] = field(default_factory=list)
    warnings: List[DocumentParseWarning] = field(default_factory=list)
    unsupported_content: List[UnsupportedContent] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    #: Populated for a non-usable status: ``dependency_unavailable``,
    #: ``unsupported``, ``image-only``, ``encrypted``, the exception type...
    reason: str = ""

    # -- predicates ---------------------------------------------------------
    @property
    def usable(self) -> bool:
        """Whether this parse produced content worth committing as a Revision."""
        return self.status in DocumentParseStatus.USABLE

    @property
    def indexable_blocks(self) -> List[DocumentBlock]:
        return [b for b in self.blocks if b.indexable and b.text.strip()]

    # -- derived ------------------------------------------------------------
    def structure_hash(self) -> str:
        """A deterministic fingerprint of the parse's STRUCTURE.

        Covers each block's key, type, ordinal, parent and content hash, so
        re-parsing the same bytes with the same parser yields the same value and
        a structural change is detectable — without storing a second copy of the
        document.
        """
        h = hashlib.sha256()
        h.update(f"{self.parser_name}\x1f{self.parser_version}\x1f"
                 f"{self.schema_version}\x1f{self.status}\x1e".encode("utf-8"))
        for b in self.blocks:
            h.update(f"{b.block_key}\x1f{b.block_type}\x1f{b.ordinal}\x1f"
                     f"{b.parent_key}\x1f{b.content_hash}\x1e".encode("utf-8"))
        return h.hexdigest()

    def add_warning(self, code: str, message: str, **detail: Any) -> None:
        self.warnings.append(DocumentParseWarning(code=code, message=message,
                                                  detail=dict(detail)))

    def note_unsupported(self, kind: str, count: int = 1,
                         detail: str = "") -> None:
        """Record (or increment) a class of content that was not extracted."""
        for entry in self.unsupported_content:
            if entry.kind == kind:
                entry.count += count
                return
        self.unsupported_content.append(
            UnsupportedContent(kind=kind, count=count, detail=detail))

    def mark_partial(self, code: str, message: str, **detail: Any) -> None:
        """Record a truncation AND downgrade the status.

        The pairing is the point: a limit must never be recorded as a warning on
        a document still reported as fully ``parsed``.
        """
        self.add_warning(code, message, **detail)
        if self.status == DocumentParseStatus.PARSED:
            self.status = DocumentParseStatus.PARTIAL

    def finalize(self) -> "ParsedDocument":
        """Compute coverage and normalize ordinals. Called once by the registry
        after a parser returns, so no parser has to remember to."""
        for i, block in enumerate(self.blocks):
            block.ordinal = i
        indexable = len(self.indexable_blocks)
        self.coverage = {
            "blocks": len(self.blocks),
            "indexable": indexable,
            "characters": sum(len(b.text) for b in self.blocks),
            "truncated": self.status == DocumentParseStatus.PARTIAL,
            "warnings": len(self.warnings),
            "unsupported": sum(u.count for u in self.unsupported_content),
            **self.coverage,
        }
        return self

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "status": self.status,
            "title": self.title,
            "media_type": self.media_type,
            "metadata": self.metadata.as_dict(),
            "blocks": [b.as_dict() for b in self.blocks],
            "warnings": [w.as_dict() for w in self.warnings],
            "unsupported_content": [u.as_dict() for u in self.unsupported_content],
            "coverage": dict(self.coverage),
        }
        if self.reason:
            out["reason"] = self.reason
        return out


# ---------------------------------------------------------------------------
# Non-usable results
#
# Each returns a REAL ParsedDocument with an explicit status, never an empty
# successful parse — "we could not read this" and "this document is empty" must
# stay distinguishable at every layer above.
# ---------------------------------------------------------------------------
def unsupported(filename: str, reason: str, *, parser_name: str = "none",
                media_type: str = "") -> ParsedDocument:
    return ParsedDocument(
        parser_name=parser_name, parser_version="0",
        status=DocumentParseStatus.UNSUPPORTED,
        title=filename, media_type=media_type, reason=reason).finalize()


def dependency_unavailable(filename: str, parser_name: str,
                           distribution: str) -> ParsedDocument:
    doc = ParsedDocument(
        parser_name=parser_name, parser_version="0",
        status=DocumentParseStatus.UNSUPPORTED, title=filename,
        reason="dependency_unavailable")
    doc.add_warning(
        "dependency-unavailable",
        f"the {parser_name!r} parser needs the {distribution!r} package, which "
        f"is not installed; install it to ingest this format",
        distribution=distribution)
    return doc.finalize()


def failed(filename: str, parser_name: str, parser_version: str,
           exc: BaseException) -> ParsedDocument:
    doc = ParsedDocument(
        parser_name=parser_name, parser_version=parser_version,
        status=DocumentParseStatus.FAILED, title=filename,
        reason=type(exc).__name__)
    doc.add_warning("parse-failed", f"{type(exc).__name__}: {exc}")
    return doc.finalize()


__all__ = [
    "DOCUMENT_SCHEMA_VERSION", "DocumentProbe", "DocumentLimits",
    "DocumentParseContext", "DocumentParseWarning", "UnsupportedContent",
    "DocumentMetadata", "DocumentBlock", "ParsedDocument",
    "DocumentBlockType", "DocumentParseStatus",
    "unsupported", "dependency_unavailable", "failed",
]
