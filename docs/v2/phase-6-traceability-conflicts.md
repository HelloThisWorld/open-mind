# OpenMind v2 — Phase 6: Requirement Traceability, Coverage Gaps and Governed Conflict Resolution

Status: implemented in this phase. Runtime version `1.6.0-dev`; artifact schema
stays `1.1.0`; database schema head moves from v0006 to v0007; Knowledge Bundle
draft schema moves from `2.0.0-draft.1` to `2.0.0-draft.2` (still a draft, not
frozen).

Phase 6 builds **formal, evidence-backed engineering traceability** and
**governed conflict management** over the Phase 5 canonical Engineering
Knowledge Graph. It reads the canonical graph; it never becomes a second one.

---

## 1. A graph path is not a trace

Phase 5 ships generic reachability (`graph path`): any chain of active
relations between two entities, regardless of what the relations mean. That
surface is preserved unchanged.

A **formal trace path** is different: it is a graph path that satisfies a
selected **Traceability Policy** — every node maps to a policy stage, every
edge type is allowed for its stage transition, lifecycle and evidence are
verified, and traversal is bounded. The two never blur:

```text
generic:  Requirement -possibly-related-> Document -contains-> Code Symbol
formal:   NOT a Requirement-to-Code trace (possibly-related satisfies nothing;
          contains is not an implements)
```

A missing trace is not failure noise — it is returned as a **Gap**, which is
the governance product this phase exists to produce. The engine never invents
a link.

## 2. Traceability Policy model

Different systems have different lifecycles; the trace engine hardcodes none
of them. A policy is a **closed declarative document** (no executable
content), with:

* `schemaVersion` (`1.0.0`), `name`, `title`;
* `rootTypes`: entity types allowed as trace roots;
* `stages`: ordered stage list — each has `name` (from the closed stage
  vocabulary), `entityTypes` (closed Phase 5 entity types), `required`,
  and optionally `requiresEvidence`;
* `transitions`: `(from stage, to stage, allowed relationTypes)` triples —
  relation types are the closed Phase 5 vocabulary;
* `rules`: `allowInferredRelations`, `inferredRelationMaximumCount`,
  `allowPossiblyRelated`, `requireCurrentEvidence`, `requireActiveObjects`,
  `maximumDepth`, and coverage-status thresholds (see §8).

Policy sources, in precedence order for name resolution:

```text
workspace selection  ->  organization directory  ->  builtin
```

**Built-in policies** (conservative, shipped in code):
`generic-engineering`, `api-service`, `event-driven-service`,
`batch-processing`, `japanese-v-model`.

**Organization policies** are YAML or JSON files in a machine-local directory
(`OPENMIND_TRACE_POLICY_DIR`, or `<machine dir>/trace-policies/`). They are
schema-validated, checksummed, listable even when invalid (with their
errors), and rejected if they contain executable content, provider URLs or
secret-looking values. An invalid policy can be inspected but never selected.

**Checksum**: SHA-256 over the canonical JSON serialization (sorted keys) of
the policy definition. Everything a trace run produces is stamped with the
policy checksum, so a policy edit is always visible as a different checksum.

**Workspace selection** (`trace policy set`) stores one active policy per
workspace. Changing it:

* does not rewrite the graph;
* marks existing active trace paths, gaps and runs **stale**;
* requires an explicit `trace refresh` to rebuild;
* records a Human Decision **and one Knowledge Revision** (see §12 for why).

## 3. Trace stage vocabulary

Closed set (policy concepts, not entity types — one stage may allow several
entity types; arbitrary model-generated stages are never persisted):

```text
requirement  design  interface  data  workflow  implementation
configuration  verification  test-result  evidence  operation
```

## 4. Trace path validation

The validator is deterministic (`openmind/traceability/engine.py` +
`validator.py`); no provider is ever called. For a candidate chain:

1. map each entity to a stage via the policy (`entityTypes`); an unmapped
   entity type ends the walk (policies have no pass-through in the shipped
   set);
2. each hop must match a policy transition `(stage_i -> stage_j)`;
3. the hop's relation type must be in that transition's `relationTypes`;
4. relation state must be active-graph (`explicit`/`inferred`/`confirmed`;
   `rejected` edges are never traversed);
5. object lifecycle must be `active` when `requireActiveObjects` (stale
   endpoints/relations produce a **stale** path instead);
6. per-step evidence status is computed (see below);
7. inferred relations are counted; over
   `inferredRelationMaximumCount` (or any, when `allowInferredRelations` is
   false) the path is not valid;
8. `possibly-related` is usable only where a transition lists it AND
   `allowPossiblyRelated` is true (no built-in policy does either);
9. depth is capped by `min(policy.maximumDepth, MAX_TRACE_DEPTH=10)`;
10. required stages skipped by the walk become **gaps**, not invented hops.

**Relation direction.** Canonical relation rows have a natural direction that
differs per type (`design refines requirement`, `code implements interface`,
`test verifies code`). Stage transitions are validated on the **stage pair**,
accepting the underlying relation in either orientation; each persisted step
records the relation's true direction (`forward` = relation source is the
earlier stage). The stage typing prevents nonsense matches: an edge only
counts when one endpoint maps to the from-stage and the other to the
to-stage, and the relation type is allowed for exactly that transition.

**Per-step evidence status** (`current` / `stale` / `missing`): a relation's
evidence joins are resolved against the immutable store; evidence whose
revision is no longer any active asset's current revision is `stale`;
a relation with no joins reports `missing` (deterministic `explicit`
projection edges are anchored by bindings instead and report `current`
while their own lifecycle is active — reconciliation stales them when
sources move). With `requireCurrentEvidence`, any `stale` step makes the
whole path **stale**.

**Completeness** = `reached required stages / total required stages` of the
policy (the root stage counts; optional stages never inflate or deflate the
number). Range 0.0–1.0, exact formula tested.

**Confidence** (deterministic, never model-scored):

```text
high    complete path; zero inferred relations; all step evidence current;
        no ambiguity flag
medium  complete path; inferred relations within the policy bound; evidence
        current
low     everything else that is still a path (partial, ambiguous endpoint,
        stale evidence tolerated by policy, informational-authority root)
```

**Ambiguity.** A stage hop is ambiguous when the only way to reach the stage
is via `inferred` relations AND more than one distinct target entity is
reachable that way (the deterministic projector preserves ambiguous call
edges rather than guessing an owner). Such paths get status `ambiguous` and
an `ambiguous-target` gap; one `explicit`/`confirmed` edge to a single
target dissolves the ambiguity (alternates remain listed as alternates).

**Path statuses**: `verified` (valid, complete to its kind's target stage,
evidence current), `partial`, `ambiguous`, `stale`, `broken` (a previously
persisted path whose object no longer resolves), `unsupported` (only
reachable by violating relation semantics — reported, never counted as
coverage).

**Ordering** of returned paths is total and deterministic:
status rank (verified < partial < ambiguous < stale) → higher completeness →
higher confidence → fewer inferred relations → shorter → path id.

## 5. Trace builders

Three typed builders, all workspace-scoped, all bounded, all policy-driven:

* **`trace_requirement`** — root must belong to the workspace, have a
  root-eligible entity type, be active, have ≥1 active claim and evidence.
  An informational/non-authoritative root is allowed and disclosed
  (`root_authority`), never silently discarded (§7.6). Returns bounded paths
  per target stage (best path per target first), stage coverage, gaps,
  ambiguities, truncation flags and limits.
* **`trace_code`** — accepts implementation-stage entities (code-component,
  code-symbol, configuration, database-object, message-topic); walks the
  policy transitions in reverse for upstream (requirements, design,
  interfaces, workflows) and forward for downstream (tests, test results,
  evidence). A `calls` edge is never a requirement link — `calls` appears in
  no built-in transition.
* **`trace_test`** — accepts test-case / test-result entities; returns
  verified requirements, implementation targets, supporting evidence, and
  `untraced: true` when no requirement path exists (an orphan test is a
  fact, not an error).

## 6. Gap taxonomy

```text
missing-design            missing-interface      missing-data-model
missing-workflow          missing-implementation partial-implementation-only
missing-configuration     missing-test           missing-test-result
missing-evidence          stale-path             broken-path
ambiguous-target          unsupported-relation   authority-gap
orphan-requirement        orphan-code            orphan-test
orphan-document
```

Severity (`info` / `low` / `medium` / `high` / `critical`) is
**policy-driven and deterministic** — a table in the policy (overridable by
organization policies within the closed severity set), with these shipped
defaults:

```text
missing-implementation -> high        missing-test        -> high
missing-test-result    -> medium      missing-evidence    -> medium
missing-design (required stage) -> high; optional stage absent -> no gap
ambiguous-target       -> medium      stale-path          -> high
broken-path            -> high        orphan-requirement  -> high
orphan-code            -> info        orphan-test         -> low
orphan-document        -> info        authority-gap       -> low
unsupported-relation   -> medium      partial-implementation-only -> medium
missing-interface/-data-model/-workflow/-configuration (required) -> high
```

A gap row records: root entity, stage, gap type, severity, reason, the
blocking objects (JSON: completed stages, blocking relation/ambiguity ids,
relevant evidence), knowledge revision, policy checksum, and a
**detection fingerprint** (hash of root + stage + type + sorted blocking
object identity) used for suppression (§9).

**Gap lifecycle**: `open` → `resolved` (a later refresh no longer detects
it) / `accepted` (intentional, with actor, bounded note, optional expiry) /
`dismissed` (false positive, suppression fingerprint recorded) → `reopen`
(explicit, or automatic when an acceptance expiry passes or the fingerprint
no longer matches the re-detected gap). Governance never fabricates the
missing object; explicit `resolve` is allowed only with actor + note +
supporting knowledge revision and is refused while the engine still detects
the gap (unless the documented engine-exception reason is supplied).
Accepted gaps are excluded from the unresolved count and reported in their
own bucket; expired acceptances reopen on the next refresh or read.

## 7. Orphans

Explicit queries, not side effects:

* `find_orphan_requirements` — active root-typed entities with no valid path
  to any required implementation-stage;
* `find_orphan_code` — implementation-stage entities with no valid upstream
  requirement path; always `orphan: true, classification: "untraced"`,
  never `invalid` (framework/utility code is not a defect);
* `find_orphan_tests` — test-cases with no valid upstream requirement or
  implementation path;
* `find_orphan_documents` — document entities carrying promoted engineering
  claims but zero active canonical relations into the engineering graph.

## 8. Coverage

Computed per refresh over **current** (non-stale) paths and gaps, at
workspace, requirement, stage, entity-type and authority levels. Metrics
(each as `{count, numerator, denominator, percentage}`):

total requirements; with design; with interface/data; with implementation;
with full implementation; with tests; with test results; with current
evidence; fully traced; partially traced; untraced; stale traces; ambiguous
traces; orphan code objects; orphan test objects.

**Zero denominators are honest**: `percentage: null`, never a fabricated 0
or 100. Coverage **status** (`healthy` / `warning` / `critical` /
`unknown`) is policy-driven: thresholds live in the policy's rules
(`coverageStatus: {healthyMinimumPct, warningMinimumPct}` applied to the
fully-traced percentage, plus "no open critical gaps" for healthy). No
global hardcoded enterprise threshold; a workspace with zero requirements is
`unknown`.

Snapshots are historical records: a completed refresh writes one
`traceability_coverage_snapshots` row (metrics JSON + knowledge revision +
policy checksum + engine version + scope + limits + truncation). Old
snapshots are never overwritten or deleted.

## 9. Deterministic conflicts

### 9.1 Comparable facts

Detectors compare **typed comparable facts**, never arbitrary prose:

```text
subject_key  property  operator  value  unit  value_type
source_claim_id  evidence_id  authority_status
```

Value types: string, integer, decimal, boolean, duration, size, count,
http-method, api-path, data-type, identifier, enum-set.

Extraction is a closed set of deterministic extractors over canonical
claims and entity keys:

* structured attributes stored in claim metadata (promoted Phase 4
  candidates carry an `attributes` map);
* strict patterns over claim statements for closed properties
  (timeout/latency with explicit units, retry/maximum counts, HTTP method +
  path declarations, `key=value` configuration forms, SQL/JSON type
  declarations, boolean obligations in closed forms);
* interface entity canonical keys (`interface:POST:/name-check`);
* configuration entity keys and values.

Normalization: ms/s/min durations; explicit byte units; case-normalized
HTTP methods; API paths (trailing slash, case of the literal segments
preserved, `{param}` placeholders unified); boolean synonyms from a closed
list (`true/yes/enabled/on`, `false/no/disabled/off`); integer/decimal
formatting. **A missing unit is never guessed** — the fact keeps
`unit: ""`, and a comparison between a unitless and a united value returns
`not-comparable`, which is silence, not a conflict.

### 9.2 Detector SPI

```python
class ConflictDetector(Protocol):
    name: str
    version: str
    categories: set[str]
    def plan(self, workspace_id, knowledge_revision) -> ConflictDetectionPlan
    def detect(self, context) -> list[ConflictDraft]
```

Rules: deterministic; no provider call; bounded comparisons (facts are
grouped by normalized subject key + property, never O(n²) across unrelated
claims); every draft carries evidence joins; detectors report their
omissions and limits in the plan; one detector's failure produces a partial
run with an explicit per-detector error and never corrupts existing
conflicts.

### 9.3 Shipped detectors

| detector | fires only when |
|---|---|
| document-document | two active claims share a stable identifier subject or normalized subject key (or an explicit supersession/authority context) and expose structurally incompatible values of the same type/unit dimension |
| requirement-design | requirement and design claims are canonically related (refines/derived-from chain) and a deterministically extracted attribute differs |
| specification-code | a documented fact and a code/config/interface fact share subject+property and differ (endpoint method/path, configuration value, topic name) |
| requirement-test | requirement and test-case are canonically linked (verifies chain) and comparable expected values differ; a missing test is a Gap, never a Conflict |
| interface-schema | two interface/data-model descriptions of the same operation/schema differ structurally: missing field, extra required field, type mismatch, nullable mismatch, method/path mismatch, response-code mismatch |
| revision-authority | multiple active claims share a stable subject key, at least one is explicitly authoritative, comparable values conflict, and supersession does not resolve it; authority is never inferred |

### 9.4 Conflict identity, dedup and suppression

Conflict identity = SHA-256 over
`(workspace, category, normalized subject key, sorted conflicting object
ids, normalized property, detector name)` stored as `dedup_key`. A repeated
scan that observes the identical conflict updates `last_observed` metadata
on the existing row and **does not** create a new conflict or a new
Knowledge Revision. When the compared values or evidence change, the old
conflict is **superseded** (kept) and a new one is created. A dismissed
conflict stores a **suppression fingerprint** (hash of the compared values +
evidence quote hashes); an unchanged re-detection stays suppressed, a
changed one no longer matches the fingerprint and creates a new open
conflict.

### 9.5 Conflict lifecycle

```text
open -> under-review -> accepted-risk | resolved | dismissed
open/any <- reopen (explicit, or accepted-risk expiry)
any -> superseded (values changed) ; any -> stale (sources moved on)
```

* **accept-risk** requires actor + bounded note; optional expiry date and
  follow-up reference. An expired accepted risk reopens on the next scan or
  read-side reconciliation — it is never silently accepted forever.
* **resolve** requires actor + note + resolution type
  (`left-correct` / `right-correct` / `both-updated` / `superseded` /
  `false-positive` / `other`) + supporting evidence. Resolution **never
  rewrites claims or relations**; the human makes the graph governance
  changes separately, and a later scan confirms the incompatibility is gone.
* **dismiss** requires a reason and records the suppression fingerprint.
* **reopen** is explicit or deterministic (expiry / changed facts).

Every action writes one `engineering_conflict_decisions` row AND one Phase 5
`knowledge_decisions` row inside one graph transaction — so every conflict
decision is globally auditable in the same ledger as all other canonical
governance, and carries one Knowledge Revision.

## 10. Conflict Candidate promotion

Phase 4 `semantic_conflict_candidates` remain proposals. Phase 6 adds the
one explicit bridge (mirroring Phase 5 candidate promotion):

Eligibility (all required, no bypass flags):

```text
review_status = confirmed        lifecycle_status = active
evidence_status = verified       category supported by the canonical model
all referenced graph objects resolve in this workspace
all evidence belongs to this workspace
not already promoted
```

`plan_conflict_promotion` is a deterministic dry-run. `promote` re-checks
everything inside the graph transaction, verifies quotes against the
immutable store, creates the conflict + object joins + evidence joins,
records promotion provenance (`promoted_from_conflict_candidate_id` + a
`knowledge_promotions` row with candidate kind `conflict-candidate`),
records the initial decision, and mints exactly one Knowledge Revision.
Promotion is idempotent — promoting twice returns `already-promoted` with
the existing conflict. The candidate itself is never mutated into the
conflict and remains queryable in the Phase 4 store.

## 11. Incremental recomputation

Everything derived is stamped with three coordinates:

```text
Knowledge Revision   ×   policy checksum   ×   TRACE_ENGINE_VERSION
```

**No-op**: a refresh where all three match the latest completed run creates
no run, no snapshot and no writes unless `--force`.

**Affected-root calculation** when the knowledge revision advanced from M to
N: objects with `updated_knowledge_revision > M` are collected (entities,
claims, relations — indexed reads); a changed requirement affects itself; a
changed relation/claim/implementation/test object affects the requirement
roots that can reach it within a bounded reverse traversal; a policy or
engine-version change affects every root. Unaffected roots' current paths
and open gaps are **reused** (revalidation-stamped with the new run id, path
hash unchanged); affected roots' current paths are marked stale and rebuilt.
An alias-only change touches no path (aliases are not path objects).

**Staleness**: when the graph moves on, old snapshots stay queryable, stale
paths keep their rows (`stale_at` set, status `stale`), and current coverage
is computed only over current paths. `reconcile_staleness` also runs the
Phase 5 graph reconciliation first so traces never sit on knowledge whose
sources moved.

## 12. Knowledge Revision interaction (the chosen rule)

```text
trace refresh / coverage snapshot / gap detection     -> NO Knowledge Revision
   (derived analysis; stamped WITH the revision it analyzed)

conflict create (scan batch) / promote / resolve /
dismiss / accept-risk / reopen / supersede             -> ONE Knowledge Revision each
   (canonical governance writes, per the Phase 5 discipline:
    one logical transaction = one revision; a scan that creates
    several conflicts is one logical write = one revision;
    a scan that observes only identical conflicts writes nothing)

trace policy change                                   -> ONE Human Decision + ONE Knowledge Revision
   (policy selection governs how canonical knowledge is interpreted;
    Phase 5 requires every Human Decision to live inside a graph
    transaction, and a graph transaction that records a decision mints
    exactly one revision — so policy changes enter the ledger)

gap accept / dismiss / reopen / explicit resolve       -> ONE Human Decision + ONE Knowledge Revision
   (same reasoning: governance judgements, auditable in the one ledger)
```

Derived trace paths never increment the revision; conflict and governance
writes always do.

## 13. Storage (migration v0007)

New tables (see `openmind/migrations/versions/v0007_traceability_conflicts.py`;
migrations v0001–v0006 are untouched):

`workspace_traceability_policies`, `traceability_runs`, `trace_paths`,
`trace_path_steps`, `trace_path_evidence`, `traceability_gaps`,
`traceability_coverage_snapshots`, `engineering_conflicts`,
`engineering_conflict_objects`, `engineering_conflict_evidence`,
`engineering_conflict_decisions` — exactly the logical schema of the Phase 6
specification, plus `dedup_key` / `detection_fingerprint` /
`suppression_fingerprint` columns where §9.4/§6 need indexed identity, and
indexes for workspace/root, workspace/target, path status, gap type/status,
knowledge revision, policy checksum, staleness, conflict dedup key and
conflict status.

Run statuses: `planned running partial done failed cancelled stale`.
Path kinds: `requirement-to-design requirement-to-interface
requirement-to-implementation requirement-to-test requirement-to-evidence
code-to-requirement test-to-requirement`.

Traceability rows **reference** canonical objects by id and never duplicate
their semantic content (statements, evidence text, entity descriptions stay
in their canonical tables).

## 14. Services, jobs and adapters

**Service**: `runtime.traceability` / `ServiceContainer.traceability`
(`openmind/traceability/service.py`) exposing exactly the operation set of
the specification (§26): policies, refresh planning/execution, the three
trace builders, path/coverage/gap reads, gap governance, orphan queries,
conflict scan/promotion/governance, staleness reconciliation. Every
operation validates the workspace first; nothing resolves across
workspaces.

**Jobs**: two new types on the existing single worker —
`traceability_refresh` (steps: planning, selecting-roots, building-paths,
validating-paths, calculating-coverage, detecting-gaps, detecting-orphans,
persisting-snapshot, done) and `conflict_scan` (planning,
collecting-comparable-facts, running-detectors, verifying-evidence,
deduplicating, persisting-conflicts, reconciling-conflicts, done). Payloads
carry identifiers and options only; jobs are persisted, cancellable,
restart-safe, bounded, and provider-free. A detector failure yields an
honest `partial` run.

**CLI**: new `trace` and `conflict` command groups (§31 of the
specification), same contract as every existing command: one JSON object on
stdout with `--json`, diagnostics on stderr, no ANSI in JSON mode, stable
exit codes, bounded output, no secrets, and explicit `--actor`/`--note` on
every write.

**REST**: additive routes under `/projects/{id}/traceability/...` and
`/projects/{id}/conflicts...` (§32) — typed operations only, no generic
mutation endpoint, all lists bounded. No existing route changes.

**MCP**: exactly eight additive **read-only** tools —
`trace_requirement`, `trace_code`, `trace_test`, `get_trace_path`,
`get_traceability_coverage`, `list_traceability_gaps`,
`list_engineering_conflicts`, `get_engineering_conflict` — bringing the
verified Phase 5 count of 35 to 43. No MCP tool changes policies, refreshes,
scans, promotes, resolves or mutates anything; writes remain CLI/REST verbs
a human can see.

## 15. Bundle 2.0 Draft extension

Schema advances to `2.0.0-draft.2` (still draft; no freeze). New files:

```text
traceability-policies.jsonl   traceability-runs.jsonl
trace-paths.jsonl             trace-path-steps.jsonl
trace-gaps.jsonl              coverage-snapshots.jsonl
conflicts.jsonl               conflict-objects.jsonl
conflict-evidence.jsonl       conflict-decisions.jsonl
```

Export modes: the existing `--current-only` / `--include-history` /
`--knowledge-revision` are respected; new opt-in flags
`--include-traceability` and `--include-conflicts` add the new files
(omitted = Phase 5 bundle layout plus empty-file-free manifest). A
current-only export includes the latest non-stale trace snapshot for the
exported revision, current gaps, and open/under-review/accepted-risk
conflicts. The standalone verifier is extended to check: trace root/target
entities exist, trace relations exist, steps are densely ordered, gap roots
exist, conflict objects and evidence exist, conflict decisions reference
existing conflicts, policy checksums are present, coverage metrics are
internally consistent (numerator/denominator/percentage arithmetic), and
current-only bundles contain no stale snapshot. No provider prompts, raw
model responses or semantic cache ever enter the bundle.

## 16. Compatibility

* the Phase 5 canonical graph is the **only** graph store — no second
  entity/claim/relation table, no external graph database, no Cypher;
* Phase 4 candidates remain proposals; only confirmed+active+verified
  conflict candidates can be promoted, each by explicit action;
* `graph path` remains generic reachability, unchanged;
* ordinary ingestion stays deterministic and cloud-free (it may cheaply mark
  trace staleness after graph sync, but rebuilds are explicit);
* traceability refresh and conflict scan make **zero provider calls**;
* REST keeps `/projects`; nothing removed or renamed;
* all 35 Phase 1–5 MCP tools unchanged; Phase 6 adds exactly 8 read-only;
* `.openmind` schemaVersion stays `1.1.0`; Skill Bridge untouched and
  database-independent;
* v0001–v0006 databases migrate to v0007 with zero loss (v0007 is purely
  additive).

## 17. Security and scaling limits

Policies contain no executable code and are schema-validated with typed
errors; org policy files are size-capped (256 KB) and checked for provider
URLs and secret-looking content. Traversal is bounded everywhere
(`MAX_TRACE_DEPTH=10`, per-root path cap, per-run root cap, per-detector
comparison caps); every truncation is disclosed in the result and the run
summary. All rows carry `workspace_id` and every read filters on it. Notes
are bounded (2 000 chars), actors bounded (200), and no secret material is
ever persisted in trace or conflict rows.

## 18. Testing strategy

Fifteen new acceptance suites (all registered in
`scripts/run_acceptance.py`; a missing registration fails the manifest):
migration, policies, paths, coverage, gaps, orphans, incremental, conflict
model, detectors, promotion, governance, conflict incremental, bundle, CLI,
adapters. Fixtures are invented and neutral (the NameCheck lifecycle:
REQ-NC-017 → basic design → NameCheck API → schema → timeout configuration →
NameCheckService → test case → test result → evidence) with controlled
defect variants (missing design/implementation/test, stale revision,
ambiguous implementation, timeout/method/schema/threshold mismatches,
authority conflict, a confirmed semantic conflict candidate). CI runs the
full core tier on Ubuntu and a cross-platform smoke that covers migration,
a built-in policy, a complete requirement trace, a missing-test gap, a
deterministic conflict, a candidate promotion, a coverage snapshot and a
bundle export+verify — all in the established offline/no-egress
environment.

## 19. Explicitly deferred (Phase 7+)

Git commit-diff synchronization; feature-branch and PR overlays; webhooks;
merge-base analysis; automatic code-change impact from Git diffs; CI merge
blocking; automatic conflict resolution; automatic candidate promotion;
automatic authority inference; automatic requirement approval; automatic
test execution; Jira/Confluence connectors; Titan Mind integration; Feature
Evidence Packets; Claude Code / Codex plugin packaging; new Agent Skills;
Agent Skill Forge / Verification changes; Neo4j / Cypher / GraphQL;
worker-pool or job-DAG replacement; a complete graph-governance UI; the
Bundle 2.0 stable schema freeze.
