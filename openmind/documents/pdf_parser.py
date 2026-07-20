"""Text-based PDF extraction via ``pypdf``.

LICENSE CHOICE, STATED EXPLICITLY
---------------------------------
``pypdf`` is BSD-3-Clause. The obvious alternative, PyMuPDF, extracts text better
but is **AGPL-3.0**, which would impose copyleft obligations on this MIT project
and on anyone embedding it. That is not a trade this phase makes, so PyMuPDF is
deliberately not used. The cost is real — pypdf recovers less layout — and it is
paid honestly: layout-dependent structure is simply not claimed.

WHAT IS AND IS NOT CLAIMED
--------------------------
* Page text and 1-based page locators: yes.
* Paragraph splitting inside a page: only where blank lines make it
  unambiguous. Column and reading-order reconstruction is NOT attempted.
* Tables: **never** claimed as structured tables. pypdf cannot recover cell
  boundaries, so a table becomes page text, which is what it honestly is.
* OCR: **never performed**, and never claimed. A page with no extractable text
  contributes no blocks — no fabricated content is emitted to fill the gap.

STATUS RULES
------------
``encrypted``  the file is encrypted and does not open with the empty password.
``needs-ocr``  the PDF opened and has pages, but no page yielded meaningful
               text — i.e. it is image-only. Detection is required in this
               phase; OCR itself is Phase 4+.
``partial``    a page limit was hit, or some pages failed to extract.
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List

from ..domain.types import DocumentBlockType, DocumentParseStatus
from .builder import DocumentBuilder
from .models import (DocumentParseContext, DocumentProbe, ParsedDocument,
                     dependency_unavailable)

#: Below this many non-whitespace characters across the whole document, a PDF
#: that has pages is treated as image-only. A handful of stray glyphs from a
#: scanner's header is not extractable text.
_MIN_MEANINGFUL_CHARS = 16

#: Two or more newlines separate paragraphs; a single newline inside a paragraph
#: is a line wrap, not a break.
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    """Claimed on the ``%PDF-`` signature, not the extension."""
    return bool(probe.magic.get("pdf"))


class PdfParser:
    """Page text, page locators and metadata from a text-based PDF."""

    name = "pdf"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type="application/pdf",
                             title=context.filename or context.logical_key)
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ImportError:
            return dependency_unavailable(context.filename, self.name, "pypdf")

        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as exc:
            doc.status = DocumentParseStatus.FAILED
            doc.reason = type(exc).__name__
            doc.add_warning("parse-failed",
                            f"the PDF could not be opened: {type(exc).__name__}: "
                            f"{exc}")
            return doc

        if getattr(reader, "is_encrypted", False):
            # An empty user password is extremely common for "protected" PDFs
            # and decrypting with it is not a bypass — it is what any reader
            # does. Anything else stays honestly `encrypted`.
            opened = False
            try:
                opened = bool(reader.decrypt(""))
            except Exception:
                opened = False
            if not opened:
                doc.status = DocumentParseStatus.ENCRYPTED
                doc.reason = "encrypted"
                doc.add_warning(
                    "encrypted",
                    "the PDF is encrypted and could not be opened with an empty "
                    "password; no text was extracted")
                return doc
            doc.add_warning(
                "encrypted-empty-password",
                "the PDF was encrypted with an empty user password and was "
                "opened as any reader would")

        _read_metadata(reader, doc)

        try:
            page_count = len(reader.pages)
        except Exception as exc:
            doc.status = DocumentParseStatus.FAILED
            doc.reason = type(exc).__name__
            doc.add_warning("parse-failed", f"page count unavailable: {exc}")
            return doc

        doc.metadata.page_count = page_count
        limits = context.limits
        pages_to_read = page_count
        if page_count > limits.pdf_max_pages:
            pages_to_read = limits.pdf_max_pages
            doc.mark_partial(
                "limit-exceeded",
                f"the PDF has {page_count} pages, above PDF_MAX_PAGES "
                f"({limits.pdf_max_pages}); only the first "
                f"{limits.pdf_max_pages} were extracted",
                limit="PDF_MAX_PAGES", observed=page_count,
                allowed=limits.pdf_max_pages)

        builder = DocumentBuilder(doc, max_blocks=limits.max_blocks,
                                  max_block_chars=limits.max_block_chars)
        key = context.logical_key
        builder.add_root(doc.title, {"kind": "pdf-block", "document": key,
                                     "page": 1, "blockIndex": 0})

        extracted_chars = 0
        empty_pages = 0
        failed_pages = 0
        for page_no in range(1, pages_to_read + 1):
            if builder.full:
                break
            try:
                text = reader.pages[page_no - 1].extract_text() or ""
            except Exception:
                failed_pages += 1
                continue
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                # An image-only page produces NO block. Emitting an empty one
                # would make the document look like it had readable content.
                empty_pages += 1
                continue
            extracted_chars += len(text.strip())
            _emit_page(builder, key, page_no, text)

        if failed_pages:
            doc.mark_partial(
                "page-extraction-failed",
                f"{failed_pages} page(s) could not be read; their text is "
                f"absent rather than guessed", pages=failed_pages)
        if empty_pages:
            doc.add_warning(
                "page-without-text",
                f"{empty_pages} page(s) contain no extractable text (they are "
                f"most likely images); no OCR was performed",
                pages=empty_pages)
            doc.note_unsupported(
                "image-only-page", empty_pages,
                "OCR is not performed in this phase")

        if page_count and extracted_chars < _MIN_MEANINGFUL_CHARS:
            # Detection is required in Phase 3; OCR is not. Say exactly that.
            doc.status = DocumentParseStatus.NEEDS_OCR
            doc.reason = "image-only"
            doc.add_warning(
                "needs-ocr",
                f"the PDF has {page_count} page(s) but no meaningful extractable "
                f"text, so it is image-only. OpenMind does NOT perform OCR in "
                f"this phase; no text was invented for it.",
                pages=page_count, characters=extracted_chars)

        doc.coverage["pages"] = page_count
        doc.coverage["pages_read"] = pages_to_read
        doc.coverage["pages_without_text"] = empty_pages
        return doc


def _read_metadata(reader: Any, doc: ParsedDocument) -> None:
    """Copy PDF document-information metadata. Never raises."""
    try:
        info = reader.metadata or {}
    except Exception:
        return

    def _get(name: str) -> str:
        try:
            value = info.get(name)
        except Exception:
            return ""
        return "" if value is None else str(value).strip()

    doc.metadata.author = _get("/Author")
    doc.metadata.created = _get("/CreationDate")
    doc.metadata.modified = _get("/ModDate")
    doc.metadata.producer = _get("/Producer") or _get("/Creator")
    title = _get("/Title")
    if title:
        doc.title = title
    subject = _get("/Subject")
    if subject:
        doc.metadata.extra["subject"] = subject
    # A version label is taken ONLY from an explicitly named metadata field.
    # Nothing is parsed out of the title or the filename.
    for field in ("/Version", "/DocumentVersion", "/Revision"):
        value = _get(field)
        if value:
            doc.metadata.version_label = value
            break


def _emit_page(builder: DocumentBuilder, key: str, page_no: int,
               text: str) -> None:
    """One ``page`` container plus one paragraph block per blank-line-separated
    run. The page block is stored but not indexed — its children carry the
    content, and embedding the whole page as well would return the same text
    twice for one query."""
    from ..domain.types import ContentMode
    page_key = f"page-{page_no:05d}"
    builder.add(DocumentBlockType.PAGE, "", key=page_key,
                locator={"kind": "pdf-block", "document": key, "page": page_no,
                         "blockIndex": 0},
                # A container with no text of its own is scaffolding, not a
                # verbatim quotation of anything.
                content_mode=ContentMode.DERIVED,
                indexable=False, metadata={"page": page_no})
    parts = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    if not parts:
        parts = [text]
    for index, part in enumerate(parts, start=1):
        builder.add(DocumentBlockType.PARAGRAPH, part,
                    key=f"{page_key}-b{index:04d}",
                    locator={"kind": "pdf-block", "document": key,
                             "page": page_no, "blockIndex": index},
                    parent_key=page_key,
                    metadata={"page": page_no, "block": index})


__all__ = ["PdfParser", "CLAIMS"]
