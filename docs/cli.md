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

**Why `.openmind` stays at schema 1.1.0.** The artifact bundle is a frozen
integration contract that external consumers depend on. The document model is
not exported through it, so nothing downstream changes.

### `provider` — semantic provider profiles (v2 Phase 4)

Provider profiles are **machine-local** configuration
(`~/.openmind/providers.json`): which endpoints this machine may reach and
which environment variable holds each credential. **The key VALUE is never
stored, logged or accepted as an argument** — there is deliberately no
`--api-key` flag, and the parser refuses prefix-abbreviations that could
smuggle one in.

```bash
# list profiles with their static validation (invalid ones stay visible)
python -m openmind.cli provider list --json

# create/update a profile. Only the ENV VAR NAME is stored; model names are
# yours to configure — OpenMind never hardcodes a current model as a default.
python -m openmind.cli provider configure \
  --name openai-main \
  --kind openai \
  --api-key-env OPENAI_API_KEY \
  --fast-model configured-fast-model \
  --standard-model configured-standard-model \
  --strong-model configured-strong-model \
  --max-classification internal \
  --json

# a local OpenAI-compatible profile (llama-server etc.) must be loopback
python -m openmind.cli provider configure \
  --name local-semantic --kind local-openai \
  --endpoint http://127.0.0.1:7081/v1 --json

# validate configuration (NO network call); test with one explicit call
python -m openmind.cli provider validate --name openai-main --json
python -m openmind.cli provider test --name openai-main --live --json

# remove (refused while a workspace policy selects it, unless --force)
python -m openmind.cli provider remove --name openai-main --json
```

Supported kinds: `local-openai` (loopback only), `openai`, `anthropic`,
`azure-openai` (requires `--endpoint` + `--azure-api-version`), `mock`
(tests). Remote endpoints must be HTTPS; every call is pinned to exactly the
profile's host through the audited semantic transport and recorded in
`data/semantic_audit.log` (byte counts and hashes — never bodies, never
headers).

### `semantic` — explicit, policy-gated analysis (v2 Phase 4)

Semantic analysis is **opt-in per workspace and per invocation**. Ordinary
`ingest` / `asset add` / `document add` never call a model; there is no
implicit `--analyze`. Every workspace starts `restricted` with remote use
**off**, and a remote call happens only when policy, profile, classification,
credential and budget ALL permit it — checked before any content leaves the
process.

```bash
# show / set the workspace policy (fail-closed defaults)
python -m openmind.cli semantic policy show --workspace p_... --json
python -m openmind.cli semantic policy set \
  --workspace p_... \
  --classification internal \
  --allow-remote \
  --provider openai-main \
  --max-requests 100 --max-input-tokens 500000 \
  --max-output-tokens 100000 --max-strong-requests 5 \
  --json

# dry-run plan: deterministic, writes nothing, calls NO provider
python -m openmind.cli semantic plan \
  --workspace p_... \
  --tasks requirement-extraction,interface-extraction \
  --scope documents --json

# run analysis (a resumable, budget-bounded background job)
python -m openmind.cli semantic analyze \
  --workspace p_... \
  --tasks requirement-extraction,interface-extraction \
  --scope documents --wait --json

# resume an interrupted/partial run — completed targets are never re-billed
python -m openmind.cli semantic resume --workspace p_... --run run_... --wait --json

# inspect runs, candidates, relations, conflicts and the usage ledger
python -m openmind.cli semantic runs --workspace p_... --json
python -m openmind.cli semantic show --workspace p_... --run run_... --json
python -m openmind.cli semantic candidates \
  --workspace p_... --type requirement --review-status unreviewed \
  --lifecycle-status active --json
python -m openmind.cli semantic candidate --workspace p_... --candidate sc_... --json
python -m openmind.cli semantic relations --workspace p_... --json
python -m openmind.cli semantic conflicts --workspace p_... --json
python -m openmind.cli semantic usage --workspace p_... --run run_... --json

# review: confirm / reject / reset (bounded note, caller-supplied reviewer)
python -m openmind.cli semantic review \
  --workspace p_... --candidate sc_... \
  --decision confirm --note "Reviewed against the cited specification." --json
```

**Candidates are never canonical truth.** Every extraction is stored as a
CANDIDATE with locally verified Evidence: each cited id must exist in this
workspace, must have been part of the request, and each quote must be a
substring of the immutable snapshot (whitespace-normalized). Fabricated
citations are rejected. Final confidence is derived **locally** (`high` needs
an explicit identifier or normative language plus an exact verified quote);
the model's own confidence is recorded as a hint and decides nothing.
Confirming a candidate marks it suitable for later promotion — the canonical
Knowledge Graph is Phase 5.

**Caching and cost honesty.** An identical re-analysis is a local cache hit
and performs zero provider calls; `--force` bypasses. Token usage is recorded
per request exactly as the provider reported it (`null` when it did not).
Cost is estimated only from an optional machine-local `pricing.json`;
without one, `estimated_cost` is `null` with `cost_source: "unknown"` —
never a fabricated zero. Budget exhaustion stops new requests, keeps
completed work and reports the run `partial` with `budget_exhausted`.

**Staleness.** A new Revision of a source stales its candidates (and their
dependent relation/conflict candidates) — preserved, queryable, marked. A
confirmed-but-stale candidate keeps its review status.

### `lens` — Adaptive Project Lenses (v2 Phase 4)

A lens is a small declarative description of how THIS project is organized —
roles, identifier schemes, document patterns, which semantic tasks are worth
running where. An **active lens influences semantic planning only**; it never
changes deterministic ingestion, and Template Profiles keep working untouched
(every valid Template is projected as a read-only `builtin` lens).

```bash
python -m openmind.cli lens list --workspace p_... --json
python -m openmind.cli lens show --workspace p_... --lens lens_... --json

# induction: deterministic bounded sampling -> ONE strong-model proposal
python -m openmind.cli lens induce plan --workspace p_... --provider openai-main --json
python -m openmind.cli lens induce --workspace p_... --provider openai-main --wait --json

# the induced lens is PROVISIONAL; each later step is an explicit human verb
python -m openmind.cli lens validate --workspace p_... --lens lens_... --json
python -m openmind.cli lens approve  --workspace p_... --lens lens_... --json
python -m openmind.cli lens reject   --workspace p_... --lens lens_... \
  --reason "Role overlap is too high." --json
python -m openmind.cli lens activate --workspace p_... --lens lens_... --json
python -m openmind.cli lens deactivate --workspace p_... --lens lens_... --json

# organization lens files (<data dir>/lenses, or OPENMIND_LENSES_DIR)
python -m openmind.cli lens import --workspace p_... --name org-standard --json
python -m openmind.cli lens export --workspace p_... --lens lens_... \
  --output ./org-standard.json --json
```

An induced lens can never activate itself: it must pass the closed-schema and
safe-pattern validation (no executable content, no lookbehind/backreference
regexes, no URLs, capped sizes), then deterministic whole-corpus validation
(coverage, role overlap, identifier hits), then explicit `approve`, then
explicit `activate`. Invalid organization lens files stay listed with their
errors.

### `graph` — canonical Knowledge Graph queries and projection (v2 Phase 5)

```bash
python -m openmind.cli graph stats     --workspace p_... --json
python -m openmind.cli graph seed plan --workspace p_... --json   # dry run
python -m openmind.cli graph seed      --workspace p_... --json
python -m openmind.cli graph sync      --workspace p_... --json
python -m openmind.cli graph reconcile --workspace p_... --json
python -m openmind.cli graph search    --workspace p_... --query REQ-NC-017 --json
python -m openmind.cli graph node      --workspace p_... --node ent_... --json
python -m openmind.cli graph expand    --workspace p_... --node ent_... \
    --depth 2 --direction both --json
python -m openmind.cli graph path      --workspace p_... \
    --from ent_... --to ent_... --max-depth 6 --json
```

`seed`/`sync` are the deterministic, **model-free** projector: active Assets
and Segments become code-component / code-symbol / interface / data-model /
database-object / configuration / document / build-definition Entities, with
`contains` (explicit) and `calls` (inferred, ambiguity preserved) Relations.
An unchanged source is a no-op that mints **no** Knowledge Revision. `search`
fuses exact canonical key > exact alias > exact identifier token > lexical >
vector similarity — an exact identifier is never outranked by similar text.
`expand` and `path` are bounded, deterministic traversals with honest
`truncated` / `no-path` outcomes; `path` is generic reachability, NOT formal
Requirement Traceability (Phase 6).

### `promotion` — explicit candidate promotion (v2 Phase 5)

```bash
python -m openmind.cli promotion plan --workspace p_... --candidate sc_... --json
python -m openmind.cli promotion promote --workspace p_... --candidate sc_... \
    --actor reviewer-name --note "Approved for canonical knowledge." --json
python -m openmind.cli promotion relation-plan --workspace p_... --relation sr_... --json
python -m openmind.cli promotion promote-relation --workspace p_... --relation sr_... \
    --actor reviewer-name --note "Endpoints and evidence verified." --json
```

Review and promotion are separate acts: `semantic review --decision confirm`
updates candidate metadata only; **this** command is the sole bridge into the
canonical graph. Eligibility (all re-checked inside the transaction):
`review_status=confirmed`, `lifecycle_status=active`,
`evidence_status=verified`, source Revisions current, endpoints resolving
unambiguously (relations), not already promoted. There is deliberately no
`--accept-stale` / `--ignore-evidence` / `--force-unverified`. `plan` is a
deterministic dry-run that writes nothing; promotion is transactional and
idempotent (re-promoting returns the same target, minting nothing);
conflict candidates cannot be promoted at all. Every promotion records a
promotion row, a Human Decision and exactly one Knowledge Revision.

### `entity` / `claim` / `relation` — graph governance (v2 Phase 5)

```bash
python -m openmind.cli entity list   --workspace p_... --type requirement --json
python -m openmind.cli entity show   --workspace p_... --entity ent_... --json
python -m openmind.cli entity create --workspace p_... --type requirement \
    --key requirement:REQ-NC-017 --name REQ-NC-017 \
    --evidence e_... --actor reviewer --note "manual entity" --json
python -m openmind.cli entity alias-add --workspace p_... --entity ent_... \
    --alias NC-17 --type acronym --actor reviewer --note "known acronym" --json
python -m openmind.cli entity merge  --workspace p_... --source ent_dup --target ent_main \
    --actor reviewer --note "duplicates" --json
python -m openmind.cli entity split  --workspace p_... --source ent_... \
    --new-type workflow --new-key workflow:derived:step-two \
    --claim clm_... --binding bd_... --actor reviewer --note "split" --json
python -m openmind.cli entity authority --workspace p_... --entity ent_... \
    --status authoritative --actor lead --note "board approval" --json
python -m openmind.cli claim create  --workspace p_... --entity ent_... \
    --type normative-statement --statement "..." --evidence e_... \
    --actor reviewer --note "manual claim" --json
python -m openmind.cli relation create --workspace p_... --source ent_a --target ent_b \
    --type refines --state confirmed --evidence e_... \
    --actor reviewer --note "manual relation" --json
python -m openmind.cli relation reject  --workspace p_... --relation rel_... \
    --actor reviewer --note "not real" --json
python -m openmind.cli relation restore --workspace p_... --relation rel_... \
    --actor reviewer --note "was real" --json
```

Every write records a Human Decision with the caller-supplied `--actor`
(never inferred) and bounded `--note`, plus one Knowledge Revision. Manual
Entities/Claims/Relations REQUIRE at least one valid `--evidence` id (quotes,
when given, must match the immutable snapshot; fabrications are rejected).
Manual relations may be `explicit` or `confirmed` only — never `inferred`.
Alias collisions across entities are reported, never silently attached.
`supersede`/`withdraw` subcommands exist for all three object kinds; nothing
is ever deleted — superseded, withdrawn, merged and rejected records stay
queryable.

### `knowledge revisions` / `revision` / `decisions` — graph history (v2 Phase 5)

```bash
python -m openmind.cli knowledge revisions --workspace p_... --json
python -m openmind.cli knowledge revision  --workspace p_... --number 3 --json
python -m openmind.cli knowledge decisions --workspace p_... --json
```

Added beside the Phase 3 `knowledge search` (which is unchanged). The ledger
is per-workspace, monotonic and immutable; one graph transaction = one
revision, and a failed transaction leaves none.

### `bundle export` — Knowledge Bundle 2.0 Draft (v2 Phase 5)

```bash
python -m openmind.cli bundle export --workspace p_... --output ./.openmind-v2 \
    --current-only --json
python -m openmind.cli bundle export --workspace p_... --output ./.openmind-v2 \
    --include-history --json
python -m openmind.bundle_verify ./.openmind-v2
```

A SEPARATE contract from the frozen `.openmind` 1.1.0 artifact: schema
`2.0.0-draft.1`, its own directory of deterministic JSONL files + JSON
schemas + a manifest with per-file SHA-256 hashes and record counts. No
secrets, no provider profiles, no prompts, no raw model output, no absolute
paths. `--knowledge-revision N` filters records by their creation revision
stamp (documented as such — it does not reconstruct point-in-time lifecycle
states). `python -m openmind.bundle_verify` is a standalone stdlib-only
verifier any consumer can run.

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
`save_case`, `get_doc`, `propose_fix`, `apply_fix`) are unchanged. Alongside
them: four read-only Asset tools (Phase 2), six read-only document tools
(Phase 3) and seven read-only semantic/lens tools (Phase 4) — 26 in total.
Nothing on MCP configures a provider, changes egress policy, starts a
paid analysis, reviews a candidate or activates a lens; those verbs stay on
this CLI where they are visible. Merely serving MCP never starts the
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
