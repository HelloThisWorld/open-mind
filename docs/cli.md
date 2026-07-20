# OpenMind CLI

```bash
python -m openmind.cli --help
```

> **Why `python -m` and not a bare `openmind`?** This repository has no Python
> packaging metadata (no `pyproject.toml` or `setup.py`) — it is run from a
> checkout, and `requirements.txt` is the dependency contract. A console entry
> point needs an installable distribution, so adding one would mean introducing
> packaging that nothing else here uses. `python -m openmind.cli` works from a
> clean clone with no install step. If the project later gains packaging
> metadata, exposing `openmind` as a console script is a two-line addition.

The CLI, the MCP server and the FastAPI app are three front ends over **one
runtime**. A workspace created here is the workspace the UI shows; a job
enqueued here is executed by the same worker. The web UI is optional.

> **Vocabulary.** `--workspace` takes the id the rest of OpenMind calls a
> *project id* (`p_...`). The stored entity is still a project and the REST API
> still says `/projects`. "Workspace" is internal naming that a later v2 phase
> will build on; nothing about the stored shape has changed.

---

## Global flags

| Flag | Meaning |
| --- | --- |
| `--help` | usage for the CLI or a subcommand |
| `--version` | print the runtime version and exit |
| `--json` | print one machine-readable JSON object on stdout |
| `--quiet`, `-q` | suppress human progress output |
| `--verbose`, `-v` | print extra diagnostics on stderr |

All five work on **either side** of the subcommand — `openmind --json doctor`
and `openmind doctor --json` are equivalent.

### `--json` guarantees

* Exactly **one** JSON object is printed, to **stdout**, once, at the end.
* Every human message, warning and progress line goes to **stderr**.
* No ANSI escape codes are emitted, in either mode.
* A failure still prints a JSON object, so the output is parseable even when
  the exit code is non-zero:

```json
{
  "ok": false,
  "error": {
    "code": "workspace_not_found",
    "message": "workspace not found: 'p_nope'",
    "details": { "workspace_id": "p_nope" }
  }
}
```

This means `openmind status --workspace "$WS" --json | jq .counts` is safe to
script against.

---

## Exit codes

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `0` | success | |
| `1` | the operation ran, the domain result is a failure | `doctor` found an error-severity problem; the workspace or job does not exist |
| `2` | invalid arguments or configuration | a missing required flag, a path that is not a directory, `--template` with `--no-template`, a non-loopback `serve` bind |
| `3` | a required runtime dependency or backend is unavailable | the `mcp` package is not installed; `uvicorn` is missing |
| `4` | job or execution failure | an ingest reached `failed`, `paused` or `interrupted` instead of `done` |
| `5` | timeout or cancellation | `--wait` exceeded `--timeout`; Ctrl+C |

argparse's own usage errors also exit `2`.

A note on `4`: `paused` and `interrupted` are *not* terminal states, but the
worker will not advance them without an explicit resume. `--wait` returns them
with exit `4` and the real status rather than blocking until the timeout — a
stalled job is reported honestly, not waited on forever.

---

## Commands

### `doctor`

```bash
python -m openmind.cli doctor
python -m openmind.cli doctor --json
```

Non-destructive diagnostics: data directory, database, migration version,
project-directory permissions, vector-store backend, embedding backend, MCP
dependency, model configuration, local-model readiness, network policy, and the
runtime version.

**Severity policy.** Only an `error` fails `doctor` (exit `1`). A `warn` means
degraded-but-usable and exits `0`. This matters most for the local model:
OpenMind ingests, extracts, exports artifacts and answers glossary/structure
queries with **no model at all**, so a missing `llama-server` is a warning, never
a failure. A model-dependent operation checks readiness separately.

### `init`

```bash
python -m openmind.cli init --name demo --path ./fixtures/sample-repo
python -m openmind.cli init --name demo --path ./src --ingest --wait
```

| Flag | Meaning |
| --- | --- |
| `--name` | workspace display name (required) |
| `--path` | source directory to register |
| `--exclude` | path to exclude (repeatable) |
| `--ingest` | ingest immediately — **off by default** |
| `--wait` | with `--ingest`, wait for the job to finish |
| `--timeout` | bound for `--wait`, in seconds (default 3600) |

Creating a workspace never ingests unless `--ingest` is given. Prints the
workspace id on stdout (or the full record with `--json`).

Relative paths are resolved to absolute before being stored, so a later ingest
does not depend on the worker's working directory. The absolute path is written
to the **machine-local sidecar** (`~/.openmind`), never to the portable
database or an exported artifact.

### `add`

```bash
python -m openmind.cli add --workspace p_abc123 --path ./services/api \
    --exclude target --exclude build
```

Registers a new source path or updates an existing one's exclude set. Repeating
`add` for the same path updates it in place rather than duplicating it.

### `ingest`

```bash
python -m openmind.cli ingest --workspace p_abc123
python -m openmind.cli ingest --workspace p_abc123 --wait --timeout 1800
python -m openmind.cli ingest --workspace p_abc123 --path ./services/api
```

| Flag | Meaning |
| --- | --- |
| `--workspace` | workspace id (required) |
| `--path` | restrict the ingest to this subtree |
| `--wait` | poll until the job reaches a terminal state |
| `--timeout` | bound for `--wait`, in seconds (default 3600) |

Without `--wait` it returns the persisted job id immediately; the job runs in
the background and survives the CLI process. With `--wait` it starts the job
worker, then polls the **persisted** job row — state is never cached, because
the database is the single source of truth that pause, resume and restart
recovery all depend on.

Ingestion is incremental: unchanged files are skipped by content hash.

A timeout does **not** cancel the job. It keeps running and can be polled with
`status` or waited on again.

### `status`

```bash
python -m openmind.cli status --workspace p_abc123
python -m openmind.cli status --workspace p_abc123 --jobs 20 --json
```

Reports workspace metadata, registered paths, state, template selection,
indexed-file / code-chunk / glossary-term / solved-case counts, active and
recent jobs, the schema version and the runtime version. Does **not** require a
running FastAPI server.

A count of `unknown` (`null` in JSON) means that store could not be read — it is
deliberately distinct from `0`, which means "read successfully, and empty".

### `asset`

Inspect the canonical **Asset model** (OpenMind v2 Phase 2): every indexed
file is an **Asset**, every observed version of it an immutable **Revision**,
each revision divided into **Segments**, each with source-locatable **Evidence**.
See [docs/v2/phase-2-asset-model.md](v2/phase-2-asset-model.md) for the model.

All subcommands take `--workspace` and support `--json` (exactly one object on
stdout). Lists are bounded and report a total; content is never printed unbounded.

```bash
# list assets (bounded; filter by type/state)
python -m openmind.cli asset list --workspace p_... --type source-code \
  --state active --limit 100 --json

# one asset + its current-revision summary
python -m openmind.cli asset show --workspace p_... --asset a_... --json

# an asset's revision history (newest first)
python -m openmind.cli asset revisions --workspace p_... --asset a_... --json

# a revision's segments (each carries an evidence_id for the next step)
python -m openmind.cli asset segments --workspace p_... --revision r_... \
  --limit 100 --json

# one evidence citation: locator, snapshot + current-source status,
# and bounded content recovered from the immutable snapshot
python -m openmind.cli asset evidence --workspace p_... --evidence e_... \
  --max-chars 4000 --json

# ingest a single existing file that lives under a registered source root
python -m openmind.cli asset add --workspace p_... --path ./src/File.java \
  --wait --json
```

`asset add` accepts **one existing file** under an already-registered source
root; a directory is rejected (exit `2`) with a pointer to `openmind add`. An
unsupported format is registered as an `unsupported` Asset — recorded honestly,
never falsely reported as parsed — and not ingested. Without `--wait` it returns
the job id.

`asset evidence` reports both a **snapshot** status (`available` / `corrupt` /
`missing`, recovered from the immutable content blob) and a **current source**
status (`matches` / `changed` / `missing`), so historical evidence stays readable
after the source file changes. Cross-workspace access to any asset/revision/
segment/evidence id is a typed not-found (exit `1`), never a leak.

`status` additionally reports asset counts (`assets_total` / `assets_active` /
`assets_removed`, `revisions`, `segments`, `evidence`) — additive; every prior
`status` key is unchanged.

### `document`

Append and query **enterprise documents** (OpenMind v2 Phase 3). A document
becomes an Asset like any other: immutable Revisions, structural Segments, and
Evidence you can cite. See
[docs/v2/phase-3-document-ingestion.md](v2/phase-3-document-ingestion.md).

Supported formats: Markdown, plain text / RST / AsciiDoc, HTML, CSV/TSV, DOCX,
text-based PDF, XLSX, OpenAPI (JSON/YAML), JSON Schema and SQL DDL.

```bash
# append a document from ANYWHERE on this machine
python -m openmind.cli document add \
  --workspace p_... --path ./Requirements_v3.docx --wait --json

# list document assets (bounded; filter by parse status or parser)
python -m openmind.cli document list --workspace p_... --json

# one document: asset, current revision, parse summary and warnings
python -m openmind.cli document show --workspace p_... --asset a_... --json

# a bounded STRUCTURAL outline of a revision (structure, not content)
python -m openmind.cli document outline \
  --workspace p_... --revision r_... --limit 500 --json

# document search (exact identifiers outrank merely similar text)
python -m openmind.cli document search \
  --workspace p_... --query "REQ-NC-017" --json

# deterministic candidate associations for one document
python -m openmind.cli document related --workspace p_... --asset a_... --json

# code AND documents, reported separately
python -m openmind.cli knowledge search \
  --workspace p_... --query "NameCheck timeout" --json
```

**How to append a document.** `document add` reads a local file, snapshots its
exact bytes into the immutable content store, and enqueues a `document_ingest`
job. **The absolute path never enters the portable database** — the job payload
carries a content hash and a filename. The file does not have to live under a
registered source root; a document that does not is an *attachment*, and its
snapshot is its canonical source.

**Duplicate and revision decisions.** `document add` reports which of five
things happened, and refuses to guess:

| `status` | What it means | What was written |
| --- | --- | --- |
| `new_asset` | nothing matched | a new Asset + Revision |
| `revision` | the target Asset exists and the bytes differ | the next Revision |
| `duplicate` | these exact bytes are already a document Revision here | **nothing** |
| `possible_revision` | the filename collides with a DIFFERENT document | **nothing** |
| `unsupported` | no parser claims these bytes | an `unsupported` Asset |

`possible_revision` exits **1** and hands back the commands that resolve it —
two teams' `requirements.docx` are not the same document, and merging them
because their names match is not recoverable:

```
--asset a_...                # these bytes are a new revision of that document
--new-asset                  # this is a different document
--logical-key documents/...  # name it yourself
```

`--asset`, `--logical-key` and `--new-asset` each name a *different* target for
the same bytes and are mutually exclusive (exit `2`). `--new-asset` produces a
readable deterministic key, e.g. `documents/Requirements_v3--9f2c1a3b.docx`.
`--dry-run` reports the plan and stores nothing. `--version-label` sets an
explicit label; otherwise a label is taken only from a *documented* metadata
field (OpenAPI `info.version`, the DOCX core `revision`) — never guessed from
prose or a filename.

**How document Evidence is preserved.** Every stored block's exact text is
snapshotted as its own content-addressed blob, so `asset evidence` recovers a
historical citation **without re-running a parser** — a newer parser version
can never rewrite history. For a workspace document the current-source status is
`matches` / `changed` / `missing`; for an attachment it is `not-applicable`,
because its origin path is deliberately not tracked and `missing` would be a
false alarm.

**Why `related` results are only candidates.** `document related` returns
**observed mentions**, each labelled `status: "candidate"` with a confidence:
`high` for an exact explicit identifier (a file path, a code symbol, a shared
requirement id), `medium` for an exact match after normalization (an API path, a
config key, a database object), `low` for embedding similarity alone. Nothing is
persisted, and no result claims the document *implements*, *refines*, *verifies*
or *contradicts* anything — that needs verification OpenMind does not yet
perform. Likewise `knowledge search` returns code and documents in separate
sections and never asserts a relationship between them.

**Why OCR is not implemented.** An image-only PDF is *detected* and reported as
`needs-ocr`, and produces no blocks. No text is invented for it, and no result
ever claims OCR ran. Running OCR is Phase 4+.

**Why Requirement extraction is Phase 4.** This phase ingests and normalizes the
raw document layer. Extracting Requirements, Business Rules, Design Decisions or
Acceptance Criteria — and asserting how they relate to code — requires semantic
verification, and asserting it without that would put unverifiable claims into
immutable history.

**Why `.openmind` stays at schema 1.1.0.** The artifact bundle is a frozen
integration contract that external consumers depend on. The document model is
not exported through it, so nothing downstream changes.

### `export`

```bash
python -m openmind.cli export --repo ./fixtures/sample-repo --output ./.openmind
python -m openmind.cli export --repo ./repo --output ./out --template spring-boot
python -m openmind.cli export --repo ./repo --output ./out --no-template --json
```

Writes the deterministic `.openmind` artifact directory — **schema 1.1.0**,
unchanged and frozen. Equivalent to `python -m openmind.artifacts`, which also
still works.

Export is offline and standalone: it does not open the database, the vector
store, the model server or the web app, so it runs from a clean checkout with
nothing installed beyond the standard library.

`--generated-at` overrides the manifest timestamp for byte-reproducible builds.

### `mcp serve`

```bash
python -m openmind.cli mcp serve
```

Runs the MCP stdio server — the *same* implementation as
`python -m openmind.mcp_server`, not a second copy of the tools. The nine core
tools (`search`, `route`, `dispatch`, `get_glossary`, `find_similar_cases`,
`save_case`, `get_doc`, `propose_fix`, `apply_fix`) are unchanged. Phase 2 adds
four **read-only** Asset tools alongside them (`list_assets`, `get_asset`,
`get_asset_revisions`, `get_evidence`); merely serving MCP never starts the
ingestion worker.

stdout is the MCP transport, so all CLI chatter goes to stderr on this command.

### `serve`

```bash
python -m openmind.cli serve
python -m openmind.cli serve --host 127.0.0.1 --port 8077
```

Runs the FastAPI application.

**Loopback by default.** OpenMind serves project content — source snippets,
paths, prompts. `serve` refuses a non-loopback bind with exit `2` unless
`--allow-non-loopback` is passed explicitly. There is no silent `0.0.0.0`.

---

## Scripting examples

Create, ingest and read back a workspace:

```bash
WS=$(python -m openmind.cli init --name demo --path ./src --json | jq -r .workspace_id)
python -m openmind.cli ingest --workspace "$WS" --wait --json | jq '.progress'
python -m openmind.cli status  --workspace "$WS" --json | jq '.counts'
```

Fail a build when diagnostics report a real problem:

```bash
if ! python -m openmind.cli doctor --json > doctor.json; then
    jq -r '.checks[] | select(.status=="error") | "\(.name): \(.detail)"' doctor.json
    exit 1
fi
```

Distinguish a timeout from a failure:

```bash
python -m openmind.cli ingest --workspace "$WS" --wait --timeout 300 --json
case $? in
  0) echo "ingested" ;;
  4) echo "job did not complete — check 'status' for why" ;;
  5) echo "still running; poll later" ;;
esac
```

---

## Environment variables

| Variable | Effect |
| --- | --- |
| `OPENMIND_DATA_DIR` | where the database, vector store and per-project data live (default `./data`) |
| `OPENMIND_MACHINE_DIR` | the machine-local sidecar holding absolute source roots (default `~/.openmind`) |
| `OPENMIND_EMBED_OFFLINE` | `1` forces the deterministic hashing embedder; no model download |
| `OPENMIND_EMBED_DEVICE` | `cpu`, `auto`, ... |
| `OPENMIND_ENRICH_EGRESS` | `0` disables Wikipedia glossary enrichment |
| `OPENMIND_SOURCELINK_EGRESS` | `0` disables on-demand GitHub source fetching |
| `OPENMIND_INGEST_FREE_GPU` | `0` keeps a resident model loaded during bulk embedding |

Document parsing runs inside an explicit resource envelope. Every limit below is
overridable, and hitting one produces a `partial` parse with a warning naming
that exact limit — never a silent truncation.

| Variable | Default | Bounds |
| --- | --- | --- |
| `OPENMIND_DOCUMENT_MAX_BYTES` | 25,000,000 | whole-document size (refused above it) |
| `OPENMIND_DOCUMENT_MAX_BLOCKS` | 20,000 | blocks per document |
| `OPENMIND_DOCUMENT_MAX_BLOCK_CHARS` | 20,000 | characters per block |
| `OPENMIND_PDF_MAX_PAGES` | 2,000 | pages read from a PDF |
| `OPENMIND_DOCX_MAX_PARAGRAPHS` | 50,000 | DOCX paragraphs |
| `OPENMIND_DOCX_MAX_TABLES` | 2,000 | DOCX tables |
| `OPENMIND_XLSX_MAX_SHEETS` | 100 | worksheets read |
| `OPENMIND_XLSX_MAX_ROWS_PER_SHEET` | 20,000 | rows per sheet |
| `OPENMIND_XLSX_MAX_CELLS` | 500,000 | cells per workbook |
| `OPENMIND_CSV_MAX_ROWS` | 50,000 | CSV/TSV rows |
| `OPENMIND_ZIP_MAX_MEMBERS` | 2,000 | members in a DOCX/XLSX package |
| `OPENMIND_ZIP_MAX_TOTAL_BYTES` | 400,000,000 | total uncompressed size |
| `OPENMIND_ZIP_MAX_MEMBER_BYTES` | 100,000,000 | one member's uncompressed size |
| `OPENMIND_ZIP_MAX_RATIO` | 200 | compression ratio (bomb detection) |

The acceptance runner sets the offline/no-egress set for every test.

---

## See also

* [`docs/database-migrations.md`](database-migrations.md) — the schema ledger
* [`docs/v2/phase-1-core-foundation.md`](v2/phase-1-core-foundation.md) — why the
  runtime is shaped this way
