---
name: code-graphs
description: Build deterministic structure / dependency / call / entry-point-flow graphs from a repository's real code, with every node traceable to a file:line. No invented nodes or edges; ambiguous references are flagged, not guessed.
---

# Code graphs — deterministic structure map

## What it does
Performs line-oriented static analysis (language specs in `openmind.langspec`,
tree-sitter for Java where available) to recover, for ANY repository, a persisted
structure artifact: a module tree, a per-file definition index, entry points, an
import/dependency graph, and a name-based call/usage graph. From it, `openmind.diagrams`
projects mind-map / Mermaid / DOT views for the Graphs UI.

## Determinism & anti-fabrication contract
- Graphs are recovered from facts in the code (defs, imports, call sites) — **never
  model-generated**.
- Internal import edges are resolved where the language makes it tractable;
  unresolved/external references are recorded as such, never invented.
- Call edges are name-based and **flagged `ambiguous`** when a symbol is defined in
  more than one file — the tool states uncertainty instead of guessing.
- Every node links back to a real `file:line`; there is no path that fabricates an edge.
- Incremental: hash-keyed per file, so an unchanged file is not re-scanned.

## Invocation
- **REST:** `GET /structure?scope=` (overview: stats, entry points, top modules),
  `GET /graph?scope=&kind=call|module|flow` (roots), `GET /graph/children`,
  `GET /graph/node?scope=&id=` (a node's source location, defs, call neighbors,
  cross-linked glossary terms).
- **Library:** `openmind.structure.build_structure(...)`, `get_definition(doc, symbol)`,
  `term_usage(doc, term)`; `openmind.diagrams` for projections.

## Implementation / tests
`openmind/structure.py`, `openmind/diagrams.py`, `openmind/langspec.py`,
`openmind/javaparse.py`. Acceptance: `tests/verify_structure.py`, `tests/verify_diagrams.py`.
