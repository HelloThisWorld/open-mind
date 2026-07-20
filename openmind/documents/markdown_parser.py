"""Markdown structure extraction.

WHY NOT A MARKDOWN LIBRARY
--------------------------
What this phase needs from Markdown is *structure with exact line ranges*:
headings and their hierarchy, paragraphs, list items, fenced code, block quotes
and simple pipe tables. A renderer answers a different question — how does this
look as HTML — and would cost a dependency, an AST walk, and a source-position
mapping that most renderers do not expose faithfully. Line ranges are the whole
point of a text-document locator, so a line-oriented scanner is the right tool.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No inline rendering, no reference-link resolution, no footnote assembly, no MDX.
Inline markup is left in the text verbatim, because the text IS the evidence: an
Evidence citation must reproduce what the file says, not a prettified version.

DETERMINISM
-----------
Single forward pass, no lookahead beyond the fence/table rules below, no
regular-expression backtracking on untrusted input. The same bytes always
produce the same blocks in the same order with the same keys.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..domain.types import ContentMode, DocumentBlockType, DocumentParseStatus
from .builder import DocumentBuilder, slug
from .models import (DocumentBlock, DocumentParseContext, DocumentProbe,
                     ParsedDocument)
from .security import decode_text

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`]*)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+(.+)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_SETEXT_RE = re.compile(r"^\s*(=+|-+)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")

#: Extensions this parser owns. `.markdown` and `.md` only — a `.txt` that
#: happens to contain a `#` is plain text, and pretending otherwise would invent
#: a heading hierarchy the author never wrote.
_EXTENSIONS = frozenset({".md", ".markdown", ".mdown", ".mkd"})


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    return probe.extension in _EXTENSIONS


class MarkdownParser:
    """Line-oriented Markdown structure extractor."""

    name = "markdown"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        if probe.magic.get("binary") or probe.magic.get("zip") \
                or probe.magic.get("pdf"):
            return False
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type="text/markdown",
                             title=context.filename or context.logical_key)
        if lossy:
            doc.add_warning(
                "decode-fallback",
                "the file is not valid UTF-8; it was decoded with replacement "
                "characters, so some text may not be exact",
                encoding=encoding)
        doc.metadata.extra["encoding"] = encoding

        key = context.logical_key
        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        lines = text.split("\n")
        builder.add_root(doc.title, _locator(key, 1, max(1, len(lines)), []))
        _scan(builder, key, lines, doc)

        # The document title is the first level-1 heading when there is one — a
        # recorded fact from the file, not a guess from the filename.
        for block in doc.blocks:
            if (block.block_type == DocumentBlockType.HEADING
                    and block.metadata.get("level") == 1):
                doc.title = block.text.strip()
                doc.blocks[0].text = doc.title
                break
        doc.coverage["lines"] = len(lines)
        return doc


def _locator(document: str, start: int, end: int,
             heading_path: List[str]) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"kind": "text-range", "document": document,
                           "startLine": start, "endLine": end}
    if heading_path:
        loc["headingPath"] = list(heading_path)
    return loc


def _scan(builder: DocumentBuilder, key: str, lines: List[str],
          doc: ParsedDocument) -> None:
    i = 0
    total = len(lines)
    para: List[str] = []
    para_start = 0

    def flush_paragraph() -> None:
        nonlocal para, para_start
        if not para:
            return
        body = "\n".join(para).strip("\n")
        if body.strip():
            builder.add(DocumentBlockType.PARAGRAPH, body,
                        key=f"p-{para_start:06d}",
                        locator=_locator(key, para_start, para_start + len(para) - 1,
                                         builder.heading_path))
        para = []

    while i < total:
        if builder.full:
            break
        line = lines[i]
        line_no = i + 1

        fence = _FENCE_RE.match(line)
        if fence:
            i = _fenced_code(builder, key, lines, i, fence, flush_paragraph)
            continue

        atx = _ATX_RE.match(line)
        if atx:
            flush_paragraph()
            level = len(atx.group(1))
            title = atx.group(2).strip()
            _emit_heading(builder, key, title, level, line_no, line_no)
            i += 1
            continue

        # Setext heading: a text line UNDERLINED by === or ---. Only valid when
        # the previous line is the pending paragraph's single line, otherwise a
        # `---` is a thematic break, not a heading.
        if (_SETEXT_RE.match(line) and len(para) == 1 and para[0].strip()
                and not _LIST_RE.match(para[0])):
            title = para[0].strip()
            level = 1 if line.strip().startswith("=") else 2
            start = para_start
            para = []
            _emit_heading(builder, key, title, level, start, line_no)
            i += 1
            continue

        if _TABLE_SEP_RE.match(line) and para and "|" in para[-1]:
            i = _pipe_table(builder, key, lines, i, para, para_start)
            para = []
            continue

        quote = _QUOTE_RE.match(line)
        if quote is not None and line.lstrip().startswith(">"):
            flush_paragraph()
            i = _block_quote(builder, key, lines, i)
            continue

        item = _LIST_RE.match(line)
        if item:
            flush_paragraph()
            builder.add(DocumentBlockType.LIST_ITEM, item.group(1).strip(),
                        key=f"li-{line_no:06d}",
                        locator=_locator(key, line_no, line_no,
                                         builder.heading_path))
            i += 1
            continue

        if not line.strip():
            flush_paragraph()
            i += 1
            continue

        if not para:
            para_start = line_no
        para.append(line)
        i += 1

    flush_paragraph()


def _emit_heading(builder: DocumentBuilder, key: str, title: str, level: int,
                  start: int, end: int) -> None:
    heading_key = f"h{level}-{slug(title) or start}"
    block = builder.add(
        DocumentBlockType.HEADING, title, key=heading_key,
        locator=_locator(key, start, end, builder.heading_path),
        metadata={"level": level})
    if block is not None:
        builder.push_heading(level, block.block_key, title)


def _fenced_code(builder: DocumentBuilder, key: str, lines: List[str], i: int,
                 fence: "re.Match[str]", flush: Any) -> int:
    flush()
    marker = fence.group(2)
    language = fence.group(3).strip()
    start = i + 1
    body: List[str] = []
    j = i + 1
    while j < len(lines):
        closing = _FENCE_RE.match(lines[j])
        if closing and closing.group(2)[0] == marker[0] \
                and len(closing.group(2)) >= len(marker) \
                and not closing.group(3).strip():
            break
        body.append(lines[j])
        j += 1
    end = min(j + 1, len(lines))
    builder.add(DocumentBlockType.CODE_BLOCK, "\n".join(body),
                key=f"code-{start:06d}",
                locator=_locator(key, start, end, builder.heading_path),
                metadata={"language": language} if language else {})
    return j + 1


def _block_quote(builder: DocumentBuilder, key: str, lines: List[str],
                 i: int) -> int:
    start = i + 1
    body: List[str] = []
    j = i
    while j < len(lines):
        m = _QUOTE_RE.match(lines[j])
        if m is None or not lines[j].lstrip().startswith(">"):
            break
        body.append(m.group(1))
        j += 1
    builder.add(DocumentBlockType.PARAGRAPH, "\n".join(body).strip(),
                key=f"quote-{start:06d}",
                locator=_locator(key, start, j, builder.heading_path),
                metadata={"quote": True})
    return j


def _split_row(line: str) -> List[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _pipe_table(builder: DocumentBuilder, key: str, lines: List[str], sep_idx: int,
                para: List[str], para_start: int) -> int:
    """Emit a table block plus one row block per data row.

    The header line is the pending paragraph's LAST line; anything before it was
    an ordinary paragraph and is emitted as such, so a table immediately after
    prose does not swallow the prose.
    """
    header_line = para[-1]
    header_line_no = para_start + len(para) - 1
    if len(para) > 1:
        lead = "\n".join(para[:-1]).strip()
        if lead:
            builder.add(DocumentBlockType.PARAGRAPH, lead,
                        key=f"p-{para_start:06d}",
                        locator=_locator(key, para_start, header_line_no - 1,
                                         builder.heading_path))

    headers = _split_row(header_line)
    j = sep_idx + 1
    rows: List[List[str]] = []
    row_lines: List[int] = []
    while j < len(lines) and "|" in lines[j] and lines[j].strip():
        rows.append(_split_row(lines[j]))
        row_lines.append(j + 1)
        j += 1

    table_key = f"table-{header_line_no:06d}"
    table = builder.add(
        DocumentBlockType.TABLE, " | ".join(headers), key=table_key,
        locator=_locator(key, header_line_no, j, builder.heading_path),
        content_mode=ContentMode.DERIVED,
        metadata={"columns": len(headers), "rows": len(rows),
                  "headers": ", ".join(headers)},
        indexable=False)
    parent = table.block_key if table is not None else builder.current_parent
    for n, (cells, line_no) in enumerate(zip(rows, row_lines)):
        # DERIVED: the header=value rendering is not what the file literally
        # says, so it must not claim to be verbatim. The locator still cites the
        # exact source line, which IS verbatim.
        pairs = [f"{headers[c] if c < len(headers) else f'col{c + 1}'}: {v}"
                 for c, v in enumerate(cells)]
        builder.add(DocumentBlockType.TABLE_ROW, "; ".join(pairs),
                    key=f"{table_key}-r{n + 1:04d}",
                    locator=_locator(key, line_no, line_no, builder.heading_path),
                    content_mode=ContentMode.DERIVED, parent_key=parent,
                    metadata={"row": n + 1, "raw": lines[line_no - 1].strip()})
    return j


__all__ = ["MarkdownParser", "CLAIMS"]
