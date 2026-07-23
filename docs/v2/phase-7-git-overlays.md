# OpenMind v2 — Phase 7: Git Change Intelligence, Branch/PR Overlays and Evidence-Bound Impact Analysis

Status: implemented in this phase. Runtime version moves from `1.6.0-dev` to
`1.7.0-dev`; database schema head moves from v0007 to **v0008**; the artifact
schema stays `1.1.0`; the Knowledge Bundle draft schema stays `2.0.0-draft.2`
(unchanged — Git Overlays are ephemeral and never enter the canonical Bundle).
A new, separate **Change Impact Packet** draft schema `1.0.0-draft.1` is
introduced for exporting an overlay's impact.

Phase 7 adds a **Git Overlay plane**: an isolated, read-only projection of a
branch, pull request, commit range or working tree onto the canonical Base
Workspace. It answers one question — *"if this change landed, what canonical
engineering knowledge would it touch, break, or fix?"* — and it answers it
with the same evidence discipline as the rest of OpenMind. It never mutates
Git, never contacts a remote, and never writes into a single canonical table.

---

## 1. Canonical Base Workspace versus Git Overlay

The Base Workspace is everything Phases 1–6 built: Assets, immutable
Revisions, Segments, Evidence and content snapshots (Phase 2/3); the canonical
Engineering Knowledge Graph of Entities, Claims and Relations with Knowledge
Revisions and Human Decisions (Phase 5); the formal Traceability snapshot of
policy-verified trace paths, coverage, gaps and orphans (Phase 6); and the
canonical Conflicts (Phase 6). It is the single source of truth. It is written
only by ordinary ingestion, deterministic projection, explicit promotion and
governed decisions.

A **Git Overlay** is a derived, provisional view layered *on top of* a
coherent snapshot of that Base:

```text
Overlay = Base Workspace snapshot
        + Git file delta (base → head)
        + Overlay content snapshots (immutable before/after bytes)
        + Overlay Segments / Overlay Evidence
        + graph delta (added / modified / removed graph objects)
        + derived impact (requirements, tests, traces, gaps, conflicts, risk)
```

The overlay may **reference** Base objects by id (`base_asset_id`,
`base_entity_id`, `base_relation_id`, `base_trace_path_id`,
`base_conflict_id`, …). It may never alter them. Every overlay-derived result
carries the coordinates that make it reproducible and falsifiable:

```text
overlay_id
overlay_revision
base_knowledge_revision
base_traceability_run
base_policy_checksum
base Git commit
head Git commit (or worktree hash)
```

If you cannot name the Base coordinates a result was computed against, the
result is not trustworthy — so the model refuses to produce one.

## 2. Git Baseline coherence

An overlay is only meaningful if the Base graph it is compared against
actually corresponds to a known Git commit. The **canonical Git Baseline**
(`workspace_git_baselines`) is that pin. Capturing a baseline records, for one
repository, the tuple:

```text
commit_sha, tree_sha, branch_name, head_ref,
knowledge_revision, traceability_run_id, trace_policy_checksum,
graph_projector_version, trace_engine_version, asset_state_hash
```

A baseline may be captured only when the repository is locally available; the
worktree and index are clean; HEAD resolves to a commit; the workspace's
active Assets under the repository have current Revisions whose recorded
source commit is coherent with HEAD and report no dirty source metadata;
canonical graph staleness reconciliation has completed; the current Knowledge
Revision and Traceability Policy checksum are known; and a current
Traceability snapshot exists (or the baseline explicitly records that none
does). A dirty worktree is never silently baselined — capture is an explicit
command.

Overlay planning fails safely when the requested base commit does not match
the recorded baseline, with typed errors: `base_commit_mismatch`,
`base_knowledge_revision_missing`, `baseline_not_captured`, `baseline_dirty`.
OpenMind will not compute canonical Requirement impact against the wrong
commit.

## 3. Repository discovery

Repositories are discovered through the workspace's already-registered source
paths (machine-local, Phase 1). For each candidate path OpenMind runs
`git rev-parse --show-toplevel` (a read) to find the repository root, and
`--absolute-git-dir` / `--is-bare-repository` / `rev-parse --show-object-format`
to classify it. Discovery supports:

* a workspace rooted directly at a Git repository;
* several registered roots pointing into the same repository (deduplicated);
* nested independent repositories;
* Git worktrees where `.git` is a file, not a directory;
* detached HEAD;
* SHA-1 and SHA-256 object-format repositories.

OpenMind does not recurse into ignored build/vendor directories merely to
find repositories.

## 4. Multi-repository Workspaces

One workspace may contain several repositories (a service and its client, or a
set of microservices). Each is registered with a portable **repository key**:

```text
git:<normalized-workspace-relative-root>     e.g. git:.  git:services/namecheck
```

The absolute repository root is **never** stored in the portable SQLite
database. It is resolved at run time from machine-local configuration
(`openmind/machine.py`), exactly like every other absolute source path, which
keeps the `data/` directory copyable and origin-trace-free. Deduplication of
repositories that resolve to the same Git common directory happens in
machine-local memory; only the portable key is persisted.

A **change-set overlay** spans multiple repositories, each with its own base
ref, head ref and merge-base, but produces a single logical impact report.

## 5. Git command security boundary

There is exactly **one** Git subprocess boundary: `GitCommandRunner.run()` in
`openmind/git/command.py`. Every Git invocation in the entire codebase goes
through it. It:

* uses `subprocess.run` with `shell=False` and an explicit argument list
  (never a shell string);
* sets an explicit `cwd` (the repository root);
* enforces a bounded timeout and bounds captured output size;
* sets a controlled environment: `GIT_OPTIONAL_LOCKS=0`,
  `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `GIT_EXTERNAL_DIFF=` (empty),
  `GIT_CONFIG_NOSYSTEM=1`, `LC_ALL=C`, and clears credential/askpass helpers;
* rejects any subcommand not on an explicit allow-list.

**Allowed** command families (read-only): `rev-parse`, `rev-list`,
`merge-base`, `show`, `cat-file`, `diff`, `diff-tree`, `status`, `ls-files`,
`ls-tree`, `check-ignore`, `for-each-ref`, `symbolic-ref`, `check-attr`.

**Forbidden** (rejected before spawning a process): `checkout`, `switch`,
`reset`, `restore`, `clean`, `merge`, `rebase`, `cherry-pick`, `apply`, `am`,
`commit`, `tag`, `branch`, `fetch`, `pull`, `push`, `gc`, `repack`, `config`,
`remote`, `stash`, and anything else not explicitly allowed. OpenMind Phase 7
is a Git **reader**, not a Git operator.

The CLI, REST and MCP surfaces never accept arbitrary Git argument arrays.
They expose only *typed operations* (resolve a ref, summarize a diff, …) that
build the argument list internally.

## 6. Ref and object validation

Refs arriving from a caller are validated before use. Rejected: empty refs,
refs beginning with `-` (which would be parsed as options), refs containing
NUL or ASCII control characters, and refs Git reports as ambiguous. Commit
resolution uses the option-terminated form:

```text
git rev-parse --verify --end-of-options <ref>^{commit}
```

so a hostile value can never become a Git option and a ref is never
interpolated into a shell. No Phase 7 command contacts a remote; a missing ref
or missing merge-base returns an actionable typed error
(`ref_not_available_locally`, `merge_base_unavailable`,
`possibly_shallow_clone`) and never triggers a fetch.

OpenMind does **not** bypass Git's unsafe-repository protection: it never
writes `safe.directory`. An unsafe (wrong-owner) repository is reported as
such and the user must fix ownership explicitly.

## 7. Merge-base behavior

A branch or PR overlay compares the head ref against the **merge-base** of the
target branch and the head, not against the tip of the target branch. This is
the same three-dot semantics a code review uses: it isolates what the branch
*introduces* from what merely happened on the target in parallel. `merge-base`
is computed once per repository and stored on the overlay-repository row. A
commit-range overlay uses its explicit base ref directly. When no merge-base
exists locally (commonly a shallow clone), the overlay fails with
`merge_base_unavailable` / `possibly_shallow_clone` rather than guessing.

## 8. Committed overlays

Commit-range, branch and PR overlays operate entirely on committed objects.
Diffs are read with `git diff-tree`/`git diff` against commit SHAs; content is
read with `git cat-file --batch` without ever checking anything out. Committed
overlay Evidence stays valid even if Git later garbage-collects the object,
because the exact bytes are snapshotted into OpenMind's immutable content
store at build time.

## 9. Working-tree overlays

A working-tree overlay captures three layers **separately**:

```text
staged     = diff(HEAD → index)
unstaged   = diff(index → worktree)
untracked  = files Git does not ignore and does not track
```

The final "after" state is the worktree, but layer provenance is preserved on
every changed file and every piece of Evidence. Untracked files are included
only when not ignored, of a supported type, and within bounded size and count.
A deterministic **worktree hash** is computed from the HEAD commit, index
entries, staged blob hashes, and the content hashes of unstaged and untracked
files — never from timestamps alone — so an unchanged working tree produces an
identical hash and a no-op refresh. If the worktree hash differs between the
start and end of a build, the snapshot was inconsistent: the build is marked
`partial` (or retried once) with reason `working_tree_changed_during_analysis`
and is never reported as complete.

## 10. File-change taxonomy

Diffs are read from machine-readable, NUL-delimited Git output
(`--raw --numstat --name-status -z -M -C --no-ext-diff --no-textconv
--no-color`) — never localized human output. Supported change types: `added`,
`modified`, `deleted`, `renamed`, `copied`, `type-changed`, `unmerged`,
`submodule`, `unknown`. A working tree with `unmerged` entries cannot produce a
reliable impact report; it returns `working_tree_unmerged` rather than guessing
conflict contents. Commit metadata is bounded to SHA, parent SHAs, subject and
timestamp; author email addresses are not persisted by default.

## 11. Rename and copy behavior

Rename (`-M`) and copy (`-C`) detection is on by default and can be disabled
explicitly. Each renamed/copied file preserves `old_path`, `new_path` and the
similarity score. A **pure rename** (100% similarity, blob unchanged) is
reported as a *path change*, not as if the implementation changed — it may
still affect path aliases, package/module identity, build configuration and
source references, but it does not fabricate a modified method. Only files
whose parser-relevant content or logical path actually changed are re-parsed.

## 12. Binary, symlink, submodule and Git LFS handling

* **Binary**: detected via the diff's binary marker and a NUL-byte probe.
  Recorded with hashes and change metadata; never passed to a text parser or
  an embedding model; never given fake line ranges.
* **Symlink** (mode `120000`): the link *target text* is stored as the blob
  content; the link is never followed; `is_symlink=true`.
* **Submodule** (mode `160000`): the old/new submodule commit is recorded; the
  content is opaque; OpenMind never enters or fetches the submodule.
* **Git LFS**: a standard LFS pointer is detected structurally
  (`version https://git-lfs…`, `oid sha256:…`, `size …`). The pointer's oid
  and declared size are recorded (`is_lfs_pointer=true`); the actual LFS object
  is never downloaded.

Each of these is honest about being unanalyzable — they contribute `unknown`
risk, not a false clean bill.

## 13. Immutable Git Evidence

Every changed file's before and after bytes are snapshotted into the existing
content-addressed store (`content_store.put`) at build time. Overlay Evidence
(`git_overlay_evidence`, id prefix `oev_`) cites a range within one side of one
file via a locator:

```json
{ "kind": "git-blob-range", "repository": "git:services/namecheck",
  "overlayId": "ov_…", "overlayRevision": 2, "side": "after",
  "commit": "abc123…", "path": "src/main/java/NameCheckService.java",
  "startLine": 120, "endLine": 156,
  "symbol": "NameCheckService#execute(Request)" }
```

Working-tree Evidence uses `kind: "git-worktree-range"` with a `worktreeHash`,
a `baseCommit` and a `layer` (`staged`/`unstaged`/`untracked`). Rules: paths
are repository-relative; no absolute path enters portable SQLite; content is
recoverable from the immutable blob store; committed Evidence survives Git GC;
working-tree Evidence survives later file edits; before and after Evidence have
distinct ids; quotes and content hashes are verified. **Overlay Evidence is
never accepted by canonical Candidate Promotion** — it is provisional by
construction.

## 14. Overlay segmentation

The deterministic parsing primitives (code segmentation, the document parser
registry, the OpenAPI/JSON-Schema/SQL parsers) are refactored to operate on a
`(logical_path, bytes, media_probe, source_metadata)` tuple rather than
requiring a live filesystem path, so an overlay can parse a blob that was never
checked out. No parser logic is duplicated. Only added, modified, meaningfully
renamed, relevant copied, and supported working-tree files are parsed; deleted
files reuse their before-side Base/Git snapshot segments. Changed segments are
classified `added | modified | deleted | moved | context-changed | unchanged`.
A segment is `modified` when a changed line range intersects its source range
or its content hash changes — so an edit to one Java method does not mark every
method in the file modified; symbol identity is preserved where possible.

## 15. Overlay vector indexes and Base search composition

Overlay changed content is embedded into per-overlay collections
(`overlay_code_<id>`, `overlay_documents_<id>`, `overlay_knowledge_<id>`).
Unchanged Base chunks are never copied. **Composed search** searches the
overlay changed content and the Base content, then masks Base hits that belong
to deleted / modified / renamed-old / type-changed paths (the overlay's version
supersedes them), preserves Base hits for unchanged files, fuses results
deterministically, and labels every hit `base` / `overlay-before` /
`overlay-after`. The response is sectioned (`code`, `documents`, `knowledge`)
and reports `maskedBaseHits`. Existing Base search commands and MCP tools are
unchanged. Closing or deleting an overlay drops its collections through the
existing bounded, interruptible vector-drain path, so responsive deletion,
in-flight-drain protection, the startup janitor and deleting-workspace
protection are not regressed.

## 16. Overlay graph deltas

The overlay projects a graph **delta** by running the same deterministic
projector over the before and after states of changed content and diffing the
results. Entity delta types: `added | modified | removed | renamed |
unchanged | unknown`. Examples: a new Java method → *added* code-symbol; a
deleted method → *removed* code-symbol; a renamed file with the same method →
*modified* binding/path (not removed+added); an OpenAPI `POST`→`PUT` →
*removed* old Interface + *added* new Interface; a changed config value →
*modified* Configuration fact; a changed SQL column type → *modified*
Database-Object fact. Relation deltas cover only structurally generated
relations (`contains`, `calls`, `configures`, `publishes`, `consumes`). The
model is **never** used to infer new `implements` / `refines` / `verifies`
relations, and overlay deltas are never written into canonical graph tables.

## 17. Virtual graph composition

A read-only `GraphView` port (`openmind/ports/graph_view.py`) exposes the graph
read surface (`get_entity`, `list_entities`, `get_claim`, `get_relation`,
`list_relations`, `neighbors`, `evidence_for_relation`). `CanonicalGraphView`
wraps the Phase 5 store and returns byte-identical results to today's canonical
reads. `OverlayGraphView` composes the Base view with the overlay delta:

```text
Base active object + added delta + modified delta − removed delta
    = virtual overlay graph
```

No Base row is changed; removed Base objects disappear only from the virtual
view; modified objects keep a link to their Base object; new overlay entities
use overlay ids and canonical-key *candidates*; every returned node discloses
its origin (`base` / `overlay`). Phase 5 traversal and Phase 6 trace/conflict
readers accept a `GraphView` where practical, so the same code computes both
canonical and virtual results; canonical call sites keep using
`CanonicalGraphView`.

## 18. Requirement and Test impact

For each directly affected canonical implementation / interface / data /
workflow / configuration Entity, the analyzer uses **reverse formal
traceability** (Phase 6) to collect affected Requirement roots and their
current Base trace paths, then revalidates those traces in memory against the
`OverlayGraphView` and compares before/after. Requirement impact types include
`implementation-changed/removed/added`, `interface-changed`,
`configuration-changed`, `data-model-changed`, `trace-weakened`,
`trace-broken`, `trace-created`, `trace-unchanged`, `ambiguous-impact`. An
added code symbol is **never** reported as implementing a Requirement unless a
valid overlay relation supports it.

Test impact produces two distinct groups: **impacted tests** (canonical Test
Cases / Results whose Base trace paths include changed or removed objects) and
**recommended tests** (active canonical Test Cases connected to impacted
Requirements or implementation targets, even if the test itself did not
change). Tests are never executed and a recommended test is never claimed to be
sufficient verification.

## 19. Projected Gaps

Overlay trace paths are **not** persisted into canonical `trace_paths`. The
analyzer compares the Base formal trace with the virtual overlay formal trace
and records derived impact: paths introduced / removed / weakened /
strengthened, new stale paths, new/resolved ambiguity. Projected **Gap**
impact is `introduced | resolved | persisting | unknown` (e.g. a deleted test →
*introduced* missing-test; a deleted sole implementation → *introduced*
missing-implementation; an added valid test relation → *resolved* missing-test).
A projected Gap is never inserted into canonical Gap tables.

## 20. Projected Conflicts

The existing deterministic Conflict detectors (Phase 6) run against **only the
affected subjects** in the `OverlayGraphView` — never the whole virtual graph.
Results are classified `introduced | resolved | persisting | modified |
unknown` (e.g. config `3000`→`5000` while a Requirement still says `3000` →
*introduced* specification-code conflict; OpenAPI `PUT`→`POST` matching a
Requirement → *resolved* HTTP-method conflict; a binary interface file →
*unknown*). Projected Conflicts remain overlay records; they are never inserted
into canonical `engineering_conflicts`, and no conflict-governance action is
available until the change is actually merged and canonical graph
synchronization confirms it.

## 21. Deterministic risk policy

Risk is **rule-based, never model-scored**, over the ordered scale
`critical > high > medium > low > info` with a separate `unknown`. Summarized
rules: *critical* — an authoritative Requirement deleted, a verified
Requirement loses all implementation paths, an active critical Conflict gets
worse, or a new critical deterministic Conflict is introduced. *high* — a
verified Trace becomes broken, a required implementation/Interface/Test is
removed, an authoritative Interface or normative Claim changes, a high-severity
Gap is introduced, or a policy-marked security-sensitive configuration changes.
*medium* — a traced implementation/config/schema/topic changes, an inferred
relation becomes ambiguous, a medium Gap/Conflict is introduced, or a Test Case
changes. *low* — untraced implementation change, doc-only change without
canonical bindings, or a pure rename with no semantic identity change. *info* —
comment/metadata-only, new unbound documentation, or a formatting-only change
proven by normalized content equality. *unknown* — binary change, unavailable
LFS object, truncated analysis, unsupported parser, missing Base coherence, or
an unanalyzed submodule. **`unknown` is never downgraded to `low`.**

## 22. Overlay Revision semantics

Each overlay has its own monotonic `overlay_revision`, unrelated to the
canonical Knowledge Revision. The first successful build is revision 1; a
changed head ref or working-tree state produces the next revision; an unchanged
refresh produces none; a failed build never increments. Every report is stamped
with both the Base Knowledge Revision and the Overlay Revision. Old revisions
stay queryable until the overlay is deleted; closing preserves history;
deleting removes only overlay data.

## 23. Overlay staleness

The overlay's **source hash** is computed from the Git adapter version, overlay
builder version, repository ids, base/head commits and trees, merge bases,
worktree hash, raw file-change identities, before/after content hashes, Base
Knowledge Revision, Base Trace Policy checksum, graph-projector version,
trace-engine version, conflict-detector versions, and the bounded options. A
refresh with the same source hash is a no-op (no parse, no embed, no revision,
no report). When the head ref moves, only newly changed files are re-parsed and
a new revision is created. When the target Base commit or the canonical
Knowledge Revision advances, the overlay becomes **`stale`** and must be
explicitly refreshed/rebased — the old report is never silently reinterpreted
against a new Base. A changed Trace Policy checksum invalidates only Trace
impact; a changed detector version invalidates only Conflict impact; file/segment
work is reused where safe.

## 24. Post-merge reconciliation

OpenMind never merges a branch. After the user merges externally and updates
the canonical checkout, `overlay reconcile` verifies that the overlay head
commit is now an ancestor of (or its tree provably present in) the canonical
HEAD, that the canonical worktree is clean, and that canonical ingestion +
graph sync + staleness reconciliation + trace refresh (+ optional conflict
scan) have completed; it records the resulting Knowledge Revision and
Traceability run and links the overlay's changes to canonical Revisions. On
success the overlay state becomes `merged`. Projected overlay relations are
**never** auto-promoted — only canonical ingestion, deterministic projection
and explicit governance may alter the Base graph.

## 25. PR Impact Report format

The report is rendered deterministically from structured impact records — never
written by a model — as both JSON (schema `1.0.0-draft.1`) and Markdown.
Required sections: identity; baseline; head; repositories; commits; file
summary; changed files; changed segments; direct graph deltas; impacted
Requirements; impacted Interfaces/Data-Models/Configurations/Workflows;
impacted tests; recommended tests; Trace impact; Gap impact; Conflict impact;
risk summary; unknowns; limits and truncation; Evidence index. Every impact
statement carries a Base/Overlay object id, a reason, an Evidence id, a
confidence, a source side and a path/locator. The Markdown body is
byte-identical for identical overlay state (no embedded generation timestamp;
the manifest may carry one).

The **Change Impact Packet** (`openmind impact export`) writes a deterministic,
hash-manifested directory (`manifest.json`, `summary.json`, `report.md`, and
per-record JSON-lines files) with SHA-256 file hashes, deterministic ordering,
no absolute paths, no secrets, no author emails, no LFS contents, and explicit
partial/truncation state. A standalone verifier (`python -m
openmind.impact_verify`) re-checks hashes and referential integrity. It is
**not** a Feature Evidence Packet (Phase 9).

## 26. Compatibility guarantees

* Canonical tables (`assets`, `asset_revisions`, `segments`, `evidence`,
  `engineering_entities/claims/relations`, `trace_paths`,
  `traceability_gaps`, `traceability_coverage_snapshots`,
  `engineering_conflicts`) are never written by an overlay.
* Existing ingestion (`openmind ingest`, `asset add`, `document add`) keeps its
  public contract and its zero-cloud-call guarantee.
* Existing graph and traceability commands are unchanged; a Base query stays a
  Base query and overlay-aware queries are explicit.
* Phase 4 semantic analysis is never run automatically for changed files.
* All 43 existing MCP tools keep their names, arguments, response fields and
  read-only behavior; Phase 7 adds exactly 8 read-only tools (total 51).
* `.openmind schemaVersion` stays `1.1.0`; the Knowledge Bundle stays
  `2.0.0-draft.2`; the Skill Bridge stays unchanged and database-independent.
* Phase 1–6 databases migrate to v0008 without losing any data (v0008 is
  purely additive).

## 27. Testing strategy

Focused acceptance suites (`tests/verify_git_*.py`,
`tests/verify_overlay_*.py`, `tests/verify_impact_packet.py`) build **temporary
Git repositories** in-process — no network — with invented neutral history (a
`NameCheck` service with `REQ-NC-017`, a `POST /name-check` interface, a
`timeout=3000` config, a `NameCheckService.execute` implementation, a
`NameCheckTest` and a passing result) plus feature-branch variants (method body
changed, timeout `3000`→`5000`, `POST`→`PUT`, test deleted, implementation
added, file renamed, binary contract changed) and extra fixtures for
multi-repo, working-tree layers, submodule/LFS pointers, Unicode filenames and
shallow-clone merge-base failure. Every script is registered in
`scripts/run_acceptance.py`; an unregistered `verify_*.py` fails the manifest.
Security tests assert that every command sent to the runner uses `shell=False`,
only allowed families, `-z` output, rejects `-`-leading refs, does not bypass
unsafe-repository protection, enforces the timeout and bounds output, and
contacts no remote. Isolation tests assert overlay operations change zero rows
in every canonical table.

## 28. Explicitly deferred (Phase 8+)

Deferred by design: Git fetch/pull/push/checkout/merge/rebase/cherry-pick/patch
application and any working-tree mutation; automatic code modification, test
execution, semantic-provider calls, Candidate Promotion, graph mutation from an
overlay, or canonical Conflict creation from a projected conflict; GitHub API
auth, automatic PR fetching, PR comments and webhooks; GitLab/Bitbucket APIs;
CI merge-blocking; remote repositories without a local checkout; a cloud-hosted
Runtime; Bundle 2.0 schema freeze; Titan Mind integration; the Feature Evidence
Packet; Claude Code / Codex plugin packaging; Agent Skill Forge integration and
new Agent Skills; Neo4j/Cypher; a worker-pool replacement; and a complete
PR-review UI. Phase 8 productizes Claude Code/Codex integration and Verified
Agent Skills; Phase 9 introduces the enterprise governance UI and Feature
Evidence Packets.
