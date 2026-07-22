# Phase 6 progress

Status ledger for the Phase 6 implementation on
`feat/v2-phase6-traceability-conflicts`. Kept current so a fresh session can
resume with one command.

## Phase 5 prerequisite status

**PASSED** before any Phase 6 work (2026-07-23):

* `openmind/knowledge/`, `runtime.knowledge`, `ServiceContainer.knowledge`
  present; v0006 tables (`engineering_entities/claims/relations`,
  `knowledge_revisions/decisions/promotions`) present;
* candidate + relation-candidate promotion, graph search, bounded
  expansion, bounded path discovery, Bundle 2.0 Draft all working;
* runtime was `1.5.0-dev`, migration head v0006, 35 MCP tools recorded by
  name;
* baseline acceptance: **64 passed, 0 failed, 0 skipped (all tiers)**;
* manual fixture gate: 12/12 — including `semantic review confirm` does
  NOT promote, and explicit promotion + graph lookup work.

## Completed work

Everything in the Phase 6 scope (§5 items 1–39):

* **Migration** `v0007_traceability_conflicts` — 11 additive tables +
  indexes; v0001–v0006 untouched; delete/terminate wipe extended.
* **Policy model** — closed declarative schema + validator
  (`openmind/traceability/models.py`, `validator.py`); five built-ins
  (`generic-engineering`, `api-service`, `event-driven-service`,
  `batch-processing`, `japanese-v-model`); organization directory
  (`OPENMIND_TRACE_POLICY_DIR`, default `<data>/trace-policies`) —
  checksummed, invalid files listable, executable content rejected;
  workspace selection = Human Decision + one Knowledge Revision +
  snapshot/path invalidation.
* **Trace engine** (`engine.py`) — stage-legal traversal only; per-step
  evidence status; completeness = reached required stages / total required;
  deterministic confidence; ambiguity via inferred-only multi-target hops;
  bounded everywhere with disclosed truncation; requirement + reverse
  code/test builders; root eligibility.
* **Coverage/gaps/orphans** (`coverage.py`, `gaps.py`) — honest null
  percentages; policy-driven status + severities; 19 gap types; detection
  fingerprints; gap governance (accept w/ expiry, dismiss w/ suppression,
  reopen, resolve refused while detected unless engine-exception).
* **Incremental refresh** (`snapshots.py`) — no-op on unchanged
  revision×checksum×engine; affected-root reverse traversal; reuse +
  revalidation-stamp of unaffected roots; historical snapshots immutable;
  trace staleness reconciliation (stale/broken).
* **Conflicts** (`facts.py`, `detectors.py`, `conflicts.py`) — typed
  comparable facts with closed extractors and unit normalization (never
  guessed); six deterministic detectors; subject-level identity with
  supersession + suppression fingerprints; scan batches one graph
  transaction (identical re-observation mints nothing); full lifecycle
  doubly audited (conflict ledger + knowledge ledger); explicit
  candidate promotion (eligibility matrix, canonical reference resolution,
  transactional, idempotent).
* **Service + jobs** — `runtime.traceability` with the complete §26
  operation set; `traceability_refresh` + `conflict_scan` job types on the
  single worker; ingestion hook does a lightweight trace-staleness mark
  only.
* **Adapters** — `trace`/`conflict` CLI groups; additive REST under
  `/projects/...`; exactly 8 read-only MCP tools (35 → 43).
* **Bundle** — `2.0.0-draft.2`; opt-in `--include-traceability` /
  `--include-conflicts`; referential backfill; verifier extended (roots,
  relations, step ordering, conflict joins, coverage arithmetic,
  current-only staleness).
* **Version** — `1.6.0-dev`; `.openmind` stays `1.1.0`.

## Changed files

New: `openmind/traceability/` (13 modules),
`openmind/migrations/versions/v0007_traceability_conflicts.py`,
`openmind/cli_trace.py`, `tests/_traceability_helpers.py`, 15
`tests/verify_traceability_*|verify_conflict_*` suites,
`docs/v2/phase-6-traceability-conflicts.md`, this file.

Modified: `openmind/knowledge/vocabularies.py` (additive members),
`openmind/knowledge/bundle.py`, `openmind/bundle_verify.py`,
`openmind/services/service_container.py`, `openmind/runtime.py`,
`openmind/jobs.py`, `openmind/db.py` (delete wipe), `openmind/cli.py`,
`openmind/cli_knowledge.py` (bundle flags), `openmind/main.py`,
`openmind/models.py`, `openmind/mcp_server.py`, `openmind/version.py`,
`scripts/run_acceptance.py`, `.github/workflows/ci.yml`, `README.md`,
`docs/cli.md`, `docs/database-migrations.md`, and version/tool-count
assertions in `tests/verify_migrations.py`,
`tests/verify_knowledge_migration.py`, `tests/verify_document_cli.py`,
`tests/verify_document_adapters.py`, `tests/verify_adapters.py`,
`tests/verify_semantic_adapters.py`, `tests/verify_knowledge_adapters.py`.

## Tests run and results

Per-suite (all green as of the last focused runs):
migration 24, policies 34, paths 36, coverage 23, gaps 27, orphans 16,
incremental 19, conflict model 19, detectors 21, promotion 22, governance
23, conflict incremental 14, bundle 21, cli 47, adapters 30 — **376
Phase 6 checks**, plus three exploratory smokes (engine 25, service 52,
cli/bundle 31) during development.

Full acceptance (`--all`, 79 scripts): final run in progress at the time
of writing; every suite had individually passed.

## Remaining work

* Confirm the final full-suite run is green and commit.
* Final completion report (§44).

## Known risks

* Comparable-fact extraction is a closed pattern set by design — freer
  phrasings ("timeout at 4 seconds") are silence, not conflicts.
* Coverage recomputation for reused roots reconstructs summaries from
  persisted paths; per-stage `verified` granularity for reused roots is
  approximated by `reached` (documented in code).
* Orphan scans are bounded at 500 entities per kind per refresh and
  disclosed in the result.

## Exact recommended next command

```bash
python scripts/run_acceptance.py --all
```
