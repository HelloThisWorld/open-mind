---
name: code-graphs
description: Build deterministic structure / dependency / call / entry-point-flow graphs from a repository's real code, with source-traceable file nodes and definitions. No invented nodes or edges; ambiguous references are flagged, not guessed.
---

# Code graphs — deterministic structure map

## What it does
Performs line-oriented static analysis (language specs in `openmind.langspec`,
tree-sitter for Java where available) to recover, for ANY repository, a persisted
structure artifact: a module tree, a per-file definition index, entry points, an
import/dependency graph, and a name-based call/usage graph. From it, `openmind.diagrams`
projects interactive call graph data plus Mermaid / DOT diagrams.

## Determinism & anti-fabrication contract
- Graphs are recovered from facts in the code (defs, imports, call sites) — **never
  model-generated**.
- Internal import edges are resolved where the language makes it tractable;
  unresolved/external references are recorded as such, never invented.
- Call edges are name-based and **flagged `ambiguous`** when a symbol is defined in
  more than one file — the tool states uncertainty instead of guessing.
- File nodes and definitions carry source locations; directory/module nodes are
  rollups over real files. There is no path that fabricates an edge.
- Incremental: hash-keyed per file, so an unchanged file is not re-scanned.

## Invocation
- **REST:** `GET /structure?scope=` (overview: stats, entry points, top modules),
  `GET /graph?scope=` (call roots), `GET /graph/children`, and
  `GET /graph/node?scope=&id=` (a node's source location, defs, call neighbors,
  cross-linked glossary terms).
- **Library:** `openmind.structure.build_structure(...)`, `get_definition(doc, symbol)`,
  `term_usage(doc, term)`; `openmind.diagrams` for projections.

## Implementation / tests
`openmind/structure.py`, `openmind/diagrams.py`, `openmind/langspec.py`,
`openmind/javaparse.py`. Acceptance: `tests/verify_structure.py`, `tests/verify_diagrams.py`.
