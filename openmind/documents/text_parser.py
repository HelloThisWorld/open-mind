"""Plain text, reStructuredText and AsciiDoc.

THE FLOOR, NOT A COMPROMISE
---------------------------
This parser is the registry's last resort: whatever no other parser claims and is
not binary lands here. That makes its contract "extract what is deterministically
identifiable, and be explicit about the rest" rather than "understand the
format".

What it recovers:

* exact line ranges for everything;
* blank-line-separated paragraphs;
* headings — RST/AsciiDoc-style underlines (``====`` / ``----`` under a title
  line), AsciiDoc ``= Title`` prefixes, and numbered section headings;
* list items (``-``, ``*``, ``1.``, ``a)``);
* indented code-like blocks, where an indentation jump makes them unambiguous.

What it does not: RST/AsciiDoc directives, roles, includes and substitutions.
Those are RECORDED as unsupported content with a count, so a reader can tell
"this document has 14 directives we did not expand" from "this document has
none" — which is the whole difference between a limitation and a silent lie.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from ..domain.types import DocumentBlockType
from .builder import DocumentBuilder, slug
from .models import DocumentParseContext, DocumentProbe, ParsedDocument
from .security import decode_text

#: An underline made of one repeated punctuation character, RST/AsciiDoc style.
_UNDERLINE_RE = re.compile(r"^\s*([=\-~^\"'`#*+_])\1{2,}\s*$")
_ADOC_HEADING_RE = re.compile(r"^(={1,6})\s+(\S.*)$")
_NUMBERED_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.{0,120})$")
_LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d{1,9}[.)]|[a-z][.)])\s+(.+)$")
_RST_DIRECTIVE_RE = re.compile(r"^\s*\.\.\s+[a-z][\w-]*::")
_ADOC_DIRECTIVE_RE = re.compile(r"^\s*(include::|ifdef::|ifndef::|endif::|:\w[\w-]*:)")

#: Underline character -> heading level, so ``===`` outranks ``---`` the way
#: both RST convention and AsciiDoc do.
_UNDERLINE_LEVEL = {"=": 1, "-": 2, "~": 3, "^": 4, "\"": 5, "'": 6,
                    "`": 3, "#": 1, "*": 2, "+": 4, "_": 2}

_EXTENSIONS = frozenset({".txt", ".text", ".rst", ".adoc", ".asciidoc", ".asc",
                         ".log", ""})

_MEDIA = {".rst": "text/x-rst", ".adoc": "text/asciidoc",
          ".asciidoc": "text/asciidoc", ".asc": "text/asciidoc"}


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    """The fallback claim: anything textual that no earlier parser wanted.

    Binary signatures are refused outright — decoding a PDF or a ZIP as text
    would produce blocks full of mojibake that LOOK like extracted content.
    """
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf") or probe.magic.get("ole2"):
        return False
    return True


class TextParser:
    """Deterministic structure for plain text, RST and AsciiDoc."""

    name = "text"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        ext = os.path.splitext(context.logical_key)[1].lower()
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type=_MEDIA.get(ext, "text/plain"),
                             title=context.filename or context.logical_key)
        if lossy:
            doc.add_warning(
                "decode-fallback",
                "the file is not valid UTF-8; it was decoded with replacement "
                "characters, so some text may not be exact", encoding=encoding)
        doc.metadata.extra["encoding"] = encoding

        key = context.logical_key
        lines = text.split("\n")
        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        builder.add_root(doc.title, _locator(key, 1, max(1, len(lines)), []))
        _scan(builder, key, lines, doc)
        doc.coverage["lines"] = len(lines)
        for block in doc.blocks:
            if block.block_type == DocumentBlockType.HEADING \
                    and block.metadata.get("level") == 1:
                doc.title = block.text.strip()
                doc.blocks[0].text = doc.title
                break
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
    directives = 0
    para: List[str] = []
    para_start = 0

    def flush() -> None:
        nonlocal para
        if not para:
            return
        body = "\n".join(para).strip("\n")
        if body.strip():
            builder.add(DocumentBlockType.PARAGRAPH, body,
                        key=f"p-{para_start:06d}",
                        locator=_locator(key, para_start,
                                         para_start + len(para) - 1,
                                         builder.heading_path))
        para = []

    while i < total:
        if builder.full:
            break
        line = lines[i]
        line_no = i + 1

        if _RST_DIRECTIVE_RE.match(line) or _ADOC_DIRECTIVE_RE.match(line):
            flush()
            directives += 1
            i += 1
            continue

        adoc = _ADOC_HEADING_RE.match(line)
        if adoc:
            flush()
            _emit_heading(builder, key, adoc.group(2).strip(),
                          len(adoc.group(1)), line_no, line_no)
            i += 1
            continue

        # Underlined heading: a non-empty title line followed by an underline of
        # at least the title's length. The length rule is what keeps a `-----`
        # separator after a short line from being read as a heading.
        if (i + 1 < total and para == [] and line.strip()
                and _UNDERLINE_RE.match(lines[i + 1])
                and len(lines[i + 1].strip()) >= max(3, len(line.strip()) - 2)
                and not _LIST_RE.match(line)):
            char = lines[i + 1].strip()[0]
            _emit_heading(builder, key, line.strip(),
                          _UNDERLINE_LEVEL.get(char, 3), line_no, line_no + 1)
            i += 2
            continue

        # Checked BEFORE the list rule, which would otherwise swallow every
        # "1. Overview" section title as an ordered-list item. The discriminator
        # is _is_section_heading (standalone, short, followed by a blank line),
        # so a genuine numbered list — whose items run consecutively — still
        # reads as a list.
        numbered = _NUMBERED_RE.match(line)
        if numbered and not para and _is_section_heading(lines, i):
            flush()
            level = numbered.group(1).count(".") + 1
            _emit_heading(builder, key, line.strip(), min(level, 6),
                          line_no, line_no)
            i += 1
            continue

        item = _LIST_RE.match(line)
        if item:
            flush()
            builder.add(DocumentBlockType.LIST_ITEM, item.group(1).strip(),
                        key=f"li-{line_no:06d}",
                        locator=_locator(key, line_no, line_no,
                                         builder.heading_path))
            i += 1
            continue

        # An indented run following a blank line, where the indent is at least
        # four columns: the one indentation pattern that is code in plain text,
        # RST and AsciiDoc alike.
        if not para and line.startswith(("    ", "\t")) and line.strip():
            i = _indented_code(builder, key, lines, i)
            continue

        if not line.strip():
            flush()
            i += 1
            continue

        if not para:
            para_start = line_no
        para.append(line)
        i += 1

    flush()
    if directives:
        doc.note_unsupported(
            "markup-directive", directives,
            "RST/AsciiDoc directives are recorded but not expanded in this phase")


def _is_section_heading(lines: List[str], i: int) -> bool:
    """A numbered line is a heading only when it STANDS ALONE: short, with a
    blank line (or the file start) before it and a blank line (or the file end)
    after it.

    Both sides matter. Requiring only a blank line after would make the final
    item of ``1. do this / 2. do that / 3. do the other`` a section heading,
    because it happens to end the list. Requiring a blank line before is what
    distinguishes a section title from a list item that follows its siblings.
    """
    line = lines[i].strip()
    if len(line) > 120:
        return False
    prev = lines[i - 1] if i > 0 else ""
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    return not prev.strip() and not nxt.strip()


def _emit_heading(builder: DocumentBuilder, key: str, title: str, level: int,
                  start: int, end: int) -> None:
    block = builder.add(
        DocumentBlockType.HEADING, title, key=f"h{level}-{slug(title) or start}",
        locator=_locator(key, start, end, builder.heading_path),
        metadata={"level": level})
    if block is not None:
        builder.push_heading(level, block.block_key, title)


def _indented_code(builder: DocumentBuilder, key: str, lines: List[str],
                   i: int) -> int:
    start = i + 1
    body: List[str] = []
    j = i
    while j < len(lines) and (lines[j].startswith(("    ", "\t"))
                              or not lines[j].strip()):
        body.append(lines[j])
        j += 1
    while body and not body[-1].strip():         # drop trailing blank lines
        body.pop()
        j -= 1
    builder.add(DocumentBlockType.CODE_BLOCK, "\n".join(body),
                key=f"code-{start:06d}",
                locator=_locator(key, start, max(start, j),
                                 builder.heading_path))
    return max(j, i + 1)


__all__ = ["TextParser", "CLAIMS"]
