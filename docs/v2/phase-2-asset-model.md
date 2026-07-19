# OpenMind v2 — Phase 2: Workspace Asset, Revision, Segment and Evidence Foundation

Status: implemented on branch `feat/v2-phase2-asset-model`.
Runtime version introduced by this phase: **`1.2.0-dev`**.
Artifact export contract: **unchanged** — `.openmind` schema stays `1.1.0`.

This phase builds the canonical engineering-asset data foundation that every
later OpenMind v2 capability depends on. It is a **data-model foundation**, not
a feature release, and it deliberately implements **none** of the document
parsing, requirement/business-rule extraction, Claim/Relation tables or
Knowledge-Graph work (see [§13 Deferred work](#13-deferred-phase-3-work)).

It builds directly on the Phase 1 boundaries — `OpenMindRuntime`, the
`ServiceContainer`, the strict SQLite migration runner, the typed domain
errors, and the CLI / FastAPI / MCP adapters — and does not rewrite them.

---

## 1. Baseline: what already worked

Phase 2 started from a clean working tree at `20c1026` (branch `main`), with the
whole core acceptance suite green. Recorded before any change was made
(`python scripts/run_acceptance.py --json`, 24/24 core scripts):

| Script | Result | Script | Result |
| --- | --- | --- | --- |
| verify_migrations | 49 passed | verify_fixes | 7/7 |
| verify_structure | 14 passed | verify_fixes2 | 7/7 |
| verify_glossary | 20 passed | verify_resources | 15 passed |
| verify_router | 23 passed | verify_ask | 15/15 |
| verify_diagrams | 7 passed | verify_ask2 | 29/29 |
| verify_grounding | 8 passed | verify_async_delete | 6/6 |
| verify_artifacts | 31 passed | verify_delete_race | 26/26 |
| verify_runtime | 31 passed | verify_source_link | 25 passed |
| verify_services | 97 passed | verify_modelserver | 10/10 |
| verify_cli | 112 passed | verify_templates | 21/21 |
| verify_adapters | 85 passed | verify_facets | 22/22 |
| verify | 17/17 | verify_guide | 19/19 |

Result: `ok=true, 24 passed, 0 failed, 0 skipped`. Schema head before this phase:
**version 2** (`0001_baseline`, `0002_paths_sidecar`).

---

## 2. Current storage model (before this phase)

```
Project (projects row, state, meta)
├── paths            → machine-local sidecar (~/.openmind/paths.json)
├── file_index       → per-file SHA-1 hash + chunk-id list (incremental key)
├── map/*.json       → glossary, structure, facets (deterministic artifacts)
└── vector chunks    → Chroma collection (RAG projection)
```

The per-file **SHA-1 content hash** (`walker.hash_text`) is the incremental key:
an unchanged file is neither re-scanned nor re-embedded. There is **no record of
prior versions**: once a source file changes, its previous content is gone from
OpenMind's stores — the only surviving copy is the live file on disk.

## 3. Target canonical model (this phase)

```
Workspace (= the existing project row; id p_*)
└── Asset            one logical engineering object (a source/config file)
    └── AssetRevision   an immutable observation of that Asset's bytes
        ├── Segment       a stable structural unit inside one revision
        └── Evidence      a source-locatable citation for a segment
```

The Asset model becomes the **durable source of engineering-content identity**.
Chroma remains a *retrieval projection* built from the same source bytes; it is
not the canonical knowledge store. Historical content lives in an **immutable
content-addressed blob store** so Evidence for an old revision is still readable
after the source file changes.

"Workspace" is internal vocabulary. The stored entity is still a project, the
REST API still says `/projects`, and `workspace_id` **is** the existing `p_*`
project id. Nothing about the project row's shape changes.

---

## 4. Identity rules

* **Asset identity** = `(workspace_id, logical_key)`, enforced by a UNIQUE
  constraint. For a source file, `logical_key` = the **normalized
  workspace-relative path** (e.g. `src/services/order-service.ts`,
  `config/application.yaml`). It is produced by `machine.to_rel(workspace_id,
  abspath)`, exactly the same relativization the RAG index and glossary already
  use. **No absolute path ever enters the portable database** (zero-origin-traces
  constraint); the machine-local root stays in the sidecar.
* `source_path` is the same workspace-relative key; `source_kind` = `"file"`.
* **Asset type** is deterministic and extension/path based (§10), never a model.
* Two different files never merge into one Asset; one file never splits.

## 5. Revision rules

An `AssetRevision` is an immutable observation of an Asset's contents.

1. First observed content creates **sequence 1**.
2. Re-ingesting **unchanged** content creates **no new revision**.
3. Changed content creates the **next sequence**.
4. The new revision records `supersedes_revision_id` = the previous current
   revision; the previous revision's `status` becomes `superseded`.
5. Reverting to historically-seen content (A → B → A) **still creates a new
   revision** (sequence 3). The blob is reused; the transition is recorded.
6. A revision is **immutable** after creation (no UPDATE ever touches its row
   except the one-time `status → superseded` flip in rule 4).
7. `Asset.current_revision_id` points at the active revision.
8. Removing a source file sets `Asset.state = removed`; it does **not** delete
   any revision, segment, evidence or blob.
9. A reappearing logical key **reactivates the same Asset**; a new revision is
   created only when the observed content differs from the last current revision.

**Change signal.** The revision layer reuses the ingestion's existing per-file
change decision (the SHA-1 `file_index` comparison) to decide *when to look*, and
then compares the file's **canonical SHA-256** against the Asset's current
revision `content_hash` to decide *whether a new revision is required*. This
keeps revision creation in lockstep with RAG re-embedding without hashing the
whole corpus twice, while still letting a reappeared-but-identical file reactivate
its Asset without minting a redundant revision.

### 5.1 Revision status

Vocabulary: `unknown, draft, reviewed, approved, effective, superseded,
withdrawn, archived`. Code revisions in Phase 2 are created `unknown` — approval
authority is **not** inferred. The only automatic transition is
`unknown → superseded` when a newer current revision is created.

### 5.2 Source commit

The Git commit SHA is recorded when it can be determined **locally**, with no
network access and no failure outside a repo. A per-ingestion cache maps a repo
root → its resolved `HEAD` SHA (read once from `.git/HEAD` + refs, never by
shelling out per file). A dirty working tree is recorded in revision metadata
(`git_dirty: true`), never by inventing a SHA. When no Git data is available,
`source_commit` is `""`.

## 6. Content snapshot strategy

`openmind/content_store.py` — a small immutable, content-addressed blob store:

```
data/<workspace_id>/objects/<first-2-hex>/<sha256>
```

* `put(workspace_id, data: bytes) -> blob_hash` — atomic (`tmp` + `os.replace`),
  reuses an existing matching blob, returns the SHA-256 hex.
* `get(workspace_id, blob_hash) -> bytes` — re-hashes on read; a mismatch raises
  `ContentCorruption` (never returns silently-wrong bytes).
* `exists`, `verify` — presence and integrity checks.

Invariants: blob identity is SHA-256 of the exact bytes; the database stores only
the blob hash (never an absolute blob path); old revision blobs are retained;
binary bytes round-trip even when Phase 2 cannot parse them; blobs live inside
`data/<workspace_id>/`, so workspace delete removes them with the data dir and
workspace terminate removes them explicitly. Snapshots are **never** stored in
Chroma. The store is testable with no Chroma and no FastAPI.

## 7. Segment strategy

A `Segment` is a stable structural unit inside one revision. Phase 2 introduces
`openmind/segmentation.py`, a **deterministic** segmenter that shares its
boundary primitives with the RAG chunker (the tree-sitter helpers in
`javaparse.py` and the `CHUNK_MAX_LINES` / `CHUNK_OVERLAP_LINES` line-range split
used by `rag.chunk_file`). Producing canonical Segments and preserving the exact
current RAG chunk projection are decoupled: the segmenter emits Segment records;
`rag.chunk_file` is **left byte-for-byte unchanged**, so search behaviour,
chunk ids and chunk metadata cannot regress. A test asserts the two agree on line
ranges, which is what keeps them from drifting.

* **Java** (tree-sitter available and parse succeeds): one `type` segment per
  class/interface/record/enum (content mode `derived` — the signature summary,
  matching the class-summary retrieval chunk), one `method` / `constructor`
  segment per member (content mode `verbatim`). Real source line ranges.
* **Generic source / config / parse-failure**: deterministic bounded
  `file` line-range segments (`_split_lines(text, 1, CHUNK_MAX_LINES,
  CHUNK_OVERLAP_LINES)`), 1:1 with the current generic RAG chunks.

**Segment identity.** `segment_key` is stable within one revision:
`type:org.example.Client`, `method:org.example.Client#send(Request)`,
`file-range:000001`. Ambiguous/duplicate symbols get a deterministic
`@<start_line>` position suffix rather than a bare array index.

**Content mode.** `verbatim` for real source slices; `derived` for the generated
Java class summary — a generated summary is never misrepresented as verbatim
source. `segment.content_hash` is SHA-256 of the represented content.

## 8. Evidence rules

Every Segment gets exactly one Evidence row citing its source range:

```json
{ "kind": "source-range",
  "file": "src/services/order-service.ts",
  "startLine": 10, "endLine": 42,
  "symbol": "OrderService.create(Request)" }
```

* `file` is workspace-relative; line numbers are 1-based.
* `evidence.content_hash` = SHA-256 of the **verbatim** source slice at
  `[startLine, endLine]`, so it is recomputable from the immutable revision blob.
  For a `derived` segment the evidence still cites the **verbatim** class range.
* `excerpt` is a bounded, verbatim preview.
* No Evidence cites a file outside the workspace source roots.
* Evidence retrieval reports source state honestly, distinguishing:
  **current source matches / source changed / source missing / snapshot available /
  snapshot corrupt** — recovered from the immutable blob, never a running model.

## 9. Incremental-ingestion transaction boundary

Integrated into the existing hash-keyed ingest (`jobs._run_ingest` step 4), not a
rewrite. Per changed file, ordered for crash-recoverability:

1. Build Segment + Evidence drafts in memory (deterministic; no I/O).
2. Write the immutable content **blob** (idempotent, content-addressed).
3. Build + upsert the **vector projection** (existing `rag.embed_and_upsert`,
   batched across files, stable ids → idempotent).
4. In **one** SQLite transaction, commit `AssetRevision` + `Segments` +
   `Evidence`, flip the superseded status, set `current_revision_id`, and write
   the compatibility `file_index` row.

If the DB commit fails after the vector upsert, the job fails honestly; because
the blob and vector upsert are idempotent and `file_index` still holds the old
state, the next ingestion safely repeats them. **A revision is never made current
before its segments and evidence are committed**, so a partial revision can never
be observed.

### 9.1 Unchanged legacy backfill (mandatory)

A Phase 1 workspace has `file_index` rows, Chroma chunks and maps but no Asset
rows. On the first Phase 2 ingestion, **unchanged** files must receive
Asset/Revision/Segment/Evidence records **without being re-embedded** and while
**reusing** their existing Chroma data. The ingest loads an in-memory *asset
index* once (like `file_index`); a file that the `file_index` reports unchanged
but that has no in-sync Asset is backfilled through the blob + transactional
commit path **with the embedding step skipped entirely**. A test asserts the
embedding function is never called during an unchanged-legacy backfill.

### 9.2 Removed files

During prune (`jobs._run_ingest` step 5), a file no longer present:
delete its active vector chunks, delete its `file_index` row (existing
behaviour), and **mark its Asset `removed`** — preserving every revision, segment,
evidence and blob. Normal source deletion never erases Asset history.

### 9.3 Reappearing files

A removed logical key that reappears reactivates the same Asset (`state = active`)
and creates a new revision only when its content differs from the last current
revision; previous history is preserved.

### 9.4 Progress counters (additive)

`assets_created, assets_reused, assets_reactivated, assets_removed,
revisions_created, revisions_reused, segments_created, evidence_created,
content_blobs_created, content_blobs_reused`. No existing progress key is removed
or renamed.

## 10. SQLite schema (migration `v0003_asset_model`)

A new immutable migration; `v0001` / `v0002` are untouched. Tables:

* **`assets`** — `id, workspace_id, logical_key, asset_type, title, source_kind,
  source_path, media_type, state, current_revision_id, metadata_json,
  created_at, updated_at`; `UNIQUE(workspace_id, logical_key)`;
  `FOREIGN KEY(workspace_id) REFERENCES projects(id) ON DELETE CASCADE`.
* **`asset_revisions`** — `id, asset_id, sequence, content_hash, content_size,
  content_blob_hash, status, version_label, source_commit,
  supersedes_revision_id, metadata_json, created_at`;
  `UNIQUE(asset_id, sequence)`; FK → `assets(id) ON DELETE CASCADE`.
  `(asset_id, content_hash)` is deliberately **not** unique — A → B → A must be
  representable.
* **`segments`** — `id, revision_id, segment_key, segment_type, ordinal,
  start_line, end_line, symbol, content_hash, content_mode, metadata_json`;
  `UNIQUE(revision_id, segment_key)`; FK → `asset_revisions(id) ON DELETE CASCADE`.
* **`evidence`** — `id, revision_id, segment_id, locator_json, excerpt,
  content_hash, created_at`; FK → `asset_revisions(id)` and `segments(id)`,
  both `ON DELETE CASCADE`.

Indexes: workspace asset listing `(workspace_id, state)`; logical-key lookup
`(workspace_id, logical_key)` (covered by UNIQUE); current-revision + history
`(asset_id, sequence)`; segment symbol `(symbol)`; segment type
`(revision_id, segment_type)`; evidence-by-revision `(revision_id)` and
evidence-by-segment `(segment_id)`. No speculative Claim/Relation tables.

**Deletion.** With `PRAGMA foreign_keys=ON` (already set on the shared
connection), `db.delete_project`'s `DELETE FROM projects` cascades through
`assets → asset_revisions → segments → evidence`. `db.delete_project` also adds
explicit `DELETE FROM assets WHERE workspace_id=?` for clarity and for any future
caller that disables the pragma. Blobs are removed with the data dir by
`jobs._cleanup_deleted`.

## 11. Persistence, service and adapters

* **`openmind/ports/asset_repository.py`** — a narrow `Protocol` (the test seam),
  structurally satisfied by `openmind.db`.
* **`openmind/db.py`** — a focused, grouped "Asset model" section: `upsert_asset,
  get_asset, find_asset_by_logical_key, list_assets, count_assets, list_asset_index,
  create_revision, get_revision, list_revisions, set_current_revision,
  commit_revision (the single transactional writer), replace_segments_and_evidence_for_revision,
  list_segments, get_segment, get_evidence, mark_asset_removed, reactivate_asset,
  clear_workspace_assets`. Every revision+segments+evidence+current-pointer write
  is one transaction.
* **`openmind/services/asset_service.py`** — `AssetService`, exposed as
  `runtime.assets` and `ServiceContainer.assets`. Methods: `list_assets, get_asset,
  get_asset_by_logical_key, list_revisions, get_revision, list_segments,
  get_segment, get_evidence, stats, sync_file`. **Every read verifies the entity
  belongs to the supplied workspace** — an Asset id from workspace A is never
  readable through workspace B. Typed errors `AssetNotFound, RevisionNotFound,
  SegmentNotFound, EvidenceNotFound, ContentCorruption` map to HTTP/CLI through the
  existing inheritance-driven mapping (`http_status` / `exit_code`).
* **CLI** — an additive `asset` command group: `list, show, revisions, segments,
  evidence, add`; plus additive Asset counts on `status`. Same one-JSON-object
  stdout contract and exit codes.
* **REST** — additive read-only routes under the existing `/projects/{id}` tree
  (`/assets`, `/assets/stats`, `/assets/{id}`, `/assets/{id}/revisions`,
  `/revisions/{id}`, `/revisions/{id}/segments`, `/evidence/{id}`), plus an
  optional `POST /projects/{id}/assets/sync` delegating to `AssetService.sync_file`.
  Existing routes and the `/projects` naming are unchanged; cross-workspace access
  returns 404; lists are bounded.
* **MCP** — additive read-only tools `list_assets, get_asset, get_asset_revisions,
  get_evidence`. The existing nine tools are byte-for-byte unchanged; `mcp serve`
  still does not start the ingestion worker.

## 12. Compatibility requirements

* **REST**: all existing routes/response shapes operational; `/projects` not
  renamed; new endpoints additive.
* **MCP**: `search, route, dispatch, get_glossary, find_similar_cases, save_case,
  get_doc, propose_fix, apply_fix` retain names, arguments and response shapes.
* **Artifact export**: command and `.openmind` schema `1.1.0` unchanged; the Asset
  model is **not** exported yet.
* **Skill bridge**: JSON-lines protocol unchanged and independent of the app DB.
* **Existing databases**: migrate to head with no loss of projects, paths, jobs,
  file index, Ask history, cases, template metadata, maps or Chroma collections.
* **Glossary / structure / RAG / UI**: unchanged; the UI still loads existing
  workspaces (no UI rewrite).

## 12.1 Testing strategy

New acceptance scripts, each registered in `scripts/run_acceptance.py` (an
unregistered `verify_*.py` fails the runner) and gated in CI:

* **`verify_content_store.py`** — atomic write, blob reuse, changed→new blob,
  SHA-256 identity, corruption detection, no absolute path stored, binary
  round-trip, workspace cleanup.
* **`verify_asset_model.py`** — migration (tables/indexes, empty→head, legacy
  upgrade with no data loss, checksum immutability, no-op re-run, FK cascade,
  failed-migration rollback); first ingest (Assets/Revisions/Segments/Evidence,
  valid 1-based ranges, verbatim excerpt, workspace-relative paths); idempotency
  (0 new revisions, stable counts, blob reuse, **no re-embed**); change (one new
  revision, prior still queryable, historical evidence intact, supersedes chain);
  revert A→B→A (three revisions, blob reuse); removal (Asset removed, history
  kept); reappearance (same Asset reactivated); legacy backfill (**no embed**).
* **`verify_asset_cli.py`** — every `asset` command supports `--json`, one JSON
  object on stdout, cross-workspace access fails honestly, typed not-found,
  bounded lists, bounded evidence output.
* **`verify_asset_adapters.py`** — all old REST routes + MCP tools still present;
  new endpoints/tools work; cross-workspace reads 404; evidence locators are
  source-traceable.

Lifecycle regressions (`verify_async_delete`, `verify_delete_race`) must stay
green; delete stays responsive and the startup janitor race fix is preserved.

## 13. Deferred Phase 3+ work

Intentionally **not** implemented here: PDF/DOCX/XLSX parsing, OCR pipelines,
COBOL/JCL parsers, requirement/business-rule extraction, cloud LLM providers,
induced Project Lens, Claim/Relation tables, Knowledge-Graph edges,
requirement-to-code traceability, conflict detection, branch/PR overlays,
webhooks, Bundle 2.0, Titan Mind / Agent Skill Forge integration, new workflow
Skills, Neo4j, per-project Chroma-directory migration, a typed worker pool, and a
new UI. Document knowledge and requirement traceability **do not exist yet**;
nothing in this phase should be read as claiming they do.
