---
name: glossary
description: Extract a project's term/acronym definitions VERBATIM from its own authoritative sources, each with file:line provenance and a grounded usage profile; resolve a term deterministically or return an honest "not found" — never generated.
---

# Glossary — deterministic, source-traceable term index

## What it does
Scans a repository's authoritative sources (a dedicated GLOSSARY/acronym file, a
definition table or `TERM: …` line, a README/docs sentence, a doc-comment) and builds
a persisted map: `term → {definition (verbatim), source_file, line_number,
content_hash, source_kind}`. A lookup resolves a term by **exact token** (no
similarity, no paraphrase) and returns the original text with a jump-to-source link,
or `found: false` for an absent term.

## Determinism & anti-fabrication contract
- The definition is the **exact original text** lifted from the source — never
  reworded, summarized, or model-generated.
- Provenance is mandatory: `source_file:line_number` + a `content_hash` of the source.
- An unknown term returns `{found: false, message: "no authoritative definition
  found …"}`. It is **never** guessed.
- Incremental + idempotent: a per-file content hash means an unchanged source is
  reused, not re-parsed; same input → same output.

## Grounded usage profile (value density)
On a single-term hit the entry carries a **usage profile derived from the structure
map** (`openmind.structure.term_usage`), all traceable to real code:
- `defined_at` — definition site(s) `file:line:kind` when the term is also a code symbol;
- `used_in` / `use_count` — every file that references the symbol;
- `modules` — the modules the term spans;
- `related_terms` — other glossary terms co-located in the same files.
For a pure concept/acronym (not a code symbol) the code lists are honestly empty; the
module and related terms still ground it.

## Invocation
- **MCP tool:** `get_glossary(scope, term=None)` — with `term`, the resolved entry;
  without, the full term list.
- **REST:** `GET /glossary?scope=<project>&term=<term>` (the entry, with `usage`).
- **Jump to source:** `GET /source?scope=&file=&line=` (allow-listed to indexed sources).

## Implementation / tests
`openmind/glossary.py` (extraction + lookup), `openmind/structure.py::term_usage`
(usage profile). Acceptance: `tests/verify_glossary.py`.
