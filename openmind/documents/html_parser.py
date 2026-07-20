"""HTML structure extraction, using the standard library only.

WHY THE STDLIB PARSER
---------------------
``html.parser.HTMLParser`` is a tolerant, streaming, pure-Python tokenizer with
no network stack, no CSS engine and no JavaScript. That is exactly the right
capability envelope for untrusted input: there is nothing in it that *can* fetch
a linked resource or run a script, so "do not execute JavaScript, do not fetch
linked resources" is guaranteed by construction rather than by discipline.
Adding a heavier parser would buy fidelity we do not need and an attack surface
we would then have to bound.

WHAT IS DROPPED, ON PURPOSE
---------------------------
``<script>``, ``<style>``, ``<template>``, ``<noscript>`` and comments are not
content — indexing them would put minified JavaScript into a knowledge base. So
are elements carrying the ``hidden`` attribute or ``display:none``/
``visibility:hidden`` in an inline style: the author marked them as not for the
reader. That is the *deterministic* part of "hidden"; full CSS cascade
resolution is not attempted, and no claim is made that it was.

LOCATORS
--------
Each content element gets a stable ``html-element`` locator: the tag, its
document-order index among elements of that tag, and a positional ``domPath``
(``html/body/main/section[2]/p[3]``). Both are recomputable from the same bytes,
which is what makes the citation portable.
"""
from __future__ import annotations

from html.parser import HTMLParser as _StdHTMLParser
from typing import Any, Dict, List, Optional, Tuple

from ..domain.types import ContentMode, DocumentBlockType
from .builder import DocumentBuilder, slug
from .models import DocumentParseContext, DocumentProbe, ParsedDocument
from .security import decode_text

#: Never emitted as content, and their text is discarded entirely.
_DROP_CONTENT = frozenset({"script", "style", "template", "noscript", "svg",
                           "head", "meta", "link"})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = frozenset({"p", "li", "pre", "code", "blockquote", "td", "th",
                         "figcaption", "dd", "dt"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"})

_EXTENSIONS = frozenset({".html", ".htm", ".xhtml"})

#: Dropped element kinds worth REPORTING as unsupported content, and why. A drop
#: not listed here (head/meta/link) is document plumbing, and reporting it would
#: bury the drops a reader actually needs to know about.
_REPORTED_DROPS = {
    "script": "script content is never indexed and never executed",
    "style": "stylesheet content is not document content",
    "noscript": "noscript fallback markup is not indexed",
    "template": "inert template markup is not indexed",
    "svg": "inline SVG is not extracted in this phase",
    "image": "embedded images are not extracted; OCR is not performed",
    "hidden-element": "elements the author marked hidden are not indexed",
}


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf"):
        return False
    # Signature-first: a `.txt` that really is a document body is still HTML, and
    # a `.html` whose bytes are plainly not is still claimed (the extension is a
    # legitimate declaration for a text format).
    return (probe.detected_media_type == "text/html"
            or probe.extension in _EXTENSIONS)


class HtmlParser:
    """Extract title, headings, paragraphs, lists, tables and code from HTML."""

    name = "html"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type="text/html",
                             title=context.filename or context.logical_key)
        if lossy:
            doc.add_warning(
                "decode-fallback",
                "the file is not valid UTF-8; it was decoded with replacement "
                "characters, so some text may not be exact", encoding=encoding)
        doc.metadata.extra["encoding"] = encoding

        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        builder.add_root(doc.title,
                         {"kind": "html-element", "document": context.logical_key,
                          "element": "html", "elementIndex": 0, "domPath": "html"})
        extractor = _Extractor(builder, context.logical_key, doc)
        extractor.feed(text)
        extractor.close()
        extractor.finish()

        if extractor.doc_title:
            doc.title = extractor.doc_title
            doc.blocks[0].text = doc.title
        for kind, count in sorted(extractor.dropped.items()):
            if kind not in _REPORTED_DROPS:
                # `meta`/`link`/`head` are document plumbing, not content an
                # author would expect to find. Listing them as "unsupported
                # content" would bury the drops that actually matter.
                continue
            doc.note_unsupported(kind, count, _REPORTED_DROPS[kind])
        doc.coverage["elements"] = extractor.element_count
        return doc


class _Extractor(_StdHTMLParser):
    """Streaming HTML → blocks. One pass, no DOM materialization."""

    def __init__(self, builder: DocumentBuilder, logical_key: str,
                 doc: ParsedDocument) -> None:
        super().__init__(convert_charrefs=True)
        self.builder = builder
        self.key = logical_key
        self.doc = doc
        self.doc_title = ""
        self.element_count = 0
        self.dropped: Dict[str, int] = {}
        #: (tag, sibling-index) pairs, for the positional domPath.
        self._path: List[Tuple[str, int]] = []
        #: per-parent child counts, so `section[2]` counts siblings not globals.
        self._sibling_counts: List[Dict[str, int]] = [{}]
        self._tag_index: Dict[str, int] = {}
        self._suppress_depth = 0
        self._suppressed_tag = ""
        self._capture: Optional[str] = None
        self._buffer: List[str] = []
        self._capture_locator: Dict[str, Any] = {}
        self._in_title = False
        self._table_depth = 0
        self._table_key = ""
        self._row_cells: List[str] = []
        self._row_index = 0

    # -- helpers ------------------------------------------------------------
    def _dom_path(self) -> str:
        return "/".join(f"{tag}[{idx}]" if idx > 1 else tag
                        for tag, idx in self._path)

    def _next_tag_index(self, tag: str) -> int:
        self._tag_index[tag] = self._tag_index.get(tag, 0) + 1
        return self._tag_index[tag]

    @staticmethod
    def _is_hidden(attrs: List[Tuple[str, Optional[str]]]) -> bool:
        for name, value in attrs:
            if name == "hidden":
                return True
            if name == "aria-hidden" and (value or "").lower() == "true":
                return True
            if name == "style":
                style = (value or "").lower().replace(" ", "")
                if "display:none" in style or "visibility:hidden" in style:
                    return True
        return False

    def _flush(self) -> None:
        if self._capture is None:
            return
        tag = self._capture
        body = "".join(self._buffer)
        # Collapse runs of whitespace: HTML source line breaks are formatting,
        # not content, and preserving them would make every citation ragged.
        # <pre> is exempt — there the whitespace IS the content.
        if tag != "pre":
            body = " ".join(body.split())
        else:
            body = body.strip("\n")
        self._capture = None
        self._buffer = []
        if not body.strip():
            return
        if self._table_depth and tag in ("td", "th"):
            self._row_cells.append(body)
            return
        block_type = (DocumentBlockType.CODE_BLOCK if tag in ("pre", "code")
                      else DocumentBlockType.LIST_ITEM if tag in ("li", "dd", "dt")
                      else DocumentBlockType.PARAGRAPH)
        self.builder.add(block_type, body,
                         key=f"{tag}-{self._capture_locator.get('elementIndex', 0):05d}",
                         locator=dict(self._capture_locator))

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag: str,
                        attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag == self._suppressed_tag and tag not in _VOID:
                self._suppress_depth += 1
            return
        if tag in _DROP_CONTENT and tag != "head":
            self.dropped[tag] = self.dropped.get(tag, 0) + 1
            if tag not in _VOID:
                self._suppress_depth = 1
                self._suppressed_tag = tag
            if tag == "head":
                self._suppress_depth = 0
            return
        if tag == "img":
            self.dropped["image"] = self.dropped.get("image", 0) + 1
            return
        if self._is_hidden(attrs):
            self.dropped["hidden-element"] = self.dropped.get("hidden-element", 0) + 1
            if tag not in _VOID:
                self._suppress_depth = 1
                self._suppressed_tag = tag
            return

        if tag not in _VOID:
            counts = self._sibling_counts[-1]
            counts[tag] = counts.get(tag, 0) + 1
            self._path.append((tag, counts[tag]))
            self._sibling_counts.append({})
        self.element_count += 1

        if tag == "title":
            self._in_title = True
            self._buffer = []
            return
        if tag == "table":
            self._flush()
            self._table_depth += 1
            index = self._next_tag_index("table")
            self._table_key = f"table-{index:04d}"
            self.builder.add(
                DocumentBlockType.TABLE, "", key=self._table_key,
                locator=self._locator("table", index),
                content_mode=ContentMode.DERIVED, indexable=False,
                metadata={"tableIndex": index})
            self._row_index = 0
            return
        if tag == "tr" and self._table_depth:
            self._flush()
            self._row_cells = []
            self._row_index += 1
            return
        if tag in _HEADINGS:
            self._flush()
            self._capture = tag
            self._buffer = []
            self._capture_locator = self._locator(tag, self._next_tag_index(tag))
            return
        if tag in _BLOCK_TAGS:
            if self._capture != "pre":       # <code> inside <pre> stays one block
                self._flush()
                self._capture = tag
                self._buffer = []
                self._capture_locator = self._locator(tag,
                                                      self._next_tag_index(tag))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag == self._suppressed_tag:
                self._suppress_depth -= 1
                if self._suppress_depth == 0:
                    self._suppressed_tag = ""
            if self._path and self._path[-1][0] == tag:
                self._path.pop()
                if len(self._sibling_counts) > 1:
                    self._sibling_counts.pop()
            return
        if tag == "title":
            self._in_title = False
            self.doc_title = " ".join("".join(self._buffer).split())
            self._buffer = []
        elif tag in _HEADINGS:
            body = " ".join("".join(self._buffer).split())
            locator = dict(self._capture_locator)
            self._capture = None
            self._buffer = []
            if body:
                level = _HEADINGS[tag]
                block = self.builder.add(
                    DocumentBlockType.HEADING, body,
                    key=f"h{level}-{slug(body) or locator.get('elementIndex', 0)}",
                    locator=locator, metadata={"level": level})
                if block is not None:
                    self.builder.push_heading(level, block.block_key, body)
        elif tag == "tr" and self._table_depth:
            self._flush()
            self._emit_row()
        elif tag == "table" and self._table_depth:
            self._flush()
            self._table_depth = max(0, self._table_depth - 1)
        elif tag in _BLOCK_TAGS and self._capture == tag:
            self._flush()

        if self._path and self._path[-1][0] == tag:
            self._path.pop()
            if len(self._sibling_counts) > 1:
                self._sibling_counts.pop()

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        if self._in_title or self._capture is not None:
            self._buffer.append(data)
        elif self._table_depth and self._row_cells is not None:
            pass                       # cell text arrives inside td/th capture

    def _locator(self, element: str, index: int) -> Dict[str, Any]:
        loc: Dict[str, Any] = {
            "kind": "html-element", "document": self.key, "element": element,
            "elementIndex": index, "domPath": self._dom_path() or element,
        }
        if self.builder.heading_path:
            loc["headingPath"] = list(self.builder.heading_path)
        return loc

    def _emit_row(self) -> None:
        if not self._row_cells:
            return
        cells = list(self._row_cells)
        self._row_cells = []
        # DERIVED: joining cells with a separator is a rendering of the row, not
        # what the markup literally contains.
        self.builder.add(
            DocumentBlockType.TABLE_ROW, " | ".join(cells),
            key=f"{self._table_key or 'table'}-r{self._row_index:04d}",
            locator=self._locator("tr", self._row_index),
            content_mode=ContentMode.DERIVED,
            parent_key=self._table_key or self.builder.current_parent,
            metadata={"row": self._row_index, "cells": len(cells)})

    def finish(self) -> None:
        self._flush()
        if self._row_cells:
            self._emit_row()


__all__ = ["HtmlParser", "CLAIMS"]
