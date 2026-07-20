# Document parser fixtures

Small, neutral, **invented** documents for the Phase 3 document-ingestion tests.

Everything here describes a fictional "NameCheck" screening service. No content
is copied from any real, proprietary or employer document, and no identifier,
endpoint, error code or requirement id in these files refers to a real system.

## Text fixtures (checked in as source)

These are ordinary text and are edited directly — a readable diff is worth more
than uniformity with the generated ones.

| File | Exercises |
| --- | --- |
| `sample-requirements.md` | heading hierarchy, paragraphs, list items, a fenced code block, a pipe table, a block quote, exact line locators |
| `sample-design.html` | `<title>`, headings, paragraphs, lists, a table, `pre`/`code`; a `<script>`, a `<style>` and two hidden paragraphs that must **not** be indexed |
| `sample-cases.csv` | header detection, row locators, dialect sniffing |
| `sample-openapi.yaml` | `info.title`/`info.version`, path operations, a path-level parameter, request body, responses, component schemas, JSON-Pointer locators |
| `sample-schema.json` | root schema, `$defs`, properties, constraints, an internal `#/$defs` `$ref` that IS resolved, and a remote `$ref` that must **never** be fetched |
| `sample-schema.sql` | tables, columns, constraints, an index, a view, plus a PL/SQL package body that `sqlglot` cannot structure and that must fall back to bounded verbatim text with an exact line range |

## Binary fixtures (generated)

Run:

```bash
python scripts/build_document_fixtures.py
```

| File | Built with | Exercises |
| --- | --- | --- |
| `sample-requirements.docx` | `python-docx` | core properties (including `revision` → version label), heading styles and hierarchy, list styles, a table, and one embedded image recorded as unsupported content |
| `sample-tests.xlsx` | `openpyxl` | workbook properties, two visible sheets and one hidden one, a merged banner cell, bounded used ranges, and two formulas that must be stored **as formulas** and never evaluated |
| `sample-design.pdf` | hand-written PDF bytes | metadata, two pages of extractable text, 1-based page locators |
| `sample-scanned.pdf` | hand-written PDF bytes | a page with a drawn rectangle and **no text operators** → must be reported `needs-ocr`, with no fabricated text |
| `sample-encrypted.pdf` | hand-written PDF bytes | an `/Encrypt` trailer that does not open with the empty password → must be reported `encrypted` |

### Why the PDFs are hand-written

Writing the PDF bytes directly keeps a PDF-*writing* dependency out of the
repository (OpenMind only ever *reads* PDFs), keeps each file well under 2 KB,
and puts the bytes fully under our control — which is what makes "this page has
no extractable text" and "this file is encrypted" reliable test inputs instead
of a library's changing idea of them.

### Why regeneration is byte-identical

An unchanged document must produce **no new Revision**. A fixture whose bytes
drifted between runs would change its content hash and make that guarantee
untestable. So every timestamp is pinned to a fixed epoch, and the OOXML
packages are rewritten with fixed member order, fixed member timestamps and
fixed compression (`normalize_zip` in the generator) — `openpyxl` in particular
overwrites `dcterms:modified` with the wall clock during `save()`, so that field
is rewritten afterwards.

Regenerating and committing should therefore produce an empty diff. If it does
not, the generator changed and the change is real.
