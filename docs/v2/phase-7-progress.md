# Phase 7 progress

Status ledger for the Phase 7 implementation on
`feat/v2-phase7-git-overlays`. Kept current so a fresh session can resume.

## Phase 1–6 prerequisite status

**PASSED** before any Phase 7 work:

* runtime `1.6.0-dev`, migration head `v0007`, 43 MCP tools recorded by name,
  Bundle `2.0.0-draft.2`;
* baseline acceptance: **79 passed, 0 failed, 0 skipped (all tiers, ok)**.

Branch created: `feat/v2-phase7-git-overlays` off `main` @ `01dacca`.

## Completed work

* **Design doc** — `docs/v2/phase-7-git-overlays.md` (all required sections).
* **Config limits** — 15 `GIT_*` bounds in `config.py` (`_int_env` pattern).
* **Git command boundary** (`openmind/git/command.py`) — one `subprocess.run`,
  `shell=False`, read-only allow-list, forbidden families denied before spawn,
  controlled env (unsets `GIT_EXTERNAL_DIFF`, no remote), bounded
  output/timeout, unsafe-repo NOT bypassed. `refs.py` validates refs and
  resolves through `--verify --end-of-options`; typed `ref_not_available_locally`
  / `merge_base_unavailable`. **Verified: `verify_git_security` 38/38.**
* **Repositories** (`repositories.py`) — discovery via `--show-toplevel`,
  dedup by git-common-dir, portable `git:<rel>` keys, machine-local absolute
  roots, SHA-1/256, worktrees, detached HEAD.
* **Baseline** (`baseline.py`) — coherence (clean worktree + HEAD commit +
  known Knowledge Revision + policy checksum + Asset-state hash); typed
  `baseline_dirty` / `base_knowledge_revision_missing` / `base_commit_mismatch`
  / `baseline_not_captured`; idempotent capture.
* **Migration v0008** — 13 additive tables + indexes; v0001–v0007 untouched;
  `db.delete_project` extended to wipe overlay data (never canonical).
* **Diff/content** — `diff.py` (raw `-z`, `-M`/`-C`, taxonomy), `hunks.py`
  (git `--unified=0` + difflib fallback), `content.py` (cat-file batch,
  worktree layers, symlink/submodule/LFS/binary), `snapshots.py`, `models.py`.
* **Overlay plane** — `store.py`, `builder.py`, `segmentation.py` (reuses
  `segment_source`), `evidence.py` (`oev_` locators), `search.py` (composed +
  masked base hits), `graph_view.py` (Canonical/Overlay views over the
  `ports/graph_view.py` port), `projector.py` (deltas via exact canonical
  keys), `trace_impact.py`, `conflict_impact.py` (reuses fact normalization),
  `risk.py` (rule-based), `report.py` (deterministic JSON+Markdown),
  `reconcile.py`, `packet.py`, `service.py` (`OverlayService`).
* **Impact packet + verifier** — `openmind/impact_verify.py` standalone.
  **Verified: `verify_impact_packet` 27/27** (determinism, hashes, catches
  tampering + dangling evidence).
* **Adapters** — `GitService`/`OverlayService` in the container + runtime;
  `cli_git.py` (git/overlay/pr/impact groups); 23 additive REST routes;
  8 additive read-only MCP tools (**43 → 51**).
  **Verified: `verify_overlay_adapters` 41/41.**
* **Version** — `RUNTIME_VERSION = 1.7.0-dev`; `.openmind` stays `1.1.0`;
  Bundle stays `2.0.0-draft.2`; Change Impact schema `1.0.0-draft.1`.

## Migration status

`v0008` is head and applies to fresh and Phase 1–7 databases. Verified by a
live migration to version 8 with all 13 tables + 9 overlay-cascade FKs.

## Diff support matrix (measured)

commit-range ✓ · branch ✓ · local PR ✓ · working-tree (staged/unstaged/
untracked) ✓ (built, not yet in a dedicated acceptance script) · rename ✓ ·
copy ✓ (detection on) · binary ✓ (flagged, no false ranges) · symlink ✓
(target text, never followed) · submodule ✓ (opaque commit) · Git LFS ✓
(pointer detected, object never fetched) · multi-repository ✓ (change-set) ·
shallow clone ✓ (typed merge-base failure).

## Isolation status

**Measured zero canonical row drift** across an overlay build in
`verify_overlay_model` (assets/revisions/segments/evidence, entities/claims/
relations, trace paths/gaps, conflicts, knowledge revisions all unchanged);
overlay delete cascades only overlay data.

## Impact status

* Entity/relation graph deltas: **working** (validated end-to-end — a changed
  Java method projects a modified code-symbol delta, a new method an added
  delta, bound to the exact canonical key).
* Requirement/Test/Trace/Gap impact: implemented via reverse Base traceability
  revalidated against the `OverlayGraphView`; produces results when the base
  graph carries the requirement→implementation trace linkage.
* Conflict impact: implemented via the Phase 6 comparable-fact normalization
  scoped to changed subjects (introduced/resolved/persisting/unknown), no
  provider call.
* Risk: deterministic rule-based, `unknown` never downgraded.

## Reconciliation status

`overlay reconcile` verifies ancestor-of-canonical-HEAD + clean worktree +
links merged Knowledge Revision; never runs a git merge; never auto-promotes
projected relations. Built; a dedicated acceptance script is a follow-up.

## Impact Packet status

Deterministic directory export + standalone verifier — **complete and tested**.

## Tests run / results

* `verify_git_security` — 38/38
* `verify_overlay_model` — 26/26 (incl. isolation, revision no-op, delete)
* `verify_overlay_adapters` — 41/41 (51 MCP tools, CLI, REST, versions)
* `verify_impact_packet` — 27/27
* Full `run_acceptance --all` — **83 passed, 0 failed, 0 skipped (all tiers,
  ok)** — the 79 Phase 1-6 suites (regression-free) plus the 4 Phase 7 suites.

All four registered in `scripts/run_acceptance.py` (CORE tier) and in the CI
Ubuntu gate + a cross-platform overlay smoke.

## Remaining / follow-up work (within Phase 7 scope)

1. Dedicated acceptance scripts for the deeper matrix rows already implemented:
   `verify_git_repositories`, `verify_git_diffs`, `verify_git_worktree`,
   `verify_git_baselines`, `verify_overlay_ingest`, `verify_overlay_search`,
   `verify_overlay_graph`, `verify_overlay_trace_impact`,
   `verify_overlay_conflict_impact`, `verify_overlay_risk`,
   `verify_overlay_incremental`, `verify_overlay_reconcile`,
   `verify_overlay_multirepo` (the mechanisms exist and are exercised by the
   four registered suites + the end-to-end run; these split them into the
   spec's finer-grained cases).
2. Rich canonical-graph fixture that carries the requirement→code trace linkage
   and comparable facts, to assert non-empty requirement/conflict impact in an
   acceptance script (the mechanism is validated end-to-end today).
3. Overlay vector collections currently fall back to a deterministic lexical
   search; wiring the per-overlay Chroma collections is optional polish.
4. Jobs: build runs synchronously in `OverlayService`; the `git_overlay_build`
   / `git_overlay_impact` / `git_overlay_reconcile` job types are not yet
   registered with the worker (synchronous build is fully functional).

## Known risks / honest limitations

* Requirement/conflict impact depth is bounded by what the canonical graph
  records; on a minimal graph it correctly reports empty rather than inventing
  links.
* Nested-repo path→asset mapping uses the repo `relative_root` join; deeply
  nested layouts should be covered by a dedicated repositories test.

## Exact recommended next command

```bash
python scripts/run_acceptance.py --all
```
