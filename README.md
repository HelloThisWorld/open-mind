<div align="center">

# Open Mind

**Turn any codebase into a deterministic, source-traceable knowledge index — so you can
understand an unfamiliar open-source project in an afternoon, not a month.**

[![Python](https://img.shields.io/badge/python-3.12+-3776ab.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Local-first](https://img.shields.io/badge/local--first-no%20telemetry-555.svg)](#design-principles)
[![MCP](https://img.shields.io/badge/MCP-stdio%20server-8a2be2.svg)](#use-it-from-an-editor-or-agent-mcp)
[![Determinism](https://img.shields.io/badge/facts-source--traceable-e8590c.svg)](#design-principles)

</div>

Open Mind reads a repository and builds persisted *artifacts* — a verbatim glossary, a
structure / dependency / call graph, and an exact-token code search — where **every fact links
back to the precise `file:line` it came from**. It does not invent: an unknown term returns an
honest "not found," never a guess. The same code runs on a single-module library or a
17,000-file polyglot monorepo, with nothing hardcoded to one project's shape.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Screenshots](#screenshots)
- [Project structure](#project-structure)
- [How it works](#how-it-works)
- [Seeing it on real projects](#seeing-it-on-real-projects)
- [Quick start](#quick-start)
- [Use it from an editor or agent (MCP)](#use-it-from-an-editor-or-agent-mcp)
- [Capabilities, as skills](#capabilities-as-skills)
- [Configuration](#configuration)
- [Testing](#testing)
- [Design principles](#design-principles)
- [Tech stack](#tech-stack)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Why this exists

Open source is how modern software is built, but getting productive in a large, unfamiliar
repository is slow: you grep, you guess, you read the same file five times before the shape
of the system clicks. LLM assistants promise to help — and often do — but they also
*hallucinate*: they will confidently describe a function, an API, or an acronym that is not
actually in the code. When you are trying to learn a system you do not yet know, a confident
wrong answer is worse than no answer, because you cannot tell which is which.

Open Mind takes the opposite stance. It moves judgment **out of the model and into
deterministic artifacts built from the real code**, then lets you navigate them:

- *What is "ISR" in this project?* → the exact sentence that defines it, with its `file:line`.
- *What calls `SocketServer`, and what does it depend on?* → a real call / dependency graph.
- *Where is retry-backoff handled?* → exact-token search over the indexed code.

Same input, same output. Nothing is generated from thin air, and unsupported claims are
blocked. The result is a reliable, navigable map of a project's real structure and vocabulary
— the thing you wish existed every time you open a repo you have never seen before.

---

## Features

- **Deterministic glossary** — term and acronym definitions lifted *verbatim* from a project's
  own authoritative sources, each with `file:line` provenance, a content hash, and a grounded
  usage profile (where it is defined, which files reference it, related terms).
- **Source-traceable graphs** — structure, dependency, call, and entry-point-flow graphs
  recovered from real code; ambiguous edges are flagged, never invented; every node links to source.
- **Exact-token + hybrid search** — bare identifiers match as exact tokens (never the substring
  of another symbol); natural-language queries use a vector + lexical hybrid.
- **Generic across languages and scale** — detect-then-adapt design; verified on Java, Scala,
  Go, Kotlin, TypeScript, JavaScript, and Python, from ~1k to ~17.5k files.
- **Local-first and audited** — project content never leaves the machine; every outbound request
  is policy-checked and logged.
- **Incremental and idempotent** — a per-file content hash means an unchanged file is never
  re-scanned or re-embedded; re-running on an updated checkout only touches what changed.
- **Portable index** — learned data stores only repo-relative paths, so it can be copied to
  another machine and still open.
- **MCP-native** — every capability is exposed as an MCP tool for your editor or agent.
- **Optional local LLM** — the glossary, graphs, and exact-token search are fully deterministic
  and need no model at all; a local model only adds grounded, source-cited answers.

---

## Screenshots

### Deterministic glossary, with provenance and a usage profile
![Glossary entry with provenance and usage profile](docs/screenshots/glossary.png)
*A glossary entry on Apache Kafka: the verbatim in-project definition, its source location with
a working jump-to-source link, and a usage profile derived entirely from the structure map.*

### Source-traceable code graphs
![Code graph with a node-detail panel](docs/screenshots/graphs.png)
*The call / usage mind-map for a project, with a node opening its real source location, its
neighbors, and cross-linked glossary terms.*

### Token-precise code search
![Token-precise code search](docs/screenshots/search.png)
*Searching the indexed code: exact-token precision for identifiers, hybrid retrieval for prose —
each result a real snippet with its `file:line`.*

---

## Project structure

The codebase is organized as small, single-responsibility modules along a clear pipeline, with
capabilities packaged as self-contained, callable **skills**. (Line counts give a sense of
scale; full acceptance tests live in `tests/`.)

```
openmind/
│
├─ Ingest pipeline ── build the deterministic index from ANY repo
│   ├─ walker.py        selection-aware filesystem walk (.gitignore, excludes, binaries, hashing)
│   ├─ detect.py        Stage 1 · project detection (archetype, languages, entry points)
│   ├─ langspec.py      extensible language registry — the "detect-then-adapt" backbone
│   ├─ javaparse.py     tree-sitter semantic parsing (Java)
│   ├─ structure.py     Stage 2 · structure map: modules, definitions, import + call graphs
│   ├─ glossary.py      deterministic, VERBATIM term/acronym index with provenance
│   ├─ diagrams.py      Stage 3 · structure-derived Mermaid / Graphviz / mind-map projections
│   ├─ rag.py           semantic chunking + hybrid exact-token / vector retrieval
│   ├─ tokenmatch.py    token-boundary matching (never substring-conflated)
│   ├─ embeddings.py    local embeddings (fastembed/ONNX, CPU or GPU) + offline hashing fallback
│   └─ vectorstore.py   Chroma per-project collections (+ pure-numpy fallback)
│
├─ Capabilities as skills ── composable, independently callable units
│   ├─ router.py        agent-style capability routing with a deterministic if-else floor
│   ├─ cases.py /        accumulating "solved cases" — the experience layer
│   │   memory.py
│   └─ wikienrich.py     audited Wikipedia enrichment (external definitions, pinned w/ source+date)
│
├─ Interfaces ── use it from a browser, an editor, or an agent
│   ├─ main.py           FastAPI REST + SSE + the single-page UI
│   ├─ mcp_server.py     MCP stdio server (search · get_glossary · route · dispatch · …)
│   ├─ ask.py /          grounded answer assembly — strictly source-cited, or "not in the sources"
│   │   conversation.py
│   └─ static/index.html zero-dependency web UI
│
├─ Local-first & safety ── nothing leaves the machine unaudited
│   ├─ netguard.py       outbound egress guard + audit log (loopback-only by default)
│   ├─ model_server.py   local llama-server lifecycle (attach-or-spawn, health, auto-recover)
│   ├─ llm_client.py     local OpenAI-compatible client
│   ├─ resources.py      RAM governor — a long ingest never swap-thrashes the machine to a freeze
│   └─ codemod.py        the ONE write path: a small find/replace, kept only if tests stay green
│
└─ Persistence & portability
    ├─ db.py             SQLite registry / jobs / file-index / cases (WAL, thread-safe)
    ├─ machine.py        machine-local source-root sidecar — keeps `data/` trace-free and copyable
    ├─ jobs.py           resumable, reconnectable background job engine
    ├─ mapio.py          atomic per-project map-artifact I/O
    └─ config.py         central configuration + system invariants

skills/                  SKILL.md capability units  (glossary · code-graphs · capability-router)
.claude/skills/          wikipedia-glossary skill   (SKILL.md + script)
tests/                   verify_*.py acceptance tests (determinism, anti-fabrication, async-delete, …)
```

Two design choices shape everything else:

- **Detect-then-adapt, not architecture-specific.** `detect.py` + `langspec.py` recognize a
  project's languages and shape at ingest time, so the structure map, glossary, and search work
  the same on a Java monolith, a Go service, or a TypeScript frontend — no per-repo configuration.
- **Capabilities are skills.** Each capability is a self-contained unit documented in standard
  `SKILL.md` form under `skills/`, and exposed as an **MCP tool** so it drops into an editor or
  agent unchanged.

---

## How it works

Pointing Open Mind at a repository runs a resumable background pipeline:

1. **Walk** — `walker.py` selects indexable files, honoring `.gitignore`, an exclude set, and
   built-in ignores; each file is content-hashed (the incremental key).
2. **Detect** — `detect.py` + `langspec.py` identify languages, archetype, and entry points.
3. **Structure** — `structure.py` recovers the module tree, a per-file definition index, and
   import + call graphs by deterministic static analysis (tree-sitter for Java).
4. **Glossary** — `glossary.py` extracts verbatim term/acronym definitions with provenance.
5. **Diagrams** — `diagrams.py` projects the structure map into Mermaid / DOT / mind-map views.
6. **Index** — `rag.py` chunks the code semantically and stores embeddings in Chroma for
   exact-token + hybrid retrieval.
7. **Enrich (optional, audited)** — `wikienrich.py` attaches authoritative external definitions
   to known terms as a separate, attributed field (source + timestamp), never replacing the
   in-project text.

Every stage is keyed by the per-file content hash, so a re-run on an updated checkout only
re-processes what actually changed.

---

## Seeing it on real projects

Here is the same pipeline run on three open-source repositories of increasing size and language
diversity, with no per-project tuning:

| Project | Languages | Files indexed | Definitions | Call edges | Modules |
|---|---|--:|--:|--:|--:|
| **Apache ZooKeeper** | Java, JS, TS, Python | 1,182 | 2,260 | 7,778 | 160 |
| **Apache Kafka** | Java, Scala, Python | 6,560 | 14,363 | 164,260 | 767 |
| **OpenClaw** | Go, Kotlin, TS, JS, Python | 17,515 | 128,141 | 675,310 | 736 |

The largest is a ~17.5k-file, five-language codebase with **675,000** recovered call edges,
built by the same deterministic pass that handles the 1,182-file project.

---

## Quick start

**Prerequisites:** Python 3.12+ (Windows / macOS / Linux). No database or external service to
set up — storage is local files. A local LLM is optional.

```powershell
git clone <your-fork-url> open-mind
cd open-mind
pip install -r requirements.txt

# Web UI + REST API on http://127.0.0.1:8077
./run.ps1                 # Windows
# or, cross-platform:
python -m uvicorn openmind.main:app --host 127.0.0.1 --port 8077
```

Open http://127.0.0.1:8077, point it at a local repository, let it learn, then browse the
**Glossary** and **Graphs** tabs and the search box. The glossary, graphs, and exact-token
search are fully deterministic and need no model.

> **GPU embeddings (optional, all local).** Indexing a large repo is faster on a GPU. On
> Windows AMD/Intel: `pip uninstall onnxruntime && pip install onnxruntime-directml`; on
> NVIDIA: `onnxruntime-gpu`. Open Mind picks the device automatically and, by default, will
> briefly free its own managed model server's VRAM for the embedding pass so the two never
> contend (see [Configuration](#configuration)).

---

## Use it from an editor or agent (MCP)

Open Mind ships an MCP stdio server, so the same capabilities are available to any MCP client
(editor, agent, CLI):

```bash
python -m openmind.mcp_server
```

Register it (client config shape may vary):

```json
{
  "mcpServers": {
    "open-mind": { "command": "python", "args": ["-m", "openmind.mcp_server"] }
  }
}
```

Exposed tools: `search`, `get_glossary`, `route`, `dispatch`, `find_similar_cases`,
`save_case`, `propose_fix`, `apply_fix`.

---

## Capabilities, as skills

Each capability is small, independently testable, and callable two ways — over REST (the web
UI) and as an **MCP tool** — under one discipline: *deterministic, source-traceable,
non-fabricating.*

| Skill | What it is | Determinism |
|---|---|---|
| [`glossary`](skills/glossary/SKILL.md) | verbatim term index + grounded usage profile | fully deterministic |
| [`code-graphs`](skills/code-graphs/SKILL.md) | structure / dependency / call / flow graphs from real code | fully deterministic |
| [`capability-router`](skills/capability-router/SKILL.md) | route a request to the right capability | model-assisted with a deterministic floor |
| [`wikipedia-glossary`](.claude/skills/wikipedia-glossary/SKILL.md) | authoritative external definitions, pinned with source + timestamp | deterministic write-back; egress audited |

The **router** shows how the model is used throughout: a deterministic if-else classifier always
decides first; when a local model is available it may refine the choice, but its answer is
*validated against the known capability set* and accepted only if valid — an unavailable or
off-spec model silently degrades to the deterministic rule. The model helps where it is
reliable; behavior never *depends* on it being right.

---

## Configuration

Everything works out of the box; these environment variables tune it. Nothing is required.

| Variable | Default | Purpose |
|---|---|---|
| `OPENMIND_DATA_DIR` | `./data` | Where the learned index lives (SQLite + Chroma + map files). |
| `OPENMIND_MACHINE_DIR` | `~/.openmind` | Machine-local sidecar holding each project's absolute source root (kept *outside* `data/` so the index stays portable). |
| `OPENMIND_EMBED_DEVICE` | `smart` | `cpu` · `smart` (GPU when the card is free, else CPU) · `auto` (always GPU if available) · `gpu`. |
| `OPENMIND_INGEST_FREE_GPU` | `1` | Briefly stop a managed model server during the embed pass to free VRAM for GPU embedding, then restart it. |
| `OPENMIND_EMBED_OFFLINE` | `0` | `1` forces the deterministic hashing embedder (no model download). |
| `OPENMIND_ENRICH_EGRESS` | `1` | `0` disables the audited Wikipedia enrichment lookup (fully offline). |

---

## Testing

Acceptance tests assert the contracts that matter — determinism, anti-fabrication, and the
concurrency edge cases — and most run with no heavy dependencies:

```bash
python tests/verify_glossary.py       # verbatim defs, provenance, honest "not found"
python tests/verify_structure.py      # deterministic structure map + incremental upkeep
python tests/verify_diagrams.py       # graphs are valid + honestly empty when there's nothing
python tests/verify_router.py         # routing floor + graceful model degradation
python tests/verify_resources.py      # RAM governor never freezes the machine
python tests/verify_async_delete.py   # delete is instant + never revives a project mid-cleanup
```

---

## Design principles

- **Deterministic and non-fabricating.** Definitions are lifted verbatim; graphs are recovered
  from real defs/imports/calls; ambiguity is flagged; absent facts return "not found." The
  guardrails are enforced in code (`tokenmatch`, `glossary`, `structure`), not just asserted.
- **Local-first and audited.** Project content never leaves the machine. The only model calls go
  to a local server pinned to loopback; every outbound request is policy-checked and logged by
  `netguard`. (One opt-in, audited exception fetches public reference definitions — single terms
  only, never project source.)
- **Verify before trusting.** The single code-write path (`codemod`) applies a change only if the
  project's own test suite stays green; the model never decides correctness, the tests do.
- **Generic.** Detect-then-adapt (`detect` + `langspec`) — works across languages and
  architectures with no per-repo configuration.
- **Portable.** Learned data stores only repo-relative paths; the absolute source root lives in a
  machine-local sidecar outside `data/`, so the index can be copied to another machine and still
  open.

---

## Tech stack

Python 3.12 · FastAPI + Uvicorn (REST/SSE) · a zero-dependency single-page UI · tree-sitter
(semantic parsing) · ChromaDB + fastembed/ONNX (local embeddings, with pure-Python fallbacks) ·
SQLite (WAL) · the Model Context Protocol (MCP) · an optional local `llama-server`.

---

## Roadmap

Open Mind is an index first; the long-term goal is to make any unfamiliar codebase feel
*already understood* the moment you open it — richer cross-project linking, deeper flow-level
views, and more capability skills that stay true to the same rule: **show the user the real
code, never a plausible fiction.**

---

## Contributing

Issues and pull requests are welcome. A good change keeps the project's core promise intact:

- New facts must be **traceable to source** and never fabricated; prefer deterministic
  extraction over model generation.
- Keep capabilities **self-contained and testable**; add a `verify_*.py` acceptance test.
- Keep it **local-first**: no new outbound network path without going through `netguard`.

---

## License

Released under the [MIT License](LICENSE).
