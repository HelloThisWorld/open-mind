# OpenMind v2 — Phase 3: Enterprise Document Ingestion and Appendable Knowledge

Status: implemented on branch `feat/v2-phase3-document-ingestion`.
Runtime version introduced by this phase: **`1.3.0-dev`**.
Artifact export contract: **unchanged** — `.openmind` schema stays `1.1.0`.

Phase 3 makes enterprise documents first-class OpenMind Assets. It adds a
deterministic, model-free document-ingestion plane on top of the Phase 2
Asset/Revision/Segment/Evidence foundation, and it deliberately implements
**none** of the semantic layer above it — no Requirement extraction, no Business
Rules, no Design Decisions, no Claim/Relation tables, no Knowledge-Graph edges,
no OCR, no cloud model calls. See [§16 Deferred work](#16-deferred-phase-4-work).

---

## 1. Baseline: what already worked

Phase 3 started from a clean working tree at `3a5f6e9` (branch `main`), with the
whole core acceptance suite green. Recorded before any change was made
(`python scripts/run_acceptance.py --json`):

```
ok=true — 28 passed, 0 failed, 0 skipped
```

| Script | Result | Script | Result |
| --- | --- | --- | --- |
| verify_migrations | 64 passed | verify_facets | 22/22 |
| verify_content_store | 23 passed | verify_guide | 19/19 |
| verify_structure | 14 passed | verify | 17/17 |
| verify_glossary | 20 passed | verify_fixes | 7/7 |
| verify_router | 23 passed | verify_fixes2 | 7/7 |
| verify_diagrams | 7 passed | verify_resources | 15 passed |
| verify_grounding | 8 passed | verify_ask | 15/15 |
| verify_artifacts | 31 passed | verify_ask2 | 29/29 |
| verify_runtime | 31 passed | verify_async_delete | 6/6 |
| verify_services | 97 passed | verify_delete_race | 26/26 |
| verify_cli | 112 passed | verify_source_link | 25 passed |
| verify_adapters | 88 passed | verify_modelserver | 10/10 |
| verify_asset_model | 75 passed | verify_templates | 21/21 |
| verify_asset_cli | 34 passed | verify_asset_adapters | 31 passed |

Schema head before this phase: **version 3** (`v0001_baseline`,
`v0002_paths_sidecar`, `v0003_asset_model`).
MCP tool set before this phase: **13** — the nine core tools
(`search`, `route`, `dispatch`, `get_glossary`, `find_similar_cases`,
`save_case`, `get_doc`, `propose_fix`, `apply_fix`) plus the four Phase 2 Asset
tools (`list_assets`, `get_asset`, `get_asset_revisions`, `get_evidence`).

---

## 2. Phase 2 Asset model (what this phase builds on)

```
Workspace (= the existing project row; id p_*)
└── Asset            one logical engineering object, keyed (workspace_id, logical_key)
    └── AssetRevision   an immutable observation of that Asset's bytes
        ├── Segment       a stable structural unit inside one revision
        └── Evidence      a source-locatable citation for a segment
```

Facts Phase 3 relies on and does not change:

* `assets.logical_key` is workspace-relative and portable; no absolute machine
  path is ever stored in the database (the source root lives in the
  machine-local sidecar, `openmind/machine.py`).
* `asset_revisions.content_blob_hash` is the SHA-256 of the revision's **exact
  bytes**, stored in the immutable content store
  (`data/<workspace>/objects/<xx>/<sha256>`).
* `db.commit_revision` is the single transactional writer: an Asset's
  `current_revision_id` is repointed **last**, so it can never name a revision
  whose segments and evidence are not yet committed.
* Unchanged content creates **no** new revision (idempotent, revert-safe), and a
  removed-then-reappearing Asset is reactivated rather than duplicated.
* Chroma is a *retrieval projection*, never the source of truth.

Phase 3 adds a parallel, additive plane. It does not modify `v0003`, does not
change `commit_revision`'s existing behaviour, and does not move code Segments.

---

## 3. Target document-ingestion architecture

```
Local files under registered roots        Manually attached document
              │                                       │
              └──────────────┬────────────────────────┘
                             ▼
                    Document Intake Service        (openmind/documents/intake.py)
                             │  read bytes → SHA-256 → immutable staged blob
                             ▼
                     Parser Registry              (openmind/documents/registry.py)
                             │  probe → exactly one parser (or a typed failure)
                             ▼
                    Normalized ParsedDocument     (openmind/documents/models.py)
                       metadata · blocks · warnings · coverage
                             │
                             ▼
        Asset / Revision / Segment / Evidence + document_parses
                             │
              ┌──────────────┴───────────────┐
              ▼                              ▼
   documents_<workspace> collection   deterministic candidate association
              │                              │
              └──────────────┬───────────────┘
                             ▼
                  CLI · REST · MCP · Claude Code
```

Raw bytes and normalized content stay on the machine. No project document
content leaves the machine in this phase; no parser dependency performs network
I/O or telemetry.

---

## 4. Parser SPI

`openmind/documents/` holds the whole plane:

```
openmind/documents/
├── __init__.py          lazy re-exports; importing it pulls in NO parser dependency
├── models.py            ParsedDocument, DocumentBlock, warnings, probe, context
├── security.py          limits, ZIP package safety, safe XML, bounded decoding
├── registry.py          register / list_parsers / select / parse  (lazy imports)
├── probe.py             magic + package-structure detection (never extension alone)
├── text_parser.py       plain text, RST, AsciiDoc
├── markdown_parser.py   Markdown / CommonMark subset
├── html_parser.py       HTML (stdlib HTMLParser, scripts and styles dropped)
├── csv_parser.py        CSV / TSV (stdlib csv, bounded sniffing)
├── docx_parser.py       DOCX (python-docx)
├── pdf_parser.py        PDF (pypdf)
├── spreadsheet_parser.py XLSX (openpyxl)
├── openapi_parser.py    OpenAPI 2/3 (JSON + YAML)
├── json_schema_parser.py JSON Schema
├── sql_parser.py        SQL DDL (sqlglot, honest text fallback)
├── pipeline.py          parse → blobs → segments → transactional commit → vectors
└── candidates.py        deterministic candidate association
```

### 4.1 Contract

```python
class DocumentParser(Protocol):
    name: str
    version: str
    def supports(self, probe: DocumentProbe) -> bool: ...
    def parse(self, content: bytes, context: DocumentParseContext) -> ParsedDocument: ...
```

`DocumentProbe` carries `filename`, `extension`, `declared_media_type`,
`detected_media_type`, `magic` (signature facts: `zip`, `pdf`, `ole2`, `utf8`,
`bom`, `zip_members` for OOXML package structure), `size`, and a bounded `head`
sample of the bytes. **A parser is never selected from the extension alone.**
For ZIP-based OOXML formats the probe verifies package structure
(`word/document.xml` for DOCX, `xl/workbook.xml` for XLSX) before a parser may
claim the file.

`DocumentParseContext` carries the portable `logical_key`, the original
`filename`, the workspace id, the effective `DocumentLimits`, and free-form
`parser_options`.

### 4.2 Registry rules

1. **Deterministic order** — parsers are registered with an explicit integer
   priority; ties break on parser name. `list_parsers()` returns that order.
2. **Exactly one parser is selected.** `select(probe)` returns the single
   highest-priority parser whose `supports()` is true.
3. **Ambiguity fails clearly.** Two parsers at the *same* priority both claiming
   a probe raise `AmbiguousParser` rather than silently picking one.
4. **Missing optional dependency** → `ParsedDocument(status="unsupported")` with
   `reason="dependency_unavailable"` and the missing distribution named. Only
   that format fails.
5. **Unsupported format** → `status="unsupported"`, never an empty successful
   parse.
6. **Parser imports are lazy** — `registry.select()` imports a parser module
   only when its probe matches, and the third-party library only inside
   `parse()`. Importing `openmind.documents` imports no parser dependency.
7. **Artifact export stays dependency-free**: `openmind export` never imports
   `openmind.documents`.

---

## 5. Normalized document model

```
ParsedDocument
├── parser_name, parser_version, schema_version ("1.0")
├── status         parsed | partial | needs-ocr | encrypted | unsupported | failed
├── title, media_type
├── metadata       DocumentMetadata (author/created/modified/producer/version_label/extra)
├── blocks         [DocumentBlock]
├── warnings       [DocumentParseWarning]  (code, message, locator?)
├── unsupported_content [UnsupportedContent] (kind, count, detail)
└── coverage       {"blocks": n, "indexable": n, "truncated": bool, ...}
```

```
DocumentBlock
├── block_key      stable, deterministic, unique within the document
├── block_type     document | section | heading | paragraph | list-item | code-block
│                  | table | table-row | sheet | cell-range | page | api-operation
│                  | schema-definition | sql-object
├── ordinal        dense, 0-based, document order
├── parent_key     the containing block's key ("" for the root)
├── heading_path   [str] ancestor headings, outermost first
├── text           the represented text
├── content_mode   verbatim | derived
├── locator        a portable document locator (see §6)
├── metadata       {} format-specific facts
└── indexable      whether this block is embedded into the document collection
```

**Content mode.** `verbatim` is used only when `text` is an exact textual
representation of the source. A synthesized table rendering, a spreadsheet row
serialized as `header=value` pairs, an OpenAPI operation summary and the
document root block are `derived`. Markdown/text/HTML paragraphs, DOCX
paragraph runs, PDF page text and code blocks are `verbatim`.

**Indexable.** Structural containers (the `document` root, an empty `section`,
a `table` wrapper whose rows carry the content) are stored as Segments but not
embedded. Only content-bearing leaves are indexed, so retrieval is not diluted
by empty scaffolding. Every stored block still becomes a Segment with Evidence.

---

## 6. Document source locators

The Phase 2 `source-range` locator remains valid for code and is untouched.
Document locators are additive and always carry a **portable logical document
key**, never an absolute path. Page and line numbers presented to users are
1-based.

| Format | `kind` | Fields |
| --- | --- | --- |
| Markdown / text / RST / AsciiDoc | `text-range` | `document`, `startLine`, `endLine`, `headingPath` |
| HTML | `html-element` | `document`, `element`, `elementIndex`, `domPath`, `headingPath` |
| DOCX paragraph | `docx-paragraph` | `document`, `paragraphIndex`, `headingPath` |
| DOCX table row | `docx-table-row` | `document`, `tableIndex`, `rowIndex`, `cellRange` |
| PDF | `pdf-block` | `document`, `page`, `blockIndex` |
| XLSX | `spreadsheet-range` | `document`, `sheet`, `range` |
| CSV | `spreadsheet-range` | `document`, `sheet` (`""`), `range`, `rowIndex` |
| OpenAPI / JSON Schema | `json-pointer` | `document`, `pointer` |
| SQL DDL | `text-range` | `document`, `startLine`, `endLine`, `symbol` |

---

## 7. Segment-content snapshots

Phase 2 Evidence for **code** is reconstructed by slicing `[startLine, endLine]`
out of the revision blob. That is safe because the mapping from bytes to lines
is fixed forever.

For a **document** it is not. Re-deriving a DOCX paragraph or a PDF page block
requires re-running a parser, and a future parser version may legitimately
produce different block boundaries. Reconstructing historical Evidence that way
would silently rewrite history.

So Phase 3 adds an optional **segment content blob**:

```sql
ALTER TABLE segments ADD COLUMN content_blob_hash TEXT NOT NULL DEFAULT '';
```

For a document segment the pipeline

1. stores the block's exact represented text (UTF-8) in the content store,
2. records that blob's SHA-256 in `segments.content_blob_hash`,
3. sets `segments.content_hash` to the SHA-256 of that same exact text,
4. sets the Evidence `content_hash` to the same value.

Historical document Evidence is then recovered **from the block blob**, with no
parser rerun. Existing code segments keep `content_blob_hash = ''` and continue
to resolve through the line-range path; Phase 2 rows are **not** backfilled.

---

## 8. Document parse records

```sql
CREATE TABLE document_parses (
    revision_id TEXT PRIMARY KEY,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    unsupported_json TEXT NOT NULL DEFAULT '[]',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    structure_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(revision_id) REFERENCES asset_revisions(id) ON DELETE CASCADE
)
```

It is a **derived parse projection over an immutable Revision**. In Phase 3 a
Revision gets exactly one parse result; the parser name and version are
recorded; the output is deterministic (`structure_hash` is the SHA-256 over the
ordered `(block_key, block_type, ordinal, parent_key, content_hash)` tuples, so
a re-parse that changes structure is detectable). Old Revisions are never
automatically re-parsed, and a historical parse is never silently replaced by a
different parser version. A future analysis-generation model may allow that.

**The presence of a `document_parses` row for an Asset's current Revision is the
definition of "this Asset is a document."** That is a recorded fact, not an
inference.

---

## 9. Document vector projection

Documents get their **own collection per workspace** so the existing `search`
contract cannot change:

```
code_<workspace>        unchanged — code/config chunks (existing `search`)
cases_<workspace>       unchanged
documents_<workspace>   NEW — document block chunks
```

```sql
CREATE TABLE document_index (
    workspace_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, asset_id),
    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
)
```

Each indexed chunk carries: `workspace_id`, `asset_id`, `revision_id`,
`segment_id`, `evidence_id`, `logical_key`, `title`, `asset_type`, `block_type`,
`heading_path`, `parser_name`, `page`, `sheet`, `json_pointer`, `content_hash`.
Only relevant fields are populated.

**Active-revision behaviour.** Document search returns the current active
Revision. When a new Revision becomes current the pipeline removes the previous
active chunks, adds the new ones and updates `document_index`; every historical
Segment and Evidence row is preserved. Historical search may be added later.

**Lifecycle.** `vectorstore._pid_of` recognizes the `documents_` prefix, so:
workspace terminate drops the document collection, workspace delete drops it,
and the startup orphan sweep classifies it correctly (a *deleting* workspace is
still "known" and therefore not an orphan). Drains stay batched, interruptible
and protected by the existing in-flight guard — the delete-race fixes are not
touched.

Chunk ids are stable and content-derived
(`d_` + `sha1(asset_id|revision_id|block_key)[:18]`), so a retried upsert after
a mid-commit failure writes the same ids and is idempotent.

---

## 10. Format support

| Format | Parser | Support | Not supported in Phase 3 | Security limits |
| --- | --- | --- | --- | --- |
| Markdown | `markdown` | headings + hierarchy, paragraphs, lists, fenced code, block quotes, pipe tables, line locators | reference/footnote resolution, inline HTML rendering, MDX | `DOCUMENT_MAX_BYTES`, `DOCUMENT_MAX_BLOCKS`, `DOCUMENT_MAX_BLOCK_CHARS` |
| Plain text / RST / AsciiDoc | `text` | line ranges, blank-line paragraphs, underline/`=`/`#` headings, list items, indented code blocks | RST/AsciiDoc directives, includes, substitutions (reported as unsupported content) | as above |
| HTML | `html` | `<title>`, headings, paragraphs, list items, tables, `pre`/`code`; scripts, styles, comments and `hidden` content dropped | JavaScript execution, external resource fetch, CSS-computed visibility | as above; no network |
| DOCX | `docx` | core properties, paragraphs, heading styles + hierarchy, list items, tables/rows/cells, code-like styles, section order | macros, embedded images (recorded as unsupported content), tracked changes, headers/footers | ZIP package caps, safe XML, `DOCX_MAX_PARAGRAPHS`, `DOCX_MAX_TABLES` |
| PDF (text) | `pdf` | metadata, per-page text, block ordering where available, 1-based page locators | OCR, table recovery, form fields, annotations | `PDF_MAX_PAGES`, `DOCUMENT_MAX_BYTES` |
| XLSX | `xlsx` | workbook props, visible sheets, bounded used ranges, row/cell values, formulas **as text**, merged-cell metadata, header detection | formula evaluation, external links, macros (`.xlsm` unsupported), charts | ZIP package caps, `XLSX_MAX_SHEETS`, `XLSX_MAX_ROWS_PER_SHEET`, `XLSX_MAX_CELLS` |
| CSV / TSV | `csv` | encoding fallback, bounded dialect sniffing, header row, row locators | nested/multi-table files, type inference | `CSV_MAX_ROWS`, `DOCUMENT_MAX_BLOCK_CHARS` |
| OpenAPI (JSON/YAML) | `openapi` | `info.title`/`info.version`, paths + operations, parameters, request bodies, responses, component schemas, JSON-Pointer locators | remote `$ref` resolution, business-meaning inference, validation | safe YAML (`yaml.safe_load`), `DOCUMENT_MAX_BLOCKS` |
| JSON Schema | `json-schema` | root, `definitions`/`$defs`, properties, constraints, internal `$ref` | remote `$ref` fetch, schema validation | as above; **no network** |
| SQL DDL | `sql` | tables, views, columns, indexes, constraints, sequences, line ranges; honest bounded-text fallback on unsupported dialect syntax | dialect-perfect parsing, execution | `DOCUMENT_MAX_BLOCKS` |

**Truncation is never silent.** When a limit is reached the parser keeps the
already-extracted content, sets `status="partial"`, appends a warning naming the
exact limit and the observed value, and reports `coverage`.

**Honest PDF statuses.** An image-only page produces no fabricated text. A PDF
with no meaningful extractable text becomes `needs-ocr`; an encrypted PDF that
cannot be opened with the empty password becomes `encrypted`. **No OCR is
performed in Phase 3** and no result ever claims OCR was performed.

---

## 11. Security and resource controls

Document parsing handles untrusted files, so the controls are explicit and
tested (`tests/verify_document_security.py`).

**ZIP package safety** (`security.inspect_zip`) — for DOCX/XLSX:
member-count cap, total-uncompressed-size cap, per-member size cap, path
traversal rejection (`..`, absolute, drive-letter, backslash members),
compression-ratio (zip-bomb) detection. Members are read **in memory from the
already-validated archive**; nothing is ever extracted to a filesystem path, and
no embedded content is executed.

**XML safety** — `defusedxml` is installed as a dependency and
`defusedxml.defuse_stdlib()` is applied before any OOXML parse, so external
entities, external DTDs and entity-expansion bombs are refused by the underlying
stdlib parsers that `python-docx`/`openpyxl` use. When `defusedxml` is absent the
DOCX/XLSX parsers report `dependency_unavailable` rather than parsing unsafely.

**Configurable limits** (`openmind/config.py`, each overridable by an
`OPENMIND_*` environment variable):

```
DOCUMENT_MAX_BYTES          25_000_000
PDF_MAX_PAGES               2_000
DOCX_MAX_PARAGRAPHS         50_000
DOCX_MAX_TABLES             2_000
XLSX_MAX_SHEETS             100
XLSX_MAX_ROWS_PER_SHEET     20_000
XLSX_MAX_CELLS              500_000
CSV_MAX_ROWS                50_000
DOCUMENT_MAX_BLOCKS         20_000
DOCUMENT_MAX_BLOCK_CHARS    20_000
ZIP_MAX_MEMBERS             2_000
ZIP_MAX_TOTAL_BYTES         400_000_000
ZIP_MAX_MEMBER_BYTES        100_000_000
ZIP_MAX_RATIO               200
```

**Parsing isolation.** `registry.parse()` catches every parser exception and
returns `status="failed"` with the exception type and message. A parser failure
fails that document's job step, never the worker process, and never leaves a
partial current Revision.

---

## 12. Append-document workflow and identity rules

`DocumentService` (`runtime.documents`, `ServiceContainer.documents`) exposes
`plan_import`, `add_document`, `get_document`, `list_documents`, `get_outline`,
`search`, `search_knowledge`, `find_related_candidates`.

```
Read local bytes → SHA-256 → immutable staged blob (content store)
  → persist a safe import payload (jobs.payload_json — NO absolute path)
  → enqueue a `document_ingest` job
  → parse from the staged blob
  → Asset / Revision / Segments / Evidence + document_parses
  → document vector projection
  → deterministic candidate association
  → import report
```

The job payload may contain only `staged_blob_hash`, `original_filename`,
`requested_asset_id`, `requested_logical_key`, `import_mode`, `version_label`,
`parser_options`. **The absolute origin path never enters the portable
database.**

### 12.1 Identity

| Origin | Logical key |
| --- | --- |
| Under a registered source root | `workspace_id` + workspace-relative path (unchanged Phase 2 rule) |
| Manually attached | `documents/<normalized-filename>` |
| `--logical-key K` | `K` (normalized) |
| `--new-asset` | `documents/<stem>--<content-hash-prefix>.<ext>` — readable and deterministic, never opaque-random |

Two different documents are **never** silently merged because their filenames
match.

### 12.2 Import decisions

| Condition | `status` | Effect |
| --- | --- | --- |
| Content hash already exists as a document Revision in the workspace | `duplicate` | Returns the existing Asset + Revision. **No job, no Revision, no vector duplicate.** |
| `--asset A` supplied, content differs | `revision` | Next Revision of `A` (validated: belongs to the workspace, is document-compatible) |
| `--asset A` supplied, content unchanged | `duplicate` | As above |
| `--logical-key K` | `new_asset` / `revision` | Find or create that Asset |
| Default key exists with **different** content and no explicit choice | `possible_revision` | **Nothing is written.** Returns the candidate Asset, its current Revision, both content hashes and the exact retry commands. |
| `--new-asset` | `new_asset` | Distinct deterministic key |
| Nothing matches | `new_asset` | Create |

### 12.3 Version label

Only explicit deterministic metadata is used: OpenAPI `info.version`, the DOCX
core `revision` property, a clearly-defined PDF metadata version field, or a
caller-supplied `--version-label` (which always wins). Versions are **never**
inferred from prose or filename patterns. `revision.status` stays `unknown` —
approval authority is not inferred.

### 12.4 Workspace discovery vs. code discovery

`config` now separates the policies:

```
CODE_INDEX_EXTENSIONS        = INDEX_EXTENSIONS   (unchanged: .java .ts .yaml .sql .html …)
TEXT_DOCUMENT_EXTENSIONS     = .md .markdown .rst .adoc .asciidoc .txt
BINARY_DOCUMENT_EXTENSIONS   = .docx .pdf .xlsx
STRUCTURED_DOCUMENT_EXTENSIONS = .csv .tsv
DOCUMENT_DISCOVERY_EXTENSIONS = (the three above) − CODE_INDEX_EXTENSIONS
```

The subtraction is load-bearing: a file already owned by the **code** pipeline
(`.html`, `.yaml`, `.json`, `.sql`, …) is never also discovered as a workspace
document, so nothing is ingested twice. Those formats are still fully parseable
**on demand** through `document add`, which is how an OpenAPI YAML or a schema
SQL file becomes a document Asset. And no document extension is added to
`INDEX_EXTENSIONS`, so a `.pdf` can never reach `walker.read_text`.

A **full** ingest discovers code assets, discovers document assets, runs both
pipelines, and marks removed assets in both. A **filtered** code ingest never
prunes document Assets, and a filtered document ingest never rebuilds or prunes
code Assets.

**Removal.** A workspace document that disappears has its active document chunks
removed and its Asset marked `removed`; every Revision, Segment, Evidence row
and content blob is preserved. A **manually attached** snapshot never becomes
`removed` because its external origin path vanished — the snapshot is the
canonical source (`source_kind="attachment"`).

---

## 13. Evidence retrieval

`AssetService.get_evidence` keeps its exact Phase 2 behaviour for code Evidence
(source-range slice out of the revision blob + live-file comparison). Document
Evidence is resolved through an additive locator-resolver path:

1. If the Segment has a `content_blob_hash`, the exact block text is read from
   that blob and verified against the Evidence `content_hash` → `snapshot:
   available` (or `corrupt` on mismatch). **No parser is rerun.**
2. `current_source.status` is:
   * `not-applicable` for an attachment (its origin path is deliberately not
     tracked, so `missing` would be a false claim);
   * `matches` / `changed` / `missing` for a workspace document file, decided by
     comparing the **whole current document's** content hash against the
     Revision's — block-level re-resolution is not deterministic across parser
     versions, so document-level comparison is the honest answer.

The response adds `parser`, `revision`, `snapshot`, `current_source` and
`truncated`; bounded output truncates honestly.

---

## 14. Retrieval

### 14.1 Document search (`openmind/document_rag.py`)

`vector retrieval + lexical retrieval + exact identifier matching`, fused with
**RRF**, bounded. An exact Requirement ID, API path, error code or identifier is
matched on token boundaries and is **promoted above** any merely
embedding-similar result — the same guarantee the code RAG already gives.

Each hit carries `asset_id`, `revision_id`, `segment_id`, `evidence_id`,
`title`, `logical_key`, `block_type`, `heading_path`, `locator`, `excerpt`,
`score`, `retrieval_sources`. Filters: `asset_type`, `parser`, `block_type`,
`logical_key`. Removed Assets are excluded by default.

### 14.2 Combined knowledge search

`search_knowledge` returns code and documents **separately**:

```json
{"query": "...",
 "code": {"hits": []},
 "documents": {"hits": []},
 "grounding": {"codeCount": 0, "documentCount": 0}}
```

It never claims a document hit *implements* or *refines* a code hit. This is
retrieval, not relationship inference. The existing MCP `search` tool stays
code-oriented and byte-for-byte unchanged.

### 14.3 Deterministic candidate association

`find_related_candidates` compares an imported document against existing
knowledge using deterministic signals first:

| # | Signal | Candidate type | Confidence |
| --- | --- | --- | --- |
| 1 | exact workspace file path | `mentions-file` | high |
| 2 | exact code symbol (Segment `symbol`) | `mentions-symbol` | high |
| 3 | Requirement-like identifier (`REQ-NC-017`) | `mentions-document` / `similar-content` | high |
| 4 | change-request / ticket identifier (`ABC-1234`) | `mentions-document` | high |
| 5 | API path + HTTP method | `mentions-api` | medium |
| 6 | error code | `mentions-configuration` | medium |
| 7 | configuration key (dotted) | `mentions-configuration` | medium |
| 8 | database object | `mentions-database-object` | medium |
| 9 | message / topic name | `mentions-configuration` | medium |
| 10 | exact glossary term | `mentions-document` | high |
| 11 | semantic retrieval fallback | `similar-content` / `possibly-related` | low |

Every candidate carries `candidate_type`, `confidence`, `reason`,
`document_evidence`, `target`, `target_evidence`, `retrieval_method` and
`status: "candidate"`. `implements`, `refines`, `verifies` and `contradicts` are
**never** returned — they require the semantic verification deferred to Phase 4.
**Candidates are computed on demand and are never persisted as canonical
Relations.**

---

## 15. Compatibility boundaries

| Surface | Guarantee |
| --- | --- |
| Runtime | CLI, MCP, FastAPI and tests keep using the same `OpenMindRuntime`; `documents` is an additive service property. |
| REST | No route removed or renamed. The public API keeps saying `/projects`, never `/workspaces`. All Phase 3 routes are additive. `/ocr` stays separate — a PDF import is **never** silently routed through OCR. |
| MCP | The nine core tools and the four Phase 2 Asset tools are unchanged. Six read-only document tools are added; **no document-write MCP tool exists in Phase 3** (Claude Code drives imports through the CLI). MCP startup still does not start the worker. |
| Artifact | `.openmind schemaVersion` stays `1.1.0`; the document model is **not** exported through Bundle 1.x. |
| Skill bridge | JSON-lines protocol unchanged, still independent of the application database. |
| Job engine | The single worker is not replaced. `document_ingest` is an additional job type; `jobs.payload_json` is an additive nullable column. |
| Databases | A Phase 1 or Phase 2 database migrates to head with projects, paths, jobs, Asset history, content blobs, file index, vector collections, maps, cases, Ask history and template metadata intact — `v0004` only `CREATE`s tables/indexes and `ADD COLUMN`s with defaults. |

### 15.1 Schema (migration `v0004_document_ingestion`)

* `ALTER TABLE segments ADD COLUMN content_blob_hash TEXT NOT NULL DEFAULT ''`
* `ALTER TABLE jobs ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'`
* `CREATE TABLE document_parses (…)`
* `CREATE TABLE document_index (…)`
* indexes: `idx_document_parses_status`, `idx_document_index_ws`,
  `idx_segments_blob`

The migration is written so it is safe to re-run against a database that already
has the column (SQLite has no `ADD COLUMN IF NOT EXISTS`), via an `upgrade(conn)`
function that inspects `PRAGMA table_info` first.

### 15.2 Dependency policy

| Package | License | Why | Used by |
| --- | --- | --- | --- |
| `python-docx` | MIT | DOCX paragraphs/tables/styles | `docx_parser` |
| `pypdf` | BSD-3-Clause | pure-Python PDF text extraction | `pdf_parser` |
| `openpyxl` | MIT | XLSX cells, formulas-as-text, merges | `spreadsheet_parser` |
| `defusedxml` | PSF-2.0 | hardens the stdlib XML parsers OOXML uses | `security` |

All four are permissive and compatible with this MIT project. **No AGPL parser
dependency is introduced** — in particular PyMuPDF is deliberately *not* used
for PDF text extraction. None of the four performs telemetry or network calls.
All are imported lazily, a missing one fails only its format, and the
dependency-free `.openmind` artifact export CI job stays green.

---

## 16. Testing strategy

New acceptance scripts, all registered in `scripts/run_acceptance.py` (an
unregistered `tests/verify_*.py` already fails the runner, so a missing core
document test fails the manifest):

| Script | Covers |
| --- | --- |
| `verify_document_registry` | probe facts, deterministic order, single selection, ambiguity failure, `dependency_unavailable`, `unsupported`, lazy imports |
| `verify_document_parsers` | every mandatory per-format case in the task spec, plus byte-for-byte determinism on a repeated parse |
| `verify_document_security` | ZIP traversal, zip bomb, member caps, XML entities disabled, oversized PDF, oversized workbook, formulas not executed, HTML scripts ignored, no remote `$ref` fetch |
| `verify_document_ingest` | the 13 mandatory import cases + the mandatory Evidence cases |
| `verify_document_search` | exact-ID retrieval, exact-outranks-semantic, evidence ids, combined search shape, candidate association, low-confidence labelling, never-confirmed |
| `verify_document_cli` | `document add/list/show/outline/search/related`, `knowledge search`, JSON contract, exit codes, bounds, `--dry-run` |
| `verify_document_adapters` | additive REST routes, cross-workspace 404, the 13 pre-existing MCP tools still present, the 6 new document tools additive |

Fixtures live in `fixtures/documents/` and are **invented, neutral content**
about a fictional "NameCheck" service. `sample-requirements.docx`,
`sample-design.pdf` and `sample-tests.xlsx` are generated deterministically by
`scripts/build_document_fixtures.py` (documented in the fixture README) and are
each a few kilobytes.

---

## 17. Result

Implemented as designed. The full suite, every tier:

```
python scripts/run_acceptance.py --all --json
ok=true — 36 passed, 0 failed, 0 skipped   (1,568 individual checks)
```

| Script | Checks | Script | Checks |
| --- | --- | --- | --- |
| verify_document_registry | 42 | verify_document_search | 79 |
| verify_document_parsers | 190 | verify_document_cli | 90 |
| verify_document_security | 74 | verify_document_adapters | 93 |
| verify_document_ingest | 102 | verify_migrations | 76 (was 64) |

Schema head: **4**. MCP tool set: **19** (9 core + 4 Asset + 6 document), with
the first thirteen unchanged. Artifact contract: **1.1.0**, unchanged.

Five bugs the suites caught during implementation, all fixed:

1. `DocumentBuilder.full` was a silent early-exit signal — a parser that broke
   out of its loop produced a truncated document still labelled `parsed`.
2. `decode_text` tried UTF-16 before the single-byte fallbacks, and a UTF-16
   decode of arbitrary text succeeds whenever the length is even, silently
   turning `caf\xe9 latte` into CJK mojibake.
3. `cmd_document_add` mutated `ok`/`error` *after* emitting, so a
   `possible_revision` printed `"ok": true` while exiting non-zero.
4. `extract_terms` fed identifier COMPONENTS back as lexical terms, so
   `REQ-NC-017` also matched `REQ-NC-018` and `REQ-NC-019`.
5. A removed-then-reappeared document was reactivated but never re-indexed —
   active in the database and invisible to search, which is the worst failure
   mode because nothing looks wrong.

Verified out of band as well: with `python-docx`, `pypdf`, `openpyxl` and
`defusedxml` all blocked, the artifact export still runs, the registry still
loads, Markdown/HTML/CSV still parse, and DOCX/PDF/XLSX report
`dependency_unavailable` for the *right* parser. A database populated by the
Phase 2 build and opened by this one migrates 3 → 4 with zero differences across
projects, jobs, file index, assets, revisions, segments, evidence, Ask history,
kv and project meta.

---

## 18. Deferred (Phase 4+) work

Explicitly **not** implemented here, and never described as implemented:

* Requirement, Business Rule, Design Decision and Acceptance Criterion
  extraction;
* semantic document classification and document-authority inference;
* canonical Claim and Relation tables, Knowledge-Graph edges,
  Requirement-to-Code traceability, conflict detection, induced Project Lens;
* OCR execution (image-only PDFs are *detected* and marked `needs-ocr` only);
* COBOL / JCL / PPTX / email-archive parsing; Jira and Confluence connectors;
* branch or PR overlays, webhooks, Bundle 2.0, Titan Mind, Neo4j;
* cloud OpenAI / Anthropic / Bedrock / Azure / Vertex calls;
* historical (non-current-revision) document search;
* a worker-pool rewrite, new Agent Skills, and any new UI.
