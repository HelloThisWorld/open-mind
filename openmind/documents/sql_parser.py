"""SQL DDL structure via ``sqlglot``, with an honest text fallback.

WHY sqlglot
-----------
It is already a dependency (the folded DB-schema model uses it), it is
dialect-aware, and it parses without connecting to anything. Reusing it means SQL
documents and the existing schema model agree about what a table is.

THE FALLBACK IS THE INTERESTING PART
------------------------------------
Real enterprise DDL contains vendor syntax no general parser accepts —
``CREATE PROCEDURE`` bodies, ``PACKAGE BODY``, storage clauses, hints. When
``sqlglot`` cannot parse a statement, this parser does NOT: drop it, guess at it,
or fail the document. It emits the statement as a bounded verbatim text block
with its exact source line range and records an ``unsupported-syntax`` warning
naming the position. The content stays searchable and citable; the limitation
stays visible.

Statement splitting is done here rather than by ``sqlglot`` so that a
parse failure of one statement cannot cost the line numbers of the others: the
splitter is a small quote/comment-aware scanner that tracks source lines, and
each statement is parsed independently.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Dict, Iterator, List, Tuple

from ..domain.types import ContentMode, DocumentBlockType
from .builder import DocumentBuilder, slug
from .models import DocumentParseContext, DocumentProbe, ParsedDocument
from .security import decode_text

_EXTENSIONS = frozenset({".sql", ".ddl"})

#: Statement kinds that become their own `sql-object` block. Anything else is
#: kept as a bounded text block rather than being classified as something it is
#: not.
_OBJECT_KINDS = {
    "Create": "object", "Alter": "alteration", "Drop": "drop",
    "Insert": "data", "Comment": "comment",
}


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf"):
        return False
    return probe.extension in _EXTENSIONS


class SqlParser:
    """Tables, views, columns, indexes, constraints and sequences from DDL."""

    name = "sql"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type="application/sql",
                             title=context.filename or context.logical_key)
        if lossy:
            doc.add_warning("decode-fallback",
                            "the file is not valid UTF-8; it was decoded with "
                            "replacement characters", encoding=encoding)
        doc.metadata.extra["encoding"] = encoding

        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        key = context.logical_key
        lines = text.split("\n")
        builder.add_root(doc.title, _locator(key, 1, max(1, len(lines)), ""))

        dialect = str(context.parser_options.get("dialect") or "") or None
        try:
            import sqlglot
        except ImportError:
            sqlglot = None                     # noqa: N806 - honest fallback below
            doc.add_warning(
                "dependency-unavailable",
                "sqlglot is not installed, so statements are stored as bounded "
                "text with exact line ranges instead of structured objects",
                distribution="sqlglot")

        statements = 0
        unparsed = 0
        objects = 0
        for sql, start, end in _split_statements(text):
            if builder.full:
                break
            statements += 1
            parsed = None
            if sqlglot is not None:
                with _quiet_sqlglot():
                    try:
                        parsed = sqlglot.parse_one(sql, read=dialect)
                    except Exception:
                        parsed = None
            # sqlglot does not always RAISE on syntax it cannot handle: it falls
            # back to a generic `Command` node holding the raw text. Treating
            # that as a successful parse would hide exactly the case this
            # fallback exists for, so it counts as unstructured too.
            structured = (parsed is not None and not _is_unstructured(parsed)
                          and _emit_object(builder, key, sql, parsed, start, end))
            if structured:
                objects += 1
            else:
                # Every non-structured outcome lands here — a hard parse error,
                # sqlglot's generic passthrough node, and a statement that is
                # simply not an object definition. All are kept verbatim with
                # their exact line range rather than dropped or guessed at.
                unparsed += 1
                _emit_text_statement(builder, key, sql, start, end)

        if unparsed:
            doc.add_warning(
                "unsupported-syntax",
                f"{unparsed} of {statements} statement(s) could not be "
                f"structured as database objects; they were kept verbatim with "
                f"their exact source line ranges rather than dropped or guessed "
                f"at",
                statements=unparsed, total=statements)
            doc.note_unsupported(
                "unstructured-statement", unparsed,
                "vendor-specific or non-DDL statements are stored as text, not "
                "as structured objects")
        doc.coverage["statements"] = statements
        doc.coverage["objects"] = objects
        doc.coverage["lines"] = len(lines)
        return doc


@contextlib.contextmanager
def _quiet_sqlglot() -> Iterator[None]:
    """Silence sqlglot's per-statement "unsupported syntax" logger.

    The fallback is a DOCUMENTED, reported outcome here (an
    ``unsupported-syntax`` warning on the parsed document), so letting the
    library also print its own line per statement would put unstructured noise
    on stderr for a condition already handled honestly.
    """
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _is_unstructured(parsed: Any) -> bool:
    """Whether sqlglot produced its generic passthrough node rather than a real
    parse. ``Command`` is what it emits for syntax it does not model."""
    return type(parsed).__name__ in ("Command", "Anonymous")


def _locator(document: str, start: int, end: int, symbol: str) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"kind": "text-range", "document": document,
                           "startLine": start, "endLine": end}
    if symbol:
        loc["symbol"] = symbol
    return loc


def _split_statements(text: str) -> Iterator[Tuple[str, int, int]]:
    """Yield ``(sql, start_line, end_line)`` for each ``;``-terminated statement.

    Quote- and comment-aware, so a semicolon inside a string literal, a
    ``--`` comment or a ``/* */`` block does not split a statement in half.
    Line numbers are tracked here rather than recomputed later, so a statement
    that fails to parse still has an exact, citable range.
    """
    buffer: List[str] = []
    line = 1
    start_line = 1
    in_single = in_double = in_line_comment = in_block_comment = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            in_line_comment = False
            buffer.append(ch)
            i += 1
            continue
        if in_line_comment:
            buffer.append(ch)
            i += 1
            continue
        if in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_single or in_double:
            buffer.append(ch)
            if (ch == "'" and in_single) or (ch == '"' and in_double):
                in_single = in_double = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buffer.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buffer.append(ch)
            buffer.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single = True
            buffer.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buffer.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buffer).strip()
            if statement:
                yield statement, start_line, line
            buffer = []
            start_line = line if text[i:i + 2] != ";\n" else line + 1
            i += 1
            continue
        if not buffer and ch.isspace():
            # Leading whitespace belongs to no statement; keep start_line honest.
            start_line = line
            i += 1
            continue
        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        yield tail, start_line, line


def _emit_text_statement(builder: DocumentBuilder, key: str, sql: str,
                         start: int, end: int) -> None:
    """The honest fallback: verbatim SQL with an exact line range."""
    builder.add(DocumentBlockType.CODE_BLOCK, sql,
                key=f"stmt-{start:06d}",
                locator=_locator(key, start, end, ""),
                metadata={"parsed": False})


def _expression_name(expression: Any) -> str:
    for attr in ("this", "name"):
        value = getattr(expression, attr, None)
        if isinstance(value, str) and value:
            return value
    try:
        return expression.sql(identify=False)
    except Exception:
        return ""


def _emit_object(builder: DocumentBuilder, key: str, sql: str, parsed: Any,
                 start: int, end: int) -> bool:
    """Emit one ``sql-object`` block plus a child per column / constraint / index.

    Returns False when the statement is not an object definition, so the caller
    can fall back to a verbatim text block rather than this inventing a shape.
    """
    import sqlglot
    from sqlglot import exp

    kind = _OBJECT_KINDS.get(type(parsed).__name__)
    if kind is None:
        return False

    table = parsed.find(exp.Table)
    name = ""
    if table is not None:
        try:
            name = table.sql(identify=False)
        except Exception:
            name = _expression_name(table)
    if not name:
        name = _expression_name(parsed)[:80]

    object_type = "table"
    if isinstance(parsed, exp.Create):
        object_type = str(getattr(parsed, "kind", "") or "table").lower()

    object_key = f"sql-{slug(f'{object_type}-{name}') or start}"
    block = builder.add(
        DocumentBlockType.SQL_OBJECT, sql, key=object_key,
        locator=_locator(key, start, end, name),
        metadata={"object_type": object_type, "name": name, "statement": kind,
                  "parsed": True})
    parent = block.block_key if block is not None else builder.current_parent

    if not isinstance(parsed, exp.Create):
        return True

    schema = parsed.find(exp.Schema)
    if schema is None:
        return True
    for index, definition in enumerate(schema.expressions, start=1):
        if builder.full:
            break
        try:
            rendered = definition.sql(identify=False)
        except Exception:
            continue
        if isinstance(definition, exp.ColumnDef):
            child_kind = "column"
            child_name = _expression_name(definition.this) or f"col{index}"
        elif isinstance(definition, exp.Constraint) or \
                type(definition).__name__.endswith("Constraint"):
            child_kind = "constraint"
            child_name = _expression_name(definition) or f"constraint{index}"
        else:
            child_kind = "definition"
            child_name = f"item{index}"
        builder.add(
            DocumentBlockType.SQL_OBJECT, f"{name}.{child_name}: {rendered}",
            key=f"{object_key}-{child_kind}-{slug(child_name) or index}",
            locator=_locator(key, start, end, f"{name}.{child_name}"),
            # DERIVED: `table.column: <ddl>` is a rendering of the definition,
            # not the file's literal text. The parent object block IS verbatim.
            content_mode=ContentMode.DERIVED, parent_key=parent,
            metadata={"object_type": child_kind, "name": child_name,
                      "table": name})
    return True


__all__ = ["SqlParser", "CLAIMS"]
