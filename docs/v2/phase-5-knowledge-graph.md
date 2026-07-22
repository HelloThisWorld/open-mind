# OpenMind v2 — Phase 5: Canonical Engineering Knowledge Graph, Candidate Promotion and Knowledge Bundle 2.0 Draft

Status: implemented in this phase. Runtime version `1.5.0-dev`; artifact schema
stays `1.1.0`; database schema head moves from v0005 to v0006; Knowledge Bundle
uses its own separate draft schema `2.0.0-draft.1`.

Phase 5 adds the first **canonical** engineering knowledge layer: durable,
versioned, evidence-bound Entities, Claims and Relations that enter the graph
only through an explicit write — deterministic projection, explicit manual
creation, or the explicit promotion of a human-confirmed semantic Candidate.
Nothing a model proposed becomes canonical on its own, and nothing canonical
loses its provenance.

---

## 1. Candidate versus canonical knowledge

Phase 4 ends at the Candidate: a provider-produced, locally evidence-verified
proposal with `review_status` (human judgement) and `lifecycle_status`
(source freshness). Confirming a Candidate marks it *suitable for promotion*;
it changes candidate metadata only.

Phase 5 adds the canonical side and the one bridge between them:

| | Candidate (Phase 4) | Canonical object (Phase 5) |
|---|---|---|
| Produced by | provider + local verifier | deterministic projector, explicit human action, explicit promotion |
| Tables | `semantic_candidates`, `semantic_relation_candidates`, `semantic_conflict_candidates` | `engineering_entities`, `engineering_claims`, `engineering_relations` (+ aliases, bindings, evidence joins) |
| Truth status | always `status: "candidate"` | governed record with lifecycle, authority and provenance |
| History | reviewed/stale, kept | versioned by Knowledge Revisions, superseded/withdrawn/merged, kept |
| Deleted when promoted | **never** — the Candidate remains queryable | n/a |

**Canonical does not mean infallible.** A canonical Relation may be
`inferred` (a deterministic call edge with preserved ambiguity) or
`confirmed` (a human promoted it). Canonical means: stored in the canonical
schema, versioned, evidence-bound, provenance-recorded, governed by explicit
lifecycle actions, included in Knowledge Revisions, and queryable through
stable contracts.

Candidates are never copied wholesale into the graph. A canonical object
records `promoted_from_candidate_id`, `promoted_by`, `promoted_at` and
`promotion_policy_version` (in `knowledge_promotions` + the object's own
provenance columns), and the source Candidate remains in its Phase 4 table.
The graph never depends on the Candidate row's survival — the stored id is
provenance, not an ownership edge, and nothing cascade-deletes canonical
history.

## 2. The four write paths (and no others)

Graph truth can enter through exactly:

1. **deterministic projection** (`graph seed` / `graph sync`) — model-free,
   local, versioned by `GRAPH_PROJECTOR_VERSION`;
2. **explicit manual creation** (`entity create`, `claim create`,
   `relation create`) — evidence-required, actor-attributed;
3. **explicit Candidate promotion** (`promotion promote`);
4. **explicit Relation Candidate promotion** (`promotion promote-relation`).

`semantic review confirm` does not promote. Ingestion does not promote.
Conflict candidates cannot be promoted in Phase 5 (they stay in the Phase 4
store for the Phase 6 conflict engine). There is no generic mutation
endpoint; every write is a typed operation that records a Human Decision and
a Knowledge Revision.

## 3. Entity identity

An Entity is one engineering concept in one Workspace, identified logically by

```text
UNIQUE(workspace_id, entity_type, canonical_key)
```

`canonical_key` is deterministic and normalized, never a display name and
never a bare UUID (the database id `ent_*` exists but is not the logical
identity). Key shapes used by the projector and promotion:

```text
requirement:REQ-NC-017
interface:POST:/name-check
code-component:asset:a_123
code-symbol:asset:a_123:org.example.NameCheckService#execute(Request)
configuration:asset:a_456:namecheck.timeout
database-object:database-schema:PASSENGER
document:asset:a_789
```

Identity resolution at promotion time: an exact stable identifier
(`stable_key` such as `REQ-NC-017`) resolves to `entity_type:<identifier>`;
without one, a deterministic normalized-title key is derived
(`<type>:derived:<normalized-title>`), and the plan reports
`identity_matches` / `identity_conflicts` so a human sees what the promotion
would attach to before writing. Descriptions are bounded (promoted Candidate
statement or a concise derived label); chain-of-thought is never stored.

Entity types, Claim types, Relation types, relation states, lifecycle
statuses, authority statuses and origins are **closed vocabularies**
(`openmind/knowledge/vocabularies.py`), validated at every write boundary.
An unknown model- or caller-supplied type fails typed
(`UnknownVocabularyValue`), never "accepted for later". There is deliberately
no `depends-on` relation: dependency semantics must use the closest honest
member (`calls`, `reads`, `consumes`, …) or stay out of the graph.

## 4. Aliases

`engineering_entity_aliases` holds alternative names (identifier, name,
acronym, legacy-name, path, symbol, manual), each normalized
(case-folded, whitespace collapsed) for exact lookup. Rules:

* an alias row belongs to exactly one Entity;
* an exact normalized collision with an alias of a DIFFERENT active Entity is
  **reported** (typed `AliasCollision`, listing the holders) — never silently
  attached; resolution is an explicit merge or a manual decision;
* removed aliases keep their row with `status='removed'` for audit;
* aliases participate in graph search (exact alias outranks vector
  similarity) and may carry Evidence.

## 5. Bindings

`engineering_entity_bindings` connect an Entity to source-plane objects
(asset, revision, segment, evidence, document-block, code-symbol,
configuration-key, database-object, api-operation, message-topic) in a role
(primary-source, definition-source, implementation, configuration, test,
interface, supporting, historical). The referenced object must belong to the
same Workspace (validated through the Phase 2 scoped reads). A binding to a
non-current Revision becomes `stale` at reconciliation; historical bindings
stay queryable. A binding is *not* a Relation — it anchors one concept to the
source, it does not connect two concepts.

## 6. Claim identity and Evidence

A Claim is one bounded statement about one Entity.
`normalized_statement_hash` = SHA-256 of the whitespace-collapsed, case-folded
statement; an identical normalized Claim on the same Entity deduplicates to
the existing active row instead of inserting. Claims are immutable in
meaning: correcting one creates a new Claim and supersedes the old
(`superseded_by_claim_id`), never rewrites it.

**Every active Claim requires at least one valid Evidence** join
(`engineering_claim_evidence`, roles primary/supporting/context/authority).
At write time each cited Evidence id must exist in the Workspace and each
quote must be a whitespace-normalized substring of the immutable Evidence
content (same rule as the Phase 4 verifier — fabricated quotes are rejected
locally). Manual Claims without Evidence are rejected. The one documented
exception in the whole graph: a **deterministic structural container** Entity
(e.g. a code-component projected from an Asset) may exist with bindings but
no Claim, because its existence is the recorded deterministic fact.

## 7. Relation identity and provenance

A Relation connects two Entities of the same Workspace with a closed type and
a state (`explicit` / `inferred` / `confirmed` / `rejected` / `stale` /
`superseded`). The active-identity key is

```text
(workspace_id, source_entity_id, target_entity_id, relation_type)
```

— re-promoting or re-projecting the same edge reuses the existing active row
(idempotent). Self-relations are rejected (no current type supports them).
Every Relation carries provenance: verified Evidence joins
(`engineering_relation_evidence`), or deterministic source facts plus
bindings (projected containment/call edges), or an explicit Human Decision
(manual creation). Unexplained Relations are impossible. State semantics:

* deterministic call edge → `inferred`, `origin=deterministic`, confidence
  from the analyzer (name-based ambiguity is preserved as low confidence,
  never upgraded);
* structurally explicit containment (OpenAPI operation in its document) →
  `explicit`;
* promoted Relation Candidate → `confirmed`, `origin=semantic-promotion`;
* a rejected Relation is retained, excluded from the active graph, and can be
  restored explicitly;
* `possibly-related` is never rewritten as `implements`.

## 8. Knowledge Revisions

`knowledge_revisions` is the per-Workspace monotonic ledger:
`UNIQUE(workspace_id, revision_number)`. One successful canonical graph
transaction = exactly one revision row, allocated *inside* the same SQLite
transaction as the graph writes (single WAL connection + process lock, so
concurrent writers serialize and numbers can neither duplicate nor skip
backwards). A failed transaction rolls everything back — no revision, no
partial graph. Each revision records action (graph-seed, graph-sync,
candidate-promotion, relation-promotion, manual-entity-create, entity-merge,
claim-supersede, authority-change, graph-reconcile, …), a bounded summary,
the actor, and per-kind changed-object counts. Read operations report the
current revision number; Bundle export records the exported revision.
Revisions are immutable.

## 9. Human Decisions

`knowledge_decisions` — one immutable row per governance write
(promote-candidate, promote-relation, create-entity, create-claim,
create-relation, add-alias, remove-alias, merge-entity, split-entity,
mark-authoritative, mark-non-authoritative, supersede, withdraw,
reject-relation, restore-relation), linked to its Knowledge Revision, with
caller-supplied `actor` (may be empty — identity is never inferred), a
bounded note, bounded before/after snapshots (no secrets — they are built
from graph rows, which never contain credentials), and the invoking command.
Decisions are exported in the Bundle.

## 10. Promotion

### Eligibility (checked at plan AND again inside the promoting transaction)

Semantic Candidate: `review_status=confirmed` ∧ `lifecycle_status=active` ∧
`evidence_status=verified` ∧ supported kind ∧ every cited Evidence belongs to
the Workspace ∧ every source Revision is current ∧ not already promoted.
Relation Candidate adds: both endpoints resolve unambiguously to Workspace
objects. There are no bypass flags (`--accept-stale` etc. do not exist);
stale material must be re-analyzed or recreated manually with fresh
Evidence.

### Planning

`plan_candidate_promotion` / `plan_relation_promotion` are deterministic,
provider-free and write nothing. They return eligibility, blocking reasons,
the proposed Entity/Claim/aliases/bindings (or relation + endpoint
resolutions), identity matches/conflicts, any existing target, and the
expected action (`create-entity-and-claim` / `attach-claim-to-existing-entity`
/ `already-promoted` / `blocked` / `identity-conflict`).

### Behavior by kind

* **engineering-concept** → resolve-or-create the Entity, create/reuse the
  primary Claim, Evidence joins copied from the verified candidate quotes,
  aliases from stable identifiers, bindings to the source Revision/Segment,
  one promotion record, one Decision, one Knowledge Revision; vector
  projection refreshed after commit.
* **classification** → never overwrites `assets.asset_type`; creates/reuses
  the Asset's `document` Entity and attaches a `classification` Claim bound
  to Asset + Revision.
* **revision-status** → a `revision-status` Claim only;
  `asset_revisions.status` is never written.
* **relation candidate** → endpoints re-resolved, Evidence revalidated,
  canonical Relation created/reused as `confirmed`.

### Idempotency and blocking

Promoting the same Candidate twice returns the same target
(`status=already-promoted`) and writes no new rows and no new Knowledge
Revision. A blocked attempt returns `status=blocked` with reasons and
persists nothing (no revision, no decision) — recorded promotions exist only
for promotions that happened.

## 11. Deterministic graph projection

`openmind/knowledge/projector.py` is local and model-free (no semantic
import, no provider, zero egress). `GRAPH_PROJECTOR_VERSION` versions the
rules; `knowledge_projection_state` stores, per Workspace, the projector
version, a source-knowledge hash (active asset ids + current revision ids +
segment content hashes + relevant facet/structure hashes), the last Knowledge
Revision written and the sync time.

Projection rules (each documented and tested):

| Source fact | Graph object |
|---|---|
| active `source-code` / `test-source` Asset | `code-component` Entity |
| active `configuration` Asset | `configuration` Entity |
| active `database-schema` Asset | `data-model` Entity (+ per-object `database-object` Entities from parsed SQL segments) |
| documentation/document Asset with a parse record | `document` Entity |
| `build-definition` Asset | `build-definition` Entity |
| Java type/method/constructor Segment | `code-symbol` Entity |
| OpenAPI `api-operation` Segment | `interface` Entity |
| OpenAPI `schema-definition` Segment | `data-model` Entity |
| SQL object Segment | `database-object` Entity |
| deterministically identified configuration key Segment | `configuration` Entity |
| message-topic facet capture | `message-topic` Entity |
| structural containment | `contains` Relation, state `explicit` |
| structure-map call edge (both endpoints resolved, evidence exists) | `calls` Relation, state `inferred`, confidence from analyzer ambiguity |

Deliberate non-projections: a generic document never becomes a Requirement
or Design; a method in a test-source file never becomes a Test Case; a
name-only ambiguous call never becomes a confirmed edge; facets are
projected only where their semantics are exact.

`sync` is incremental: unchanged source hash → zero writes, zero revisions.
A changed hash updates only the affected Assets' entities/bindings/relations,
stales what disappeared, and writes one Knowledge Revision. `seed` is the
first sync; a full reconcile pass exists for recovery.

## 12. Staleness

`reconcile_graph_staleness(workspace_id)` — incremental, indexed:

1. bindings referencing non-current Revisions → stale;
2. Claims whose only live primary Evidence sits on stale Revisions → stale;
3. Relations depending on stale endpoints/claims/evidence → stale;
4. deterministic Entities with no active bindings → stale;
5. manual Entities with valid active Claims stay active (no code binding
   required); authority status is preserved on stale objects; confirmed
   promotions keep their history.

Runs after code/document Revision commits (same hook chain as the Phase 4
candidate reconciliation), at Runtime worker startup as a backstop, before
active graph statistics, and before a current-only Bundle export. Stale
objects are excluded from active queries by default and remain queryable
with `include_stale`.

## 13. Merge and split

**Merge** (source → target, one transaction): source becomes `merged` with
`merged_into_entity_id` (never deleted; still addressable and resolving to
the target); aliases move unless they collide (collisions reported in the
result and left on the source, auditable); bindings move and deduplicate;
Claims move (their normalized hashes deduplicate against the target's);
Relations are rewired, self-relations eliminated, duplicates deduplicated;
Evidence intact; one Decision, one Knowledge Revision, vector projections
refreshed (source's active projection removed).

**Split** is explicit and narrow: the caller lists exactly the Claim ids and
Binding ids to move and the Relation endpoint rewrites; the new Entity's
type/key/name are given, at least one Claim or Binding must move, every moved
id must belong to the source, and the transaction is all-or-nothing. No
model chooses a split. One Decision, one Knowledge Revision.

## 14. Search and the graph vector projection

Each Workspace gets a separate `knowledge_<workspace-id>` vector collection —
never mixed into `code_` / `documents_` (the existing search contracts stay
byte-identical). Indexed objects: **active Entities and Claims** (Relations
are not embedded). Entity text = type + canonical key + display name +
aliases + description + active primary claims; Claim text = type + statement
+ entity identity + bounded evidence locator summary. Metadata carries
workspace, object kind, ids, types, lifecycle, authority, origin, revision
and content hash.

`search_graph` fuses, deterministically and in this precedence order: exact
canonical key → exact normalized alias → exact identifier token → lexical
match → vector similarity. Exact identifiers always outrank semantic
similarity. Entities and Claims are returned as separate sections; every hit
carries Evidence ids or a navigable Claim id, plus the current Knowledge
Revision. Stale/superseded/withdrawn objects are excluded by default.
Lifecycle: updates re-project, merge removes the source's active projection,
terminate/delete drop `knowledge_<id>` (registered in
`vectorstore._COLLECTION_PREFIXES`, so the startup orphan sweep recognizes
it and deleting workspaces are not treated as orphans; the batched-drain and
in-flight-drop protections apply unchanged).

## 15. Graph queries

`runtime.knowledge` / `ServiceContainer.knowledge` expose the full
workspace-scoped operation set (stats, current revision, entity/claim/
relation list+get, node lookup, search, expand, path, subgraph, promotion
plan/promote for both kinds, manual creates, aliases, merge, split,
authority, supersede, withdraw, seed/sync/reconcile, revision + decision
history). A graph object of Workspace A is unreachable through Workspace B —
every SQL read filters on the validated workspace id.

**Node abstraction**: `get_node` returns the stable camelCase read shape
(id, nodeKind, entityType, canonicalKey, displayName, lifecycleStatus,
authorityStatus, origin, knowledgeRevision, bindings, claimCount,
relationCount). Node kinds: entity, claim, asset, revision, segment,
evidence — the source-plane kinds are *projected into the read shape* from
their canonical Phase 2 rows, not copied into graph tables.

**Expansion** is bounded BFS with deterministic ordering (relation type,
then target key): direction in/out/both, relation-type filter, depth ≤ 4,
nodes ≤ 1000, edges ≤ 3000 (defaults lower), explicit `truncated` flag,
`include_stale` off by default. No recursive SQL, no unbounded traversal.

**Path discovery** is deterministic BFS shortest-path with bounded depth and
visited-node cap, equal-length paths returned up to a cap, Relation Evidence
summaries included, and three honest outcomes: found / no-path / truncated.
It is generic path discovery — explicitly **not** formal Requirement
Traceability (Phase 6).

**Subgraph export** returns the bounded node+edge set around a seed node
set, same limits and determinism.

## 16. Knowledge Bundle 2.0 Draft

`.openmind` 1.1.0 is untouched. `openmind bundle export` writes a separate
`.openmind-v2/` directory: `manifest.json`, `workspace.json`, JSONL files
(assets, revisions, segments, evidence, entities, aliases, bindings, claims,
claim-evidence, relations, relation-evidence, decisions,
knowledge-revisions, lenses) and `schemas/*.schema.json`. Guarantees:
deterministic ordering (stable sort keys per file), UTF-8 JSONL with `\n`,
source-relative locators, no absolute paths, no secrets/prompts/raw provider
responses/semantic cache/provider profiles, every active Claim has Evidence,
every Relation endpoint and Relation Evidence exists, and the manifest
records runtime version, bundle schema version, workspace, Knowledge
Revision, timestamp, per-file SHA-256 hashes, record counts and warning
flags.

Modes: `--current-only` (active objects only, after a staleness reconcile),
`--include-history` (stale/superseded/withdrawn/merged included),
`--knowledge-revision N` (records whose created/updated revision ≤ N — an
honest approximation documented as such: it filters by revision stamps, it
does not reconstruct point-in-time lifecycle states).

`python -m openmind.bundle_verify <dir>` is a standalone, dependency-free
verifier (schema shape, referential integrity, evidence integrity, counts,
hashes, active-claim evidence, relation endpoints, duplicate ids,
ordering). Export requires no semantic provider. The Bundle stays **Draft**:
not yet a frozen external contract, and there is no import.

## 17. GraphStore boundary

```text
openmind/knowledge/            the Phase 5 package
├── vocabularies.py            closed vocabularies (+ validation helpers)
├── errors.py                  typed graph errors (OpenMindError subclasses)
├── store.py                   SQL repository over the v0006 tables
│                              (same shared WAL connection + lock as db.py)
├── revisions.py               transactional revision allocation + decisions
├── identity.py                canonical keys, normalization, statement hashes
├── verifier.py                evidence ownership + quote verification
├── promotion.py               eligibility, planning, promotion transactions
├── projector.py               deterministic seed/sync (+ GRAPH_PROJECTOR_VERSION)
├── reconciliation.py          graph staleness
├── graph.py                   node shape, bounded expansion, paths, subgraph
├── search.py                  exact/lexical/vector fused graph search
├── vector_projection.py       knowledge_<ws> collection lifecycle
├── decisions.py               decision record helpers
├── bundle.py                  Bundle 2.0 Draft exporter
├── service.py                 KnowledgeService (runtime.knowledge)
└── models.py                  row mappers / read shapes
openmind/ports/graph_repository.py   the repository protocol (testing seam)
openmind/bundle_verify.py            standalone verifier (python -m openmind.bundle_verify)
```

`db.py`, `jobs.py` and `main.py` gain only thin hooks (reconcile call sites,
job-type-free — graph writes are synchronous CLI/REST verbs, no new job
types; the projector runs in-process). Graph SQL lives in
`knowledge/store.py`, grouped, never scattered.

## 18. Compatibility

* `ingest` / `asset add` / `document add`: behavior preserved; zero cloud
  calls; no automatic graph projection is attached to ingestion in this
  phase (graph seed/sync are explicit verbs), so ingestion cannot fail from
  a graph error;
* Phase 4 semantics preserved (`unreviewed/confirmed/rejected` ×
  `active/stale`); `semantic review confirm` still only updates metadata;
* local Ask untouched — no graph or cloud redirection;
* REST: `/projects` naming, all existing routes intact, graph routes
  additive;
* MCP: the 26 existing tools unchanged; exactly nine additive **read-only**
  graph tools (`get_graph_stats`, `search_graph`, `get_graph_node`,
  `expand_graph`, `find_graph_path`, `list_engineering_entities`,
  `get_engineering_entity`, `get_engineering_claim`,
  `get_engineering_relation`) = 35 total, accounted for in tests. No MCP
  tool promotes, creates, merges, changes authority, seeds or exports;
* `.openmind` export and the dependency-free artifact path unchanged;
* Skill Bridge unchanged and database-independent;
* v0006 is purely additive — Phase 1–4 databases migrate losing nothing;
* delete-race / delete-responsive protections extended to the new
  collection prefix, not modified.

## 19. Testing strategy

Fifteen focused suites, all registered in `scripts/run_acceptance.py` (a
missing registration fails the manifest): `verify_knowledge_migration`,
`_entities`, `_claims`, `_relations`, `_promotion`, `_projection`,
`_revisions`, `_decisions`, `_staleness`, `_merge_split`, `_search`,
`_graph`, `_bundle`, `_cli`, `_adapters`. All offline (isolated data dirs,
`OPENMIND_EMBED_OFFLINE=1`, no egress); semantic Candidates are created with
the Phase 4 **mock provider** or direct store writes — no real provider API
is ever called. CI: the ubuntu full gate runs the whole core tier plus the
Bundle verifier and MCP/Skill-Bridge smoke; cross-platform smoke covers
migration → seed → promotion → lookups → path → bundle export+verify.

## 20. Explicitly deferred to Phase 6+

Formal Requirement Traceability and coverage/gap scoring; conflict
*resolution* and conflict-candidate promotion; change-impact analysis;
branch/PR overlays; webhooks; CI policy gates; Neo4j or any external graph
database; Cypher/Gremlin; a Graph UI; Bundle 2.0 schema freeze and import;
Titan Mind integration; Feature Evidence Packets; provider batch APIs; cloud
embeddings; historical document vector search; OCR; COBOL/JCL parsing;
Jira/Confluence connectors; plugin packaging; new Agent Skills and Skill
Forge/Verification changes; worker-pool/job-DAG replacement; graph-assisted
Ask (a later opt-in).
