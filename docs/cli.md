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
`python -m openmind.mcp_server`, not a second copy of the tools. The nine tools
(`search`, `route`, `dispatch`, `get_glossary`, `find_similar_cases`,
`save_case`, `get_doc`, `propose_fix`, `apply_fix`) are unchanged.

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

The acceptance runner sets the offline/no-egress set for every test.

---

## See also

* [`docs/database-migrations.md`](database-migrations.md) — the schema ledger
* [`docs/v2/phase-1-core-foundation.md`](v2/phase-1-core-foundation.md) — why the
  runtime is shaped this way
