# OpenMind v2 — Phase 1: Core Extraction and Tool-First Foundation

Status: implemented on branch `feat/v2-phase1-core-foundation`.
Runtime version introduced by this phase: **`1.1.0-dev`**.

This phase is an **architectural foundation**, not a feature release. It moves
OpenMind from a UI-centric local application to a headless engineering knowledge
runtime that CLI, MCP, FastAPI and tests all drive through one shared bootstrap.
It deliberately implements **none** of the later v2 enterprise knowledge
features (see [Deferred work](#8-deferred-v2-work)).

---

## 1. Baseline: what already worked

Phase 1 started from a clean working tree at `f76aac7`, with the whole existing
acceptance suite green. Recorded before any change was made:

| Check | Result |
| --- | --- |
| `python -m compileall -q openmind` | pass |
| `tests/verify_artifacts.py` | 31 passed, 0 failed |
| `tests/verify.py` | 17/17 |
| `tests/verify_structure.py` | 14 passed |
| `tests/verify_glossary.py` | 20 passed |
| `tests/verify_router.py` | 23 passed |
| `tests/verify_diagrams.py` | 7 passed |
| `tests/verify_templates.py` | 21/21 |
| `tests/verify_facets.py` | 22/22 |
| `tests/verify_guide.py` | 19/19 |
| `tests/verify_resources.py` | 15 passed |
| `tests/verify_fixes.py` | 7/7 |
| `tests/verify_fixes2.py` | 7/7 |
| `tests/verify_grounding.py` | 8 passed |
| `tests/verify_ask.py` | 15/15 |
| `tests/verify_ask2.py` | 29/29 |
| `tests/verify_async_delete.py` | 6/6 |
| `tests/verify_delete_responsive.py` | 8/8 |
| `tests/verify_modelserver.py` | 10/10 |
| `tests/verify_source_link.py` | 25 passed, 0 failed |

This is the regression bar. Phase 1 refactors **behind** these tests; it does not
relax them.

---

## 2. Current coupling (the problem)

### 2.1 Three divergent startup paths

`db.init_db()` is reachable from three places that do materially different work:

| Entry point | What it initializes |
| --- | --- |
| `main.py:_lifespan` | `config.ensure_dirs()`, `db.init_db()`, `vectorstore.sweep_orphan_segment_dirs()`, `jobs.start_worker()`, vector-store warm-up thread |
| `mcp_server.py` (module import, line 23) | `db.init_db()` only |
| `db._c()` (lazy, any caller) | `db.init_db()` only |

Consequences:

* **A job enqueued outside FastAPI never runs.** `jobs.start_worker()` is called
  only from the FastAPI lifespan. Any other process can write a `queued` row and
  then wait forever. This is the single hardest blocker for a useful CLI.
* The orphan-segment sweep must run *before* any Chroma client exists in the
  process (raw sqlite read vs. the Rust bindings' GIL/lock interaction). Only the
  FastAPI path honours that ordering; nothing encodes it as a rule.
* Import-time side effects in `mcp_server.py` mean importing the module for a
  test opens the live database.

### 2.2 `main.py` is the only orchestrator

Orchestration — not just HTTP concerns — lives inside route bodies:

* `POST /projects` — create row, create dirs, register path, run template
  auto-detection, persist `meta.template_auto`.
* `POST /projects/{id}/paths` — normalize, validate, sidecar write, state bump.
* `POST /ingest` — resolve scope, enqueue, project-state transition.
* `POST /projects/{id}/terminate`, `DELETE /projects/{id}` — multi-step teardown.
* `GET /api/health` — assembles diagnostics inline.

None of this is reachable without importing FastAPI and constructing a request.

### 2.3 Schema evolution is untracked

`db.init_db()` runs one `executescript` of `CREATE TABLE IF NOT EXISTS`
statements plus one ad-hoc, idempotent fixer (`_migrate_paths_to_sidecar`) that
re-scans every project row on **every** startup. There is:

* no recorded schema version — nothing can report or gate on it;
* no way to express a non-additive change (a column rename, a backfill) safely;
* no protection against an older build opening a newer database.

### 2.4 Version is scattered and stale

`openmind/__init__.py` (`0.1.0`), `FastAPI(version="0.1.0")`, `package.json`
(`0.1.0`), and `artifacts._generator_version()` each carry the version
independently. There is no single constant to report from `doctor` or `--version`.

### 2.5 No first-class CLI

The only module entry points are `python -m openmind.artifacts`,
`python -m openmind.skill_bridge` and `python -m openmind.mcp_server`. Creating a
project, ingesting it, or inspecting its state requires the web UI.

---

## 3. Target boundaries

```text
CLI          MCP          FastAPI          Tests
  \           |              |              /
   \          |              |             /
    +---------+--------------+------------+
                     |
             OpenMindRuntime            (composition root; idempotent)
                     |
         Application services            (use-case orchestration)
    workspace / ingest / job / export / health
                     |
      Existing deterministic implementation
   db  jobs  machine  templates  artifacts  glossary  structure  rag ...
                     |
        SQLite  /  Vector store  /  Filesystem
```

Rules that keep this honest:

1. **Services never import FastAPI.** They return plain dicts / dataclasses.
2. **Adapters map, they do not orchestrate.** A route validates input, calls one
   service method, and shapes the response.
3. **Strangler, not rewrite.** Services wrap the existing modules. No algorithm
   in `jobs.py`, `rag.py`, `glossary.py`, `structure.py` or `artifacts.py` is
   rewritten in this phase.
4. **Ports only where a second implementation or a test seam really exists.**
   Phase 1 introduces exactly two: the workspace repository and the job
   repository, both of which the migration/services tests substitute. No
   speculative interfaces.

### 3.1 Module layout added by this phase

```text
openmind/
├── version.py                  single runtime version constant
├── domain/
│   ├── errors.py               typed application errors -> exit codes / HTTP status
│   └── types.py                dataclasses crossing the service boundary
├── ports/
│   ├── workspace_repository.py protocol implemented by db (+ test fakes)
│   ├── job_repository.py       protocol implemented by db/jobs
│   └── runtime_ports.py        clock / worker-control seams
├── services/
│   ├── service_container.py    lazily constructed, cached service instances
│   ├── workspace_service.py
│   ├── ingest_service.py
│   ├── job_service.py
│   ├── export_service.py
│   └── health_service.py
├── migrations/
│   ├── runner.py               ordered, checksummed, transactional runner
│   └── versions/
│       ├── v0001_baseline.py
│       └── v0002_paths_sidecar.py
├── runtime.py                  OpenMindRuntime + get_runtime()
└── cli.py                      argparse front end
```

`workspace` is an **internal** vocabulary choice only. The stored entity is still
a project, the REST API still says `/projects`, and `WorkspaceService` methods
return records whose id field is the existing `p_*` project id. Renaming the
public API is explicitly out of scope for this phase.

---

## 4. Compatibility constraints

These are hard constraints. Each has a regression test added in this phase.

### 4.1 REST

Every existing route keeps its path, method, query-parameter aliases (notably
`?scope=` binding to `scope_id`), status codes and response body shape. In
particular `/projects` is **not** renamed to `/workspaces`.

`GET /api/health` is additive-only: every pre-existing key is unchanged, and it
gains `version` and `schema_version`. The full `doctor` report is available at
`GET /api/health?diagnostics=1` — kept opt-in because it probes the model
endpoint with a timeout, stats the disk and writes a temp file, which is right
for an on-demand diagnostic and wrong for a liveness endpoint.

One deliberate behaviour fix, covered by `tests/verify_adapters.py`:
`DELETE /projects/{id}/paths` was **broken** for an unknown project. It called
straight through to the database, returned `None` from a handler annotated
`-> Dict[str, Any]`, and FastAPI's response validation turned that into a
**500 `ResponseValidationError`** — verified by running the route against `main`:

```text
fastapi.exceptions.ResponseValidationError: 1 validation error:
  {'type': 'dict_type', 'loc': ('response',), 'msg': 'Input should be a valid
   dictionary', 'input': None}
```

It now returns 404 like every sibling route. This is the "unless a current route
is demonstrably broken" case.

### Verified empirically, not by inspection

The refactored routes were probed on `main` and on this branch through a git
worktree, and the two transcripts diffed (status code + response shape, with
volatile values normalized). Across 29 probes:

* **zero status-code differences**;
* **zero keys removed** from any response;
* 14 purely additive differences — `/api/health` gains `version` and
  `schema_version`, and 13 error bodies gain an `error` key **alongside** the
  unchanged `detail` key, so a client reading `detail` sees no change.

### 4.2 MCP

`python -m openmind.mcp_server` keeps working, and the nine tools keep their
names, argument names and response contracts:

```text
search  route  dispatch  get_glossary  find_similar_cases
save_case  get_doc  propose_fix  apply_fix
```

The refactor adds `create_mcp_server(runtime)` so the server can be constructed
in a test without module-import side effects. The module-level `mcp` object and
`main()` remain, so the documented command is unchanged.

### 4.3 Artifacts

`python -m openmind.artifacts --repo ... --output ...` and the `.openmind`
schema **1.1.0** are frozen. `ExportService` wraps `generate_artifacts()`
without touching it, and the CLI `export` command delegates to the same
function. Bundle 2.0 is not introduced.

Export stays **standalone**: no database, no vector store, no model server, no
web app, and no heavy dependency. That required making
`openmind/services/__init__.py` resolve its re-exports lazily — importing
`ExportService` through an eagerly-importing package `__init__` pulled in
`WorkspaceService`, and through it `openmind.vectorstore`, and through that
numpy and chromadb. The dependency-free `artifact-contract` CI job runs the CLI
exporter with nothing installed, and `tests/verify_cli.py` asserts it by
simulating the absent dependencies rather than trusting the layering.

One deliberate, non-breaking change: `manifest.generator.version` now reports
`1.1.0-dev` instead of `0.1.0`, because it reads `openmind.__version__`.
`schemaVersion` is untouched, the verifier asserts only the generator *name*,
and determinism is preserved (the value is constant within and across runs).

### 4.4 Skill bridge

`python -m openmind.skill_bridge --root <dir>` keeps its JSON-lines protocol and
keeps computing everything in memory from the corpus. It is **not** routed
through the runtime — it must not open the app database. The external
Agent Skill Verification Template continues to exercise the real implementation.

### 4.5 Existing databases

A database created by any previous build must open, migrate and keep all
project, job, file-index, cases and Ask data. See §5.

---

## 5. Database migration strategy

### 5.1 Table

```sql
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
```

### 5.2 Runner guarantees

1. Works on an empty database.
2. Works on a legacy database that has the current tables but no
   `schema_migrations` — see baselining below.
3. Never destroys user data. Migrations are additive in this phase.
4. Applies in ascending numeric order.
5. Each migration runs in one transaction; a failure rolls that migration back
   and leaves `schema_migrations` unchanged for it.
6. Records a `sha256` checksum of the migration's SQL/source.
7. A changed checksum on an already-applied migration raises
   `MigrationChecksumMismatch` with the version, name, stored and computed
   checksums. It does not silently re-apply or ignore.
8. Runs under `db._lock` with the same single WAL connection the app uses, so
   the worker thread and request threads cannot race it.
9. Idempotent: a second run applies nothing and returns the same version.
10. Current version is exposed through `HealthService` → `doctor` and
    `GET /api/health`.

### 5.3 Baselining a legacy database

`v0001_baseline` is the *current* schema expressed as `CREATE TABLE IF NOT
EXISTS`. That makes it safe to run against a legacy database: the statements are
no-ops, and the runner then records version 1 as applied. Concretely:

```text
empty db      -> v0001 creates every table          -> record 1, 2
legacy db     -> v0001 statements are all no-ops    -> record 1, 2 (data intact)
current db    -> nothing to do                      -> no writes
```

`v0002_paths_sidecar` carries the existing `_migrate_paths_to_sidecar` logic. It
stays idempotent and remains callable by `db.init_db()` for defence in depth,
but it is now *recorded*, so the every-startup rescan of all project rows stops
being unconditional.

Alembic is **not** adopted: this is one SQLite file with five tables and a
single-writer lock already in place. A ~150-line runner with real checksum
enforcement is less operational weight than a migration framework, an extra
dependency, and an `alembic.ini`.

---

## 6. CLI contract

Invocation: `python -m openmind.cli` (primary).

### 6.1 Global flags

| Flag | Meaning |
| --- | --- |
| `--help` | usage |
| `--version` | runtime version, then exit 0 |
| `--json` | one JSON object on stdout, nothing else on stdout |
| `--quiet` | suppress human progress output |
| `--verbose` | debug diagnostics on stderr |

`--json` guarantees: exactly one object printed to **stdout**; no ANSI codes;
every diagnostic on **stderr**; a structured `{"ok": false, "error": {...}}` body
on failure, so the output is still parseable when the exit code is non-zero.

### 6.2 Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | operation completed, domain result is a failure (e.g. `doctor` found a problem) |
| 2 | invalid arguments or configuration |
| 3 | missing runtime dependency or unavailable backend |
| 4 | job or execution failure |
| 5 | timeout or cancellation |

### 6.3 Commands

```text
doctor                              non-destructive diagnostics
init    --name --path --exclude --ingest --wait
add     --workspace --path --exclude
ingest  --workspace --path --wait --timeout
status  --workspace
export  --repo --output [--name --template|--no-template --generated-at]
mcp serve                           same implementation as python -m openmind.mcp_server
serve   --host 127.0.0.1 --port 8077
```

`serve` keeps the loopback default and refuses a non-loopback bind unless
`--allow-non-loopback` is passed explicitly, so no silent `0.0.0.0`.

`ingest --wait` requires a running job worker. The runtime starts it on demand
(§7), then polls the **persisted** job row — it never caches job state, because
reconnectability depends on the database being the single source of truth.

---

## 7. Runtime bootstrap

`OpenMindRuntime.bootstrap()` is idempotent and ordered:

1. `config.ensure_dirs()`
2. open the database connection
3. run migrations to head
4. construct the service container

Job-worker startup is **separate and opt-in** (`runtime.ensure_worker()`),
because `doctor`, `status` and `export` must not spawn a worker thread, and
`mcp serve` must not either. FastAPI's lifespan and `ingest --wait` call it.

The orphan-segment sweep keeps its "before any Chroma client" ordering and stays
in the FastAPI lifespan; it is a server-startup concern, not a bootstrap concern,
and running it from short-lived CLI processes would be both wasteful and unsafe
while a server is live.

Waiting on jobs treats `done` and `failed` as terminal. `paused` and
`interrupted` are **not** terminal but are also not progressing, so the waiter
returns them with an explicit status rather than blocking until timeout —
preserving pause/resume semantics without hanging a CLI.

---

## 8. Testing strategy

New tests, all runnable offline and in an isolated `OPENMIND_DATA_DIR`:

| Test | Covers |
| --- | --- |
| `tests/verify_migrations.py` | empty db, legacy baseline, repeat no-op, checksum mismatch, transaction rollback |
| `tests/verify_runtime.py` | bootstrap idempotency, container identity, worker opt-in |
| `tests/verify_services.py` | workspace create / path register / template selection / ingest enqueue / status |
| `tests/verify_cli.py` | JSON contract, exit codes, doctor, init, add, ingest, status, export |
| `tests/verify_adapters.py` | MCP construction, FastAPI route compatibility, skill-bridge smoke |

The existing acceptance scripts run unchanged under a new runner:

```bash
python scripts/run_acceptance.py
```

which isolates `OPENMIND_DATA_DIR` and `OPENMIND_MACHINE_DIR` per script, forces
`OPENMIND_EMBED_OFFLINE=1`, `OPENMIND_ENRICH_EGRESS=0`,
`OPENMIND_SOURCELINK_EGRESS=0`, aggregates results, and returns non-zero on any
failure.

Two integrity properties:

* **A skip is never a pass.** A script that cannot run is reported as `skipped`
  with a reason, and a skipped CORE script fails the run.
* **A test cannot silently go unrun.** Every `tests/verify_*.py` on disk must
  appear in the runner's manifest; an unregistered file fails the run with exit
  2 rather than being quietly excluded while the suite reports green.

Tier assignment was made by checking what each script actually does, not by
assumption:

* `verify_modelserver.py` — CORE. It spawns a *Python stub* HTTP server, not a
  real `llama-server`, so it needs no model binary.
* `verify_source_link.py` — CORE. It exercises URL parsing and the netguard
  egress *policy*; it makes no live network call.
* `verify_delete_responsive.py` — **LOCAL**, the one exclusion. It asserts
  sub-2-second API latency while a delete drains; a shared CI runner's
  scheduling jitter makes that flaky, and a flaky gate is worse than an honest
  exclusion. The manifest records that reason, and `--list` prints it.

---

## 9. Deferred v2 work

Explicitly **not** built in this phase, and no placeholder code pretends
otherwise:

PDF / DOCX / XLSX parsing · COBOL / JCL support · enterprise Asset, Revision,
Claim, Relation tables · engineering Knowledge Graph · cloud model providers
(OpenAI, Anthropic, Bedrock, Azure, Vertex) · requirement-to-code traceability ·
conflict detection · branch overlays · webhooks · new Titan Mind integration ·
new graph UI · new chat interface · autonomous code modification · TypeScript
rewrite · monorepo conversion · Neo4j or another graph database · Bundle 2.0
artifact schema · typed worker pool / job DAG.

Extension points that exist for them: the migration runner (new versioned
tables), `ports/` (a second repository implementation), `ServiceContainer` (new
services), and the CLI subcommand table.

---

## 10. Known risks

* **`main.py` remains large.** It holds ~50 routes; Phase 1 moves orchestration
  out of the project/path/ingest/job/terminate/health routes but deliberately
  leaves Ask, glossary-enrichment, graph and source-navigation routes calling
  their modules directly. Those are thin already, and moving them for symmetry
  would be churn without a test seam.
* **Job-engine internals are still reachable.** `tests/verify.py` and
  `tests/verify_delete_responsive.py` touch `jobs._deleting`, `jobs._shutdown`
  and `jobs._recover_on_restart`. `JobService` wraps the public surface only, so
  those tests keep working, but the private surface is not yet sealed.
* **Enqueue dedupe is still read-then-write** without a lock in `jobs.py`.
  Phase 1 does not change the job engine, so the race is inherited, not fixed.
* **`jobs.py` has a dead `"cancelled"` job-status branch** (`_aborted`, line
  ~887); `"cancelled"` is only ever an *exchange* status. `JobService` does not
  model a `cancelled` job status, matching reality rather than the dead branch.
