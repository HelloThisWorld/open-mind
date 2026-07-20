"""Every format's mandatory parser cases, plus byte-for-byte determinism.

One suite per the Phase 3 spec's per-format checklist. The theme running through
all of them: the parser must report what the document ACTUALLY contains — never
invent structure it could not see, never present a synthesized rendering as
verbatim, and never stay silent about what it dropped.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from pathlib import Path  # noqa: E402

from openmind.documents import parse_bytes  # noqa: E402
from openmind.documents.models import DocumentLimits  # noqa: E402
from openmind.domain.types import (ContentMode, DocumentBlockType,  # noqa: E402
                                   DocumentParseStatus)

_results = []
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def load(name, **kwargs):
    data = (FIXTURES / name).read_bytes()
    return data, parse_bytes(data, logical_key=f"documents/{name}",
                             filename=name, **kwargs)


def blocks_of(parsed, block_type):
    return [b for b in parsed.blocks if b.block_type == block_type]


def texts(parsed, block_type=None):
    return [b.text for b in parsed.blocks
            if block_type is None or b.block_type == block_type]


# ---------------------------------------------------------------------------
# 1. Markdown
# ---------------------------------------------------------------------------
raw, md = load("sample-requirements.md")
check("markdown: parsed", md.status == DocumentParseStatus.PARSED)
check("markdown: parser identified itself", md.parser_name == "markdown")
check("markdown: title comes from the H1, not the filename",
      md.title == "NameCheck Requirements")
levels = [b.metadata.get("level") for b in blocks_of(md, DocumentBlockType.HEADING)]
check("markdown: heading levels are read from the source", levels[:4] == [1, 2, 2, 3])
deep = [b for b in md.blocks if len(b.heading_path) == 3]
check("markdown: heading hierarchy nests", bool(deep))
check("markdown: the nested block's path is outermost-first",
      deep[0].heading_path[0] == "NameCheck Requirements")
check("markdown: paragraphs are extracted",
      any("screens submitted names" in t
          for t in texts(md, DocumentBlockType.PARAGRAPH)))
check("markdown: list items are extracted",
      any("POST /name-check" in t for t in texts(md, DocumentBlockType.LIST_ITEM)))
code = blocks_of(md, DocumentBlockType.CODE_BLOCK)
check("markdown: fenced code blocks are extracted",
      bool(code) and "NameCheckService" in code[0].text)
check("markdown: the code fence language is recorded",
      code[0].metadata.get("language") == "java")
check("markdown: the code block is verbatim",
      code[0].content_mode == ContentMode.VERBATIM)
rows = blocks_of(md, DocumentBlockType.TABLE_ROW)
check("markdown: pipe table rows are extracted", len(rows) >= 3)
check("markdown: a table row is DERIVED, not verbatim",
      all(r.content_mode == ContentMode.DERIVED for r in rows))
check("markdown: table rows carry their header labels",
      any("Code: NC-100" in r.text for r in rows))
check("markdown: a block quote is extracted",
      any(b.metadata.get("quote") for b in md.blocks))
para = [b for b in md.blocks if b.text.startswith("REQ-NC-017")][0]
lines = raw.decode().split("\n")
check("markdown: the line locator points at the real source line",
      lines[para.locator["startLine"] - 1].startswith("REQ-NC-017"))
check("markdown: the locator uses the portable document key, not a path",
      para.locator["document"] == "documents/sample-requirements.md")
check("markdown: locators carry the heading path",
      para.locator.get("headingPath") == ["NameCheck Requirements",
                                          "4. Functional Requirements",
                                          "4.3 Manual review"])
again = parse_bytes(raw, logical_key="documents/sample-requirements.md",
                    filename="sample-requirements.md")
check("markdown: repeated parsing is byte-for-byte identical",
      md.as_dict() == again.as_dict())

# ---------------------------------------------------------------------------
# 2. HTML
# ---------------------------------------------------------------------------
raw, html = load("sample-design.html")
check("html: parsed", html.status == DocumentParseStatus.PARSED)
check("html: title comes from <title>", html.title == "NameCheck Design Note")
check("html: headings are extracted",
      [b.text for b in blocks_of(html, DocumentBlockType.HEADING)]
      == ["NameCheck Design Note", "Interfaces", "Error codes"])
check("html: paragraphs are extracted",
      any("internal design" in t
          for t in texts(html, DocumentBlockType.PARAGRAPH)))
check("html: list items are extracted",
      len(blocks_of(html, DocumentBlockType.LIST_ITEM)) == 2)
check("html: table rows are extracted",
      len(blocks_of(html, DocumentBlockType.TABLE_ROW)) == 3)
check("html: pre/code is extracted as a code block",
      any("curl -X POST" in t for t in texts(html, DocumentBlockType.CODE_BLOCK)))
all_text = " ".join(b.text for b in html.blocks)
check("html: <script> content is NEVER indexed",
      "must never be indexed" not in all_text)
check("html: <style> content is NEVER indexed", "font-family" not in all_text)
check("html: a hidden element is not indexed",
      "marked as hidden" not in all_text and "Superseded paragraph" not in all_text)
check("html: dropped scripts are RECORDED as unsupported content",
      any(u.kind == "script" for u in html.unsupported_content))
check("html: hidden elements are recorded with a count",
      any(u.kind == "hidden-element" and u.count == 2
          for u in html.unsupported_content))
check("html: <meta>/<link> plumbing is NOT reported as unsupported content",
      not any(u.kind in ("meta", "link", "head")
              for u in html.unsupported_content))
first_p = blocks_of(html, DocumentBlockType.PARAGRAPH)[0]
check("html: the element locator names the tag and its index",
      first_p.locator["element"] == "p" and first_p.locator["elementIndex"] == 1)
check("html: the element locator carries a positional DOM path",
      first_p.locator["domPath"].startswith("html/body"))
check("html: repeated parsing is byte-for-byte identical",
      html.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-design.html",
          filename="sample-design.html").as_dict())

# ---------------------------------------------------------------------------
# 3. DOCX
# ---------------------------------------------------------------------------
raw, docx = load("sample-requirements.docx")
check("docx: parsed", docx.status == DocumentParseStatus.PARSED)
check("docx: core author property is read",
      docx.metadata.author == "OpenMind Fixture Generator")
check("docx: core created property is read", bool(docx.metadata.created))
check("docx: the core `revision` property becomes the version label",
      docx.metadata.version_label == "3")
check("docx: heading STYLES give the hierarchy",
      [b.text for b in blocks_of(docx, DocumentBlockType.HEADING)]
      == ["NameCheck Requirements", "4. Functional Requirements",
          "4.3 NameCheck"])
check("docx: nested headings produce a heading path",
      any(b.heading_path == ["NameCheck Requirements",
                             "4. Functional Requirements", "4.3 NameCheck"]
          for b in docx.blocks))
check("docx: paragraphs are extracted",
      any("REQ-NC-017" in t for t in texts(docx, DocumentBlockType.PARAGRAPH)))
check("docx: list styles are recognized",
      len(blocks_of(docx, DocumentBlockType.LIST_ITEM)) == 2)
docx_rows = blocks_of(docx, DocumentBlockType.TABLE_ROW)
check("docx: table rows are extracted", len(docx_rows) == 4)
check("docx: a table row is DERIVED, not verbatim",
      all(r.content_mode == ContentMode.DERIVED for r in docx_rows))
check("docx: a table row locator names the table, row and cell range",
      docx_rows[1].locator["kind"] == "docx-table-row"
      and docx_rows[1].locator["tableIndex"] == 1
      and docx_rows[1].locator["rowIndex"] == 2
      and ":" in docx_rows[1].locator["cellRange"])
check("docx: the header row is labelled positionally, not with itself",
      docx_rows[0].text.startswith("A: Requirement"))
check("docx: a paragraph locator carries the paragraph index",
      blocks_of(docx, DocumentBlockType.PARAGRAPH)[0].locator["kind"]
      == "docx-paragraph")
check("docx: an embedded image is RECORDED as unsupported content",
      any(u.kind == "embedded-image" and u.count == 1
          for u in docx.unsupported_content))
check("docx: the image is never OCR'd or invented into text",
      not any("Figure 1" in b.text and len(b.text) > 60 for b in docx.blocks))
check("docx: body order interleaves paragraphs and tables correctly",
      [b.block_type for b in docx.blocks].index(DocumentBlockType.TABLE)
      > [b.block_type for b in docx.blocks].index(DocumentBlockType.LIST_ITEM))
check("docx: repeated parsing is byte-for-byte identical",
      docx.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-requirements.docx",
          filename="sample-requirements.docx").as_dict())

# ---------------------------------------------------------------------------
# 4. PDF
# ---------------------------------------------------------------------------
raw, pdf = load("sample-design.pdf")
check("pdf: parsed", pdf.status == DocumentParseStatus.PARSED)
check("pdf: metadata title is read", pdf.title == "NameCheck Design Note")
check("pdf: metadata author is read",
      pdf.metadata.author == "OpenMind Fixture Generator")
check("pdf: the page count is recorded", pdf.metadata.page_count == 2)
pdf_paras = blocks_of(pdf, DocumentBlockType.PARAGRAPH)
check("pdf: page text is extracted",
      any("REQ-NC-017" in b.text for b in pdf_paras))
check("pdf: page two is extracted too",
      any("NC-100" in b.text for b in pdf_paras))
check("pdf: page locators are 1-BASED",
      sorted({b.locator["page"] for b in pdf_paras}) == [1, 2])
check("pdf: the locator kind is pdf-block",
      all(b.locator["kind"] == "pdf-block" for b in pdf_paras))
check("pdf: page containers are not indexed",
      all(not b.indexable for b in blocks_of(pdf, DocumentBlockType.PAGE)))
check("pdf: repeated parsing is byte-for-byte identical",
      pdf.as_dict() == parse_bytes(raw,
                                   logical_key="documents/sample-design.pdf",
                                   filename="sample-design.pdf").as_dict())

_, scanned = load("sample-scanned.pdf")
check("pdf: an image-only PDF is reported needs-ocr",
      scanned.status == DocumentParseStatus.NEEDS_OCR)
check("pdf: an image-only PDF fabricates NO text",
      not [b for b in scanned.blocks if b.indexable])
check("pdf: the needs-ocr result says OCR was not performed",
      any("does NOT perform OCR" in w.message or "no OCR" in w.message.lower()
          for w in scanned.warnings))
check("pdf: an image-only page is recorded as unsupported content",
      any(u.kind == "image-only-page" for u in scanned.unsupported_content))

_, encrypted = load("sample-encrypted.pdf")
check("pdf: an encrypted PDF is reported encrypted",
      encrypted.status == DocumentParseStatus.ENCRYPTED)
check("pdf: an encrypted PDF yields no blocks", encrypted.blocks == [])
check("pdf: the encrypted result explains itself",
      any(w.code == "encrypted" for w in encrypted.warnings))

data = (FIXTURES / "sample-design.pdf").read_bytes()
capped = parse_bytes(data, logical_key="documents/p.pdf", filename="p.pdf",
                     limits=DocumentLimits(pdf_max_pages=1))
check("pdf: a page limit produces `partial`, not silent truncation",
      capped.status == DocumentParseStatus.PARTIAL)
check("pdf: the page-limit warning names PDF_MAX_PAGES",
      any(w.detail.get("limit") == "PDF_MAX_PAGES" for w in capped.warnings))
check("pdf: content extracted before the limit is KEPT",
      any("REQ-NC-017" in b.text for b in capped.blocks))

# ---------------------------------------------------------------------------
# 5. XLSX
# ---------------------------------------------------------------------------
raw, xlsx = load("sample-tests.xlsx")
check("xlsx: parsed", xlsx.status == DocumentParseStatus.PARSED)
check("xlsx: workbook properties are read",
      xlsx.metadata.author == "OpenMind Fixture Generator")
sheets = blocks_of(xlsx, DocumentBlockType.SHEET)
check("xlsx: visible sheets are extracted",
      [b.text for b in sheets] == ["Test Cases", "Totals"])
check("xlsx: a hidden sheet is NOT indexed and IS recorded",
      any(u.kind == "hidden-sheet" for u in xlsx.unsupported_content))
cells = blocks_of(xlsx, DocumentBlockType.CELL_RANGE)
check("xlsx: rows are extracted", len(cells) >= 4)
check("xlsx: a row locator names the sheet and an A1 range",
      cells[0].locator["kind"] == "spreadsheet-range"
      and cells[0].locator["sheet"] == "Test Cases"
      and ":" in cells[0].locator["range"])
check("xlsx: rows are DERIVED, not verbatim",
      all(c.content_mode == ContentMode.DERIVED for c in cells))
check("xlsx: data rows carry their header labels",
      any("Case ID: TC-001" in c.text for c in cells))
formula_cells = [c for c in cells if c.metadata.get("has_formula")]
check("xlsx: a formula is kept AS A FORMULA, never evaluated",
      any("=COUNTA(" in c.text for c in formula_cells))
check("xlsx: a computed VALUE is never substituted for the formula",
      not any(c.text.strip().endswith(": 4") for c in formula_cells))
check("xlsx: the formula's column is recorded",
      any(c.metadata.get("formula_columns") for c in formula_cells))
check("xlsx: merged-cell metadata is captured",
      any(s.metadata.get("merged_count", 0) >= 1 for s in sheets))
check("xlsx: the merged range is named",
      any("A1:E1" in str(s.metadata.get("merged_ranges", "")) for s in sheets))
check("xlsx: a sheet whose only row looks like a header still yields content",
      any(c.locator["sheet"] == "Totals" for c in cells))
bounded = parse_bytes(raw, logical_key="documents/t.xlsx", filename="t.xlsx",
                      limits=DocumentLimits(xlsx_max_rows_per_sheet=2))
check("xlsx: a row limit produces `partial`",
      bounded.status == DocumentParseStatus.PARTIAL)
check("xlsx: the row-limit warning names XLSX_MAX_ROWS_PER_SHEET",
      any(w.detail.get("limit") == "XLSX_MAX_ROWS_PER_SHEET"
          for w in bounded.warnings))
check("xlsx: repeated parsing is byte-for-byte identical",
      xlsx.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-tests.xlsx",
          filename="sample-tests.xlsx").as_dict())

# ---------------------------------------------------------------------------
# 6. CSV
# ---------------------------------------------------------------------------
raw, csv_doc = load("sample-cases.csv")
check("csv: parsed", csv_doc.status == DocumentParseStatus.PARSED)
check("csv: the dialect was detected",
      csv_doc.metadata.extra.get("dialect_detected") is True)
check("csv: the delimiter is recorded",
      csv_doc.metadata.extra.get("delimiter") == ",")
check("csv: the header row is detected",
      csv_doc.metadata.extra.get("has_header") is True)
csv_rows = blocks_of(csv_doc, DocumentBlockType.TABLE_ROW)
check("csv: every data row is extracted", len(csv_rows) == 4)
check("csv: rows carry their header labels",
      csv_rows[0].text.startswith("Case ID: TC-001"))
check("csv: a row locator carries an A1 range and a row index",
      csv_rows[0].locator["range"] == "A2:E2"
      and csv_rows[0].locator["rowIndex"] == 2)
check("csv: rows are DERIVED, not verbatim",
      all(r.content_mode == ContentMode.DERIVED for r in csv_rows))

ragged = parse_bytes(b"a,b,c\n1,2,3\n4,5\n", logical_key="documents/r.csv",
                     filename="r.csv")
check("csv: a ragged row is reported, not padded or truncated",
      any(w.code == "ragged-rows" for w in ragged.warnings))
check("csv: the ragged row's own values are preserved",
      any("4" in b.text and "5" in b.text
          for b in blocks_of(ragged, DocumentBlockType.TABLE_ROW)))
unsniffable = parse_bytes(b"only one column\nvalue\n",
                          logical_key="documents/u.csv", filename="u.csv")
check("csv: an undetectable dialect is REPORTED, not silently guessed",
      any(w.code == "dialect-uncertain" for w in unsniffable.warnings))
capped_csv = parse_bytes(b"a,b\n1,2\n3,4\n5,6\n", logical_key="documents/c.csv",
                         filename="c.csv", limits=DocumentLimits(csv_max_rows=2))
check("csv: a row limit produces `partial`",
      capped_csv.status == DocumentParseStatus.PARTIAL)
check("csv: the row-limit warning names CSV_MAX_ROWS",
      any(w.detail.get("limit") == "CSV_MAX_ROWS" for w in capped_csv.warnings))

# ---------------------------------------------------------------------------
# 7. OpenAPI
# ---------------------------------------------------------------------------
raw, api = load("sample-openapi.yaml")
check("openapi: parsed", api.status == DocumentParseStatus.PARSED)
check("openapi: info.title becomes the document title",
      api.title == "NameCheck API")
check("openapi: info.version becomes the version label",
      api.metadata.version_label == "2.4.0")
ops = blocks_of(api, DocumentBlockType.API_OPERATION)
check("openapi: every path operation becomes a block", len(ops) == 2)
check("openapi: an operation records its method and path",
      any(o.metadata.get("method") == "POST"
          and o.metadata.get("path") == "/name-check" for o in ops))
check("openapi: the JSON Pointer locator escapes '/' as ~1",
      any(o.locator["pointer"] == "/paths/~1name-check/post" for o in ops))
check("openapi: request bodies are described",
      any("requestBody" in o.text for o in ops))
check("openapi: responses are described",
      any("response 202" in o.text for o in ops))
check("openapi: a path-level parameter is inherited by the operation",
      any("caseId" in o.text for o in ops))
schemas = blocks_of(api, DocumentBlockType.SCHEMA_DEFINITION)
check("openapi: component schemas become blocks",
      any(s.locator["pointer"] == "/components/schemas/ScreeningRequest"
          for s in schemas))
check("openapi: an operation summary is DERIVED, not verbatim source",
      all(o.content_mode == ContentMode.DERIVED for o in ops))
not_api = parse_bytes(b"name: something\nvalues:\n  - a\n",
                      logical_key="documents/values.yaml", filename="values.yaml")
check("openapi: a YAML file that is not an OpenAPI description is not claimed",
      not_api.parser_name != "openapi")
check("openapi: repeated parsing is byte-for-byte identical",
      api.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-openapi.yaml",
          filename="sample-openapi.yaml").as_dict())

# ---------------------------------------------------------------------------
# 8. JSON Schema
# ---------------------------------------------------------------------------
raw, schema = load("sample-schema.json")
check("json-schema: parsed", schema.status == DocumentParseStatus.PARSED)
check("json-schema: the title is read", schema.title == "ScreeningCase")
check("json-schema: the dialect is recorded",
      "json-schema.org" in schema.metadata.extra.get("dialect", ""))
defs = [b for b in schema.blocks if b.locator.get("pointer", "").startswith("/$defs/")]
check("json-schema: $defs entries become blocks", len(defs) == 2)
props = [b for b in schema.blocks
         if b.locator.get("pointer", "").startswith("/properties/")]
check("json-schema: properties become blocks", len(props) == 5)
check("json-schema: the root schema is a block",
      any(b.metadata.get("root") for b in schema.blocks))
check("json-schema: constraints are copied as declared",
      any("pattern" in b.text and "NC-" in b.text for b in props))
check("json-schema: an enum constraint is copied",
      any("enum" in b.text and "cleared" in b.text for b in defs))
check("json-schema: an internal $ref is resolved",
      any("resolved type" in b.text for b in props))
check("json-schema: a remote $ref is recorded as NOT fetched",
      any(u.kind == "external-reference" for u in schema.unsupported_content))
check("json-schema: the remote $ref is left unresolved",
      any("https://example.invalid" in b.text and "resolved type" not in b.text
          for b in props))
check("json-schema: repeated parsing is byte-for-byte identical",
      schema.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-schema.json",
          filename="sample-schema.json").as_dict())

# ---------------------------------------------------------------------------
# 9. SQL DDL
# ---------------------------------------------------------------------------
raw, sql = load("sample-schema.sql")
check("sql: parsed", sql.status == DocumentParseStatus.PARSED)
objects = blocks_of(sql, DocumentBlockType.SQL_OBJECT)
tables = [b for b in objects if b.metadata.get("object_type") == "table"]
check("sql: CREATE TABLE becomes a table object",
      {b.metadata["name"] for b in tables} == {"screening_case",
                                               "watch_list_entry"})
check("sql: a table object block is VERBATIM source",
      all(b.content_mode == ContentMode.VERBATIM for b in tables))
columns = [b for b in objects if b.metadata.get("object_type") == "column"]
check("sql: columns become child blocks",
      {b.metadata["name"] for b in columns} >= {"case_id", "submitted_name",
                                                "status"})
check("sql: constraints become child blocks",
      any(b.metadata.get("object_type") == "constraint" for b in objects))
check("sql: a view is recognized",
      any(b.metadata.get("object_type") == "view" for b in objects))
check("sql: an index statement is recognized",
      any("idx_screening_case_status" in b.text for b in sql.blocks))
check("sql: object line ranges are real source lines",
      all(b.locator["startLine"] >= 1
          and b.locator["endLine"] >= b.locator["startLine"] for b in tables))
fallback = blocks_of(sql, DocumentBlockType.CODE_BLOCK)
check("sql: unsupported vendor syntax falls back to bounded verbatim text",
      any("PACKAGE BODY" in b.text for b in fallback))
check("sql: the fallback keeps an exact source line range",
      all(b.locator["startLine"] >= 1 for b in fallback))
check("sql: the fallback is REPORTED, not silent",
      any(w.code == "unsupported-syntax" for w in sql.warnings))
check("sql: the unstructured statements are recorded with a count",
      any(u.kind == "unstructured-statement" and u.count >= 1
          for u in sql.unsupported_content))
check("sql: repeated parsing is byte-for-byte identical",
      sql.as_dict() == parse_bytes(
          raw, logical_key="documents/sample-schema.sql",
          filename="sample-schema.sql").as_dict())

# ---------------------------------------------------------------------------
# 10. Plain text / RST / AsciiDoc
# ---------------------------------------------------------------------------
text_src = (b"NameCheck Operations\n====================\n\n"
            b"Intro paragraph.\n\nEscalation\n----------\n\n"
            b"Contact the on-call engineer.\n\n- page the primary\n"
            b"- then the secondary\n\n    systemctl restart namecheck\n\n"
            b".. note:: a directive\n")
plain = parse_bytes(text_src, logical_key="documents/guide.rst",
                    filename="guide.rst")
check("text: parsed", plain.status == DocumentParseStatus.PARSED)
check("text: an underlined heading is recognized",
      [b.text for b in blocks_of(plain, DocumentBlockType.HEADING)]
      == ["NameCheck Operations", "Escalation"])
check("text: underline characters set the heading level",
      [b.metadata["level"] for b in blocks_of(plain, DocumentBlockType.HEADING)]
      == [1, 2])
check("text: blank-line paragraphs are extracted",
      any("Intro paragraph" in t for t in texts(plain, DocumentBlockType.PARAGRAPH)))
check("text: list items are extracted",
      len(blocks_of(plain, DocumentBlockType.LIST_ITEM)) == 2)
check("text: an indented block is a code block",
      any("systemctl" in t for t in texts(plain, DocumentBlockType.CODE_BLOCK)))
check("text: RST directives are RECORDED as unsupported, not expanded",
      any(u.kind == "markup-directive" for u in plain.unsupported_content))
check("text: line ranges are preserved",
      all(b.locator["startLine"] >= 1 for b in plain.blocks[1:]))
numbered = parse_bytes(b"Guide\n=====\n\n1. Overview\n\nBody.\n\nSteps:\n\n"
                       b"1. do this\n2. do that\n3. do the other\n",
                       logical_key="documents/n.txt", filename="n.txt")
check("text: a standalone numbered line is a section heading",
      any(b.text == "1. Overview"
          for b in blocks_of(numbered, DocumentBlockType.HEADING)))
check("text: consecutive numbered lines stay LIST ITEMS",
      len(blocks_of(numbered, DocumentBlockType.LIST_ITEM)) == 3)

# ---------------------------------------------------------------------------
# 11. Cross-format invariants
# ---------------------------------------------------------------------------
for name in sorted(p.name for p in FIXTURES.glob("sample-*")):
    _, doc = load(name)
    if not doc.blocks:
        continue
    check(f"{name}: every locator uses the portable document key",
          all(b.locator.get("document") == f"documents/{name}"
              for b in doc.blocks if b.locator))
    check(f"{name}: no locator contains an absolute path",
          not any(":" in str(b.locator.get("document", "")[:3])
                  or str(b.locator.get("document", "")).startswith("/")
                  for b in doc.blocks))
    check(f"{name}: block keys are unique",
          len({b.block_key for b in doc.blocks}) == len(doc.blocks))
    check(f"{name}: no indexable block is empty",
          all(b.text.strip() for b in doc.blocks if b.indexable))

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
