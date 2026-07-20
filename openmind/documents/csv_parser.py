"""CSV and TSV, using the standard-library reader.

WHY THE STDLIB
--------------
``csv`` implements RFC 4180 quoting, embedded newlines and escape handling
correctly, has no dependencies, and cannot be talked into evaluating anything.
Dialect detection uses ``csv.Sniffer`` on a BOUNDED sample — an unbounded sniff
on a hostile file is a denial-of-service in one line of code.

MALFORMED INPUT IS REPORTED, NOT REINTERPRETED
----------------------------------------------
Two things a naive importer does silently, and this one refuses to:

* **Guessing a dialect it could not detect.** When the sniffer fails, the parser
  falls back to the extension's conventional delimiter AND records a
  ``dialect-uncertain`` warning, so a mis-split file is visible rather than
  mysterious.
* **Reshaping ragged rows.** A row whose field count differs from the header's
  is kept as-is, counted, and reported in a ``ragged-rows`` warning. Padding or
  truncating it to fit would fabricate data.

Header detection is deterministic: the first row is the header when every one of
its fields is non-empty and none of them parses as a number. Anything less
certain is treated as data, and the columns are named ``col1..colN``.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List, Optional

from ..domain.types import ContentMode, DocumentBlockType
from .builder import DocumentBuilder
from .models import DocumentParseContext, DocumentProbe, ParsedDocument
from .security import decode_text

#: Bounded sniff window. Big enough to see a representative sample, small enough
#: that sniffing a hostile 25 MB file is still instant.
_SNIFF_BYTES = 16_384
_EXTENSIONS = frozenset({".csv", ".tsv"})
_DEFAULT_DELIMITER = {".csv": ",", ".tsv": "\t"}

#: Column-name limit for a header row. A "CSV" with 10k columns is a
#: transposed data dump, not a document.
_MAX_COLUMNS = 512


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf"):
        return False
    return probe.extension in _EXTENSIONS


class CsvParser:
    """Row-oriented CSV/TSV extraction with bounded dialect detection."""

    name = "csv"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        ext = os.path.splitext(context.logical_key)[1].lower()
        doc = ParsedDocument(
            parser_name=self.name, parser_version=self.version,
            media_type=("text/tab-separated-values" if ext == ".tsv"
                        else "text/csv"),
            title=context.filename or context.logical_key)
        if lossy:
            doc.add_warning(
                "decode-fallback",
                "the file is not valid UTF-8; it was decoded with replacement "
                "characters, so some values may not be exact", encoding=encoding)
        doc.metadata.extra["encoding"] = encoding

        delimiter, sniffed = _detect_delimiter(text, ext, doc)
        doc.metadata.extra["delimiter"] = ("\\t" if delimiter == "\t"
                                           else delimiter)
        doc.metadata.extra["dialect_detected"] = sniffed

        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        key = context.logical_key
        builder.add_root(doc.title, _locator(key, "A1"))

        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        try:
            rows = _read_bounded(reader, context.limits.csv_max_rows, doc)
        except csv.Error as exc:
            # A structurally broken file (an unterminated quote spanning the
            # whole document) is reported as partial with whatever was read,
            # never silently re-parsed with different rules.
            doc.mark_partial("malformed-csv",
                             f"the file could not be read past a CSV error: {exc}")
            rows = []

        if not rows:
            doc.coverage["rows"] = 0
            doc.coverage["columns"] = 0
            return doc

        headers, data_rows, header_row_no = _split_header(rows)
        doc.metadata.extra["has_header"] = header_row_no == 1
        table_key = "table-0001"
        builder.add(DocumentBlockType.TABLE, " | ".join(headers), key=table_key,
                    locator=_locator(key, _range(1, len(headers))),
                    content_mode=ContentMode.DERIVED, indexable=False,
                    metadata={"columns": len(headers), "rows": len(data_rows),
                              "headers": ", ".join(headers[:_MAX_COLUMNS])})

        ragged = 0
        for offset, cells in enumerate(data_rows):
            row_no = header_row_no + offset + 1
            if len(cells) != len(headers):
                ragged += 1
            pairs = [f"{headers[c] if c < len(headers) else f'col{c + 1}'}: {v}"
                     for c, v in enumerate(cells) if str(v).strip()]
            if not pairs:
                continue
            builder.add(
                DocumentBlockType.TABLE_ROW, "; ".join(pairs),
                key=f"{table_key}-r{row_no:06d}",
                locator=_locator(key, _range(row_no, len(cells)),
                                 row_index=row_no),
                # DERIVED: `header: value` is a rendering of the row, not the
                # file's literal bytes. The locator still cites the exact row.
                content_mode=ContentMode.DERIVED, parent_key=table_key,
                metadata={"row": row_no, "cells": len(cells)})
            if builder.full:
                break

        if ragged:
            doc.add_warning(
                "ragged-rows",
                f"{ragged} row(s) do not have the header's {len(headers)} "
                f"field(s); they were kept exactly as read, not padded or "
                f"truncated",
                rows=ragged, expected=len(headers))
        doc.coverage["rows"] = len(data_rows)
        doc.coverage["columns"] = len(headers)
        return doc


def _detect_delimiter(text: str, ext: str, doc: ParsedDocument) -> tuple:
    """(delimiter, was_sniffed). Falls back to the extension's convention and
    says so, rather than guessing silently."""
    sample = text[:_SNIFF_BYTES]
    if sample.strip():
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            return dialect.delimiter, True
        except csv.Error:
            pass
    fallback = _DEFAULT_DELIMITER.get(ext, ",")
    doc.add_warning(
        "dialect-uncertain",
        f"the CSV dialect could not be detected from the first "
        f"{_SNIFF_BYTES} bytes; the conventional delimiter for {ext or 'csv'} "
        f"({'tab' if fallback == chr(9) else fallback!r}) was used",
        delimiter=("\\t" if fallback == "\t" else fallback))
    return fallback, False


def _read_bounded(reader: Any, max_rows: int,
                  doc: ParsedDocument) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in reader:
        if len(rows) >= max_rows:
            doc.mark_partial(
                "limit-exceeded",
                f"the file has more than CSV_MAX_ROWS ({max_rows}) rows; the "
                f"remainder was not read",
                limit="CSV_MAX_ROWS", allowed=max_rows)
            break
        if any(str(c).strip() for c in row):
            rows.append([str(c) for c in row])
    return rows


def _looks_numeric(value: str) -> bool:
    v = (value or "").strip().replace(",", "").replace("%", "")
    if not v:
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def _split_header(rows: List[List[str]]) -> tuple:
    """(headers, data_rows, header_row_number).

    The first row is the header only when every field is non-empty and none is
    numeric. Otherwise the data starts at row 1 and columns are positional —
    inventing header names from a data row would mislabel every citation.
    """
    first = rows[0]
    is_header = (len(first) > 0
                 and all(str(c).strip() for c in first)
                 and not any(_looks_numeric(c) for c in first))
    if is_header:
        return ([str(c).strip() for c in first[:_MAX_COLUMNS]], rows[1:], 1)
    width = max(len(r) for r in rows)
    return ([f"col{i + 1}" for i in range(min(width, _MAX_COLUMNS))], rows, 0)


def _column_letter(index: int) -> str:
    """1-based column index -> spreadsheet letter (1 -> A, 27 -> AA)."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters or "A"


def _range(row_no: int, width: int) -> str:
    last = _column_letter(max(1, width))
    return f"A{row_no}:{last}{row_no}"


def _locator(document: str, cell_range: str,
             row_index: Optional[int] = None) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"kind": "spreadsheet-range", "document": document,
                           "sheet": "", "range": cell_range}
    if row_index is not None:
        loc["rowIndex"] = row_index
    return loc


__all__ = ["CsvParser", "CLAIMS"]
