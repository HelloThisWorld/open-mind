"""MCP server wrapping the Open Mind knowledge layer (stdio transport).

Exposes the read/query surface a client (your editor, agent, or the CLI) needs —
all in-process, all local:
  search, route, dispatch, get_glossary, find_similar_cases, save_case, get_doc,
  propose_fix, apply_fix
plus the additive read-only Asset tools (v2 Phase 2) and document tools
(v2 Phase 3). The original nine are a frozen contract; new capabilities are
ADDED beside them, never by changing one.

Run:  python -m openmind.mcp_server
      openmind mcp serve            (identical; the CLI calls straight into here)
(register either command as an MCP stdio server in your client).

CONSTRUCTION
------------
The tools are plain module-level functions collected in :data:`TOOLS`, and
:func:`create_mcp_server` registers them on a fresh ``FastMCP``. That means a
test can build a server and inspect its tool set without the import of this
module having already opened a database — the import-time ``db.init_db()`` this
module used to run is now the runtime's job, done once per process by the shared
bootstrap.

The module-level ``mcp`` object is still available and is created on first
attribute access, so anything that referenced it keeps working.

The tools themselves are deliberately NOT routed through the application
services: they are pure, deterministic queries over the glossary, structure,
cases and RAG modules. Wrapping a one-line query module call in a service method
would add indirection without adding a seam.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from . import cases, codemod, docs as docsmod, glossary, machine, mapio, rag, router, scope

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is required: pip install mcp\n" + str(exc))

SERVER_NAME = "open-mind"


def _pids(scope_id: str) -> List[str]:
    pids = scope.resolve(scope_id)
    if not pids:
        raise ValueError(f"unknown or empty scope: {scope_id!r}")
    return pids


def _anchor(file_path: str, scope_id: Optional[str]) -> str:
    """Re-anchor a project-relative path (as returned by search / get_glossary) to
    an absolute path on THIS machine, using the project's machine-local source
    root. Tolerant: an already-absolute path, or a missing scope, is returned
    unchanged."""
    if scope_id and file_path:
        for pid in scope.resolve(scope_id):
            cand = machine.from_rel(pid, file_path)
            if os.path.isfile(cand):
                return cand
    return file_path


# ---------------------------------------------------------------------------
# Tools — argument and response contracts are STABLE; external clients depend
# on them. Changing a name or a returned key is a breaking change.
# ---------------------------------------------------------------------------
def search(scope: str, query: str, k: int = 12, case_sensitive: bool = True,
           subword: bool = False) -> Dict[str, Any]:
    """Hybrid code search (cases-first, then RAG). scope = project id.

    A bare identifier/literal query is matched as an EXACT token (token-boundary;
    never a substring/prefix/suffix of a different token, never embedding-conflated);
    a natural-language query uses the vector+lexical hybrid. Set subword=True to
    also match camelCase/snake_case components; case_sensitive=False to ignore case."""
    pids = _pids(scope)
    case_hits = cases.search_cases(pids, query, k=5)
    result = rag.retrieve(pids, query, k=k, case_sensitive=case_sensitive,
                          subword=subword)
    return {
        "case_hits": case_hits,
        "case_shortcircuit": any(c.get("similarity", 0) >= 0.65 for c in case_hits),
        "code_chunks": result["code_chunks"],
        "query_mode": result["query_mode"],
        "grounding": result["grounding"],
    }


def route(query: str) -> Dict[str, Any]:
    """Agent-style capability routing with deterministic graceful degradation.

    Returns which capability (glossary / structure / search) handles the query, who
    decided (a ready local model — validated against the capability set — else the
    deterministic if-else floor), plus the deterministic fallback and reason. The
    model can never select a capability outside the known set."""
    return router.route(query)


def dispatch(scope: str, query: str) -> Dict[str, Any]:
    """Route the query to one capability and INVOKE it; returns the result plus the
    routing trace. Each capability is deterministic/grounded; the router only chooses."""
    return router.dispatch(_pids(scope), query)


def get_glossary(scope: str, term: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic term/acronym resolution from the persisted glossary map.

    With `term`: an EXACT-TOKEN lookup (no similarity, no model guess) returning
    {definition (verbatim), source_file, line_number, content_hash, source_kind};
    an absent term returns found=False with 'no authoritative definition found in
    the indexed project' (never a fabricated definition). Without `term`: list
    every known term. Acronym/term questions should route here FIRST."""
    return glossary.get_glossary(mapio.merged_glossary(_pids(scope)), term)


def find_similar_cases(problem: str, scope: str, k: int = 5) -> Dict[str, Any]:
    """Search the solved-cases store for similar problems (with staleness flags)."""
    return {"cases": cases.search_cases(_pids(scope), problem, k=k)}


def save_case(scope: str, problem_text: str, resolution_summary: str,
              involved_services: Optional[List[str]] = None,
              involved_topics: Optional[List[str]] = None,
              file_refs: Optional[List[Dict[str, Any]]] = None,
              tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Save a solved case to the (single) project resolved by scope."""
    pids = _pids(scope)
    if len(pids) != 1:
        raise ValueError("save_case requires a single-project scope, got " + str(len(pids)))
    return cases.save_case(pids[0], {
        "problem_text": problem_text, "resolution_summary": resolution_summary,
        "involved_services": involved_services or [], "involved_topics": involved_topics or [],
        "file_refs": file_refs or [], "tags": tags or [],
    })


def get_doc(page: str, scope: str) -> Dict[str, Any]:
    """Fetch a generated documentation page (markdown) with its sync status."""
    doc = docsmod.get_doc(_pids(scope), page)
    if not doc:
        raise ValueError("doc page not found: " + page)
    return doc


def propose_fix(file_path: str, find: str, replace: str,
                scope: Optional[str] = None) -> Dict[str, Any]:
    """Preview a SMALL literal find/replace edit as a unified diff. Writes
    nothing — the human-in-the-loop review step before apply_fix.

    `file_path` may be the project-relative path returned by search/get_glossary;
    pass `scope` to re-anchor it to this machine's source root."""
    return codemod.propose(_anchor(file_path, scope), find, replace)


def apply_fix(file_path: str, find: str, replace: str, test_cmd: str,
              cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """Apply a literal find/replace ONLY if it keeps the test suite green.

    Runs `test_cmd` first (baseline must be green), applies the edit, re-runs the
    tests, and KEEPS the change only on green — otherwise the file is reverted
    byte-for-byte. The model never decides correctness; the tests do.

    `file_path` may be the project-relative path returned by search/get_glossary;
    pass `scope` to re-anchor it to this machine's source root."""
    return codemod.apply_fix(_anchor(file_path, scope), find, replace, test_cmd, cwd=cwd)


#: The published tool set, in registration order. The names are the MCP tool
#: names clients call. STABLE external contract — do not rename or change a
#: returned key. New capabilities are ADDED via ASSET_TOOLS below, never by
#: changing one of these nine.
TOOLS: List[Callable[..., Any]] = [
    search, route, dispatch, get_glossary, find_similar_cases, save_case,
    get_doc, propose_fix, apply_fix,
]

TOOL_NAMES = tuple(fn.__name__ for fn in TOOLS)


# ---------------------------------------------------------------------------
# Canonical Asset model tools (OpenMind v2 Phase 2) — ADDITIVE, read-only.
#
# These route through the shared AssetService (unlike the deterministic query
# tools above) because they need its workspace-scoping and snapshot-based
# evidence recovery. ``scope`` resolves to a workspace; an entity that does not
# belong to it is an honest not-found. Results are bounded, full source is never
# returned without an explicit bounded parameter, and merely listing the tools
# never starts the ingestion worker.
# ---------------------------------------------------------------------------
def _asset_workspaces(scope: str) -> List[str]:
    return _pids(scope)


def list_assets(scope: str, asset_type: Optional[str] = None,
                state: str = "active", limit: int = 50) -> Dict[str, Any]:
    """List a workspace's canonical Assets (bounded). ``scope`` = the workspace
    (project) id; ``state`` defaults to ``active`` (also: removed / unsupported /
    None for all). Returns {assets, total, count} — file/config objects with their
    logical key, type, state and current revision id."""
    from .runtime import get_runtime
    pid = _asset_workspaces(scope)[0]
    result = get_runtime().assets.list_assets(
        pid, asset_type=asset_type, state=(state or None), limit=limit)
    return {"workspace_id": pid, "assets": result["assets"],
            "total": result["total"], "count": result["count"]}


def get_asset(scope: str, asset_id: str) -> Dict[str, Any]:
    """One Asset plus its current-revision summary. Searches every workspace the
    scope resolves to and returns the first owner; an id in no in-scope workspace
    is an honest not-found."""
    from .runtime import get_runtime
    from .domain.errors import AssetNotFound
    assets = get_runtime().assets
    last: Optional[Exception] = None
    for pid in _asset_workspaces(scope):
        try:
            return assets.get_asset(pid, asset_id)
        except AssetNotFound as exc:
            last = exc
    raise last or AssetNotFound(asset_id)


def get_asset_revisions(scope: str, asset_id: str,
                        limit: int = 20) -> Dict[str, Any]:
    """An Asset's revision history (bounded, newest first)."""
    from .runtime import get_runtime
    from .domain.errors import AssetNotFound
    assets = get_runtime().assets
    last: Optional[Exception] = None
    for pid in _asset_workspaces(scope):
        try:
            return assets.list_revisions(pid, asset_id, limit=limit)
        except AssetNotFound as exc:
            last = exc
    raise last or AssetNotFound(asset_id)


def get_evidence(scope: str, evidence_id: str,
                 max_chars: int = 4000) -> Dict[str, Any]:
    """One Evidence citation with a WORKSPACE-RELATIVE source locator, bounded
    content recovered from the immutable snapshot, and an honest report of whether
    it came from the current source, the historical snapshot, or both."""
    from .runtime import get_runtime
    from .domain.errors import EvidenceNotFound
    assets = get_runtime().assets
    last: Optional[Exception] = None
    for pid in _asset_workspaces(scope):
        try:
            return assets.get_evidence(pid, evidence_id, max_chars=max_chars)
        except EvidenceNotFound as exc:
            last = exc
    raise last or EvidenceNotFound(evidence_id)


#: Additive read-only Asset tools. Registered ALONGSIDE the nine core tools,
#: never in place of one.
ASSET_TOOLS: List[Callable[..., Any]] = [
    list_assets, get_asset, get_asset_revisions, get_evidence,
]

ASSET_TOOL_NAMES = tuple(fn.__name__ for fn in ASSET_TOOLS)


# ---------------------------------------------------------------------------
# Document tools (OpenMind v2 Phase 3) — ADDITIVE and READ-ONLY.
#
# There is deliberately NO document-write tool. Importing a document reads a
# local file, stages an immutable blob and enqueues a job; exposing that over
# MCP would let a client make a server-side process read an arbitrary path it
# chose. Claude Code can drive `openmind document add` through its shell, where
# the user sees the command. Merely listing these tools never starts the worker.
#
# Every result is bounded, workspace-scoped, evidence-cited, and explicit about
# candidate-versus-confirmed status.
# ---------------------------------------------------------------------------
def list_documents(scope: str, status: Optional[str] = None,
                   parser: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """List a workspace's DOCUMENT assets (bounded).

    A document is an Asset whose current revision has a recorded parse result —
    a fact, not a guess from the file extension. ``status`` filters on that parse
    status (parsed / partial / needs-ocr / encrypted / unsupported / failed), so
    you can ask which documents were only partially read."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().documents.list_documents(
        pid, status=status, parser=parser, limit=limit)


def get_document(scope: str, asset_id: str) -> Dict[str, Any]:
    """One document: its Asset, current Revision and full parse summary —
    parser name and version, status, coverage, warnings, and any content that
    was deliberately NOT extracted (embedded images, macros, hidden sheets)."""
    from .runtime import get_runtime
    from .domain.errors import AssetNotFound
    documents = get_runtime().documents
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return documents.get_document(pid, asset_id)
        except AssetNotFound as exc:
            last = exc
    raise last or AssetNotFound(asset_id)


def get_document_outline(scope: str, revision_id: str,
                         limit: int = 300) -> Dict[str, Any]:
    """A bounded STRUCTURAL outline of one document revision.

    Blocks with their type, heading path, evidence id and a short preview — not
    the content. Use it to find the part you want, then call ``get_evidence``
    with that block's evidence id for the exact stored text."""
    from .runtime import get_runtime
    from .domain.errors import RevisionNotFound
    documents = get_runtime().documents
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return documents.get_outline(pid, revision_id, limit=limit)
        except RevisionNotFound as exc:
            last = exc
    raise last or RevisionNotFound(revision_id)


def search_documents(scope: str, query: str, limit: int = 20,
                     parser: Optional[str] = None,
                     block_type: Optional[str] = None) -> Dict[str, Any]:
    """Search the workspace's documents (vector + exact-token, RRF-fused).

    An exact Requirement ID, API path, error code or configuration key is
    matched on token boundaries and PROMOTED above merely similar text — a query
    for REQ-NC-017 never returns REQ-NC-0170 or a paragraph that just reads like
    it. Every hit carries an ``evidence_id`` you can pass to ``get_evidence``."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().documents.search(
        pid, query, limit=limit, parser=parser, block_type=block_type)


def search_knowledge(scope: str, query: str, code_limit: int = 12,
                     document_limit: int = 12) -> Dict[str, Any]:
    """Search code and documents together, returned as SEPARATE sections.

    A document hit appearing beside a code hit is NOT a claim that one
    implements, refines or verifies the other — this is retrieval, not
    relationship inference. The code-oriented ``search`` tool is unchanged and
    remains the right tool for code alone."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().documents.search_knowledge(
        pid, query, code_limit=code_limit, document_limit=document_limit)


def find_document_related_candidates(scope: str, asset_id: str,
                                     limit: int = 30) -> Dict[str, Any]:
    """Deterministic CANDIDATE associations between a document and existing
    knowledge.

    Every result is an OBSERVED MENTION labelled ``status: "candidate"``, with a
    confidence: high for an exact explicit identifier, medium for an exact match
    after normalization, low for semantic similarity alone. Nothing is stored,
    and no candidate asserts that the document implements, refines, verifies or
    contradicts its target — that requires verification OpenMind does not yet
    perform."""
    from .runtime import get_runtime
    from .domain.errors import AssetNotFound
    documents = get_runtime().documents
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return documents.find_related_candidates(pid, asset_id, limit=limit)
        except AssetNotFound as exc:
            last = exc
    raise last or AssetNotFound(asset_id)


#: Additive read-only document tools. Registered ALONGSIDE the nine core tools
#: and the four Phase 2 asset tools, never in place of one.
DOCUMENT_TOOLS: List[Callable[..., Any]] = [
    list_documents, get_document, get_document_outline, search_documents,
    search_knowledge, find_document_related_candidates,
]

DOCUMENT_TOOL_NAMES = tuple(fn.__name__ for fn in DOCUMENT_TOOLS)


# ---------------------------------------------------------------------------
# Semantic + lens tools (OpenMind v2 Phase 4) — ADDITIVE and STRICTLY
# READ-ONLY.
#
# Deliberately absent: anything that configures providers, changes a
# workspace's egress policy, starts a paid/cloud analysis, reviews a
# candidate or activates a lens. Those verbs stay on the CLI (and REST),
# where cloud use and configuration remain visible to the human running
# them; Claude Code can invoke the explicit CLI commands through its shell.
# Every result is bounded, workspace-scoped and explicit about candidate
# status — nothing returned here is canonical truth.
# ---------------------------------------------------------------------------
def list_semantic_runs(scope: str, status: Optional[str] = None,
                       limit: int = 20) -> Dict[str, Any]:
    """List a workspace's semantic analysis runs (bounded, newest first).
    Read-only. ``status`` filters on planned/queued/running/partial/done/
    failed/cancelled; a `partial` run has honest unprocessed targets."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().semantic.list_runs(pid, limit=limit, status=status)


def get_semantic_run(scope: str, run_id: str) -> Dict[str, Any]:
    """One analysis run: status, task set, per-status target counts and its
    usage totals (token numbers are NULL when the provider reported none)."""
    from .runtime import get_runtime
    from .semantic.errors import AnalysisRunNotFound
    semantic = get_runtime().semantic
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return semantic.get_run(pid, run_id)
        except AnalysisRunNotFound as exc:
            last = exc
    raise last or ValueError(f"run not found: {run_id}")


def list_semantic_candidates(scope: str, candidate_type: Optional[str] = None,
                             review_status: Optional[str] = None,
                             lifecycle_status: str = "active",
                             limit: int = 50) -> Dict[str, Any]:
    """List semantic CANDIDATES (bounded). Every entry carries
    ``status: "candidate"`` — a locally verified model proposal awaiting
    human review, never a canonical requirement/rule/relation. Lifecycle
    defaults to ``active``; pass ``stale`` or empty for history."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().semantic.list_candidates(
        pid, candidate_type=candidate_type, review_status=review_status,
        lifecycle_status=(lifecycle_status or None), limit=limit)


def get_semantic_candidate(scope: str, candidate_id: str) -> Dict[str, Any]:
    """One candidate with its verified evidence quotes. ``confidence`` is
    locally derived from evidence verification — the model's hint is shown
    separately and never decides."""
    from .runtime import get_runtime
    from .semantic.errors import CandidateNotFound
    semantic = get_runtime().semantic
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return semantic.get_candidate(pid, candidate_id)
        except CandidateNotFound as exc:
            last = exc
    raise last or ValueError(f"candidate not found: {candidate_id}")


def list_project_lenses(scope: str,
                        source: Optional[str] = None) -> Dict[str, Any]:
    """List Project Lenses: workspace rows plus virtual built-in Template
    projections and organization lens files. Read-only — activation and
    approval are explicit CLI verbs."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().lenses.list_lenses(pid, source=source)


def get_project_lens(scope: str, lens_id: str) -> Dict[str, Any]:
    """One lens with its deterministic validation report and status. An
    induced lens stays ``provisional`` until a human validates, approves and
    activates it."""
    from .runtime import get_runtime
    from .semantic.errors import LensNotFound
    lenses = get_runtime().lenses
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return lenses.get_lens(pid, lens_id)
        except LensNotFound as exc:
            last = exc
    raise last or ValueError(f"lens not found: {lens_id}")


def get_semantic_usage(scope: str, run_id: str) -> Dict[str, Any]:
    """One run's provider-usage ledger: per-request tokens, latency, retries
    and estimated cost (NULL with ``cost_source: unknown`` when no reliable
    price exists — never a fabricated zero)."""
    from .runtime import get_runtime
    from .semantic.errors import AnalysisRunNotFound
    semantic = get_runtime().semantic
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return semantic.get_usage(pid, run_id)
        except AnalysisRunNotFound as exc:
            last = exc
    raise last or ValueError(f"run not found: {run_id}")


#: Additive read-only semantic/lens tools (v2 Phase 4). Registered ALONGSIDE
#: the nine core, four asset and six document tools — 19 + 7 = 26 in total.
SEMANTIC_TOOLS: List[Callable[..., Any]] = [
    list_semantic_runs, get_semantic_run, list_semantic_candidates,
    get_semantic_candidate, list_project_lenses, get_project_lens,
    get_semantic_usage,
]

SEMANTIC_TOOL_NAMES = tuple(fn.__name__ for fn in SEMANTIC_TOOLS)


# ---------------------------------------------------------------------------
# Knowledge-graph tools (OpenMind v2 Phase 5) — ADDITIVE and STRICTLY
# READ-ONLY.
#
# Deliberately absent: anything that promotes a candidate, creates an
# Entity/Claim/Relation, merges or splits, changes authority, seeds or syncs
# the graph, or exports a bundle. Every graph MUTATION is an explicit CLI
# (or REST) verb that records a Human Decision with a caller-supplied actor;
# Claude Code drives those through its shell where the command is visible.
# Every result is bounded, workspace-scoped and carries the current
# Knowledge Revision.
# ---------------------------------------------------------------------------
def get_graph_stats(scope: str) -> Dict[str, Any]:
    """Canonical knowledge-graph statistics for a workspace: active entity/
    claim/relation counts by type and lifecycle, plus the current Knowledge
    Revision. Runs the incremental staleness reconciliation first, so the
    numbers never count knowledge whose sources moved on."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().knowledge.get_stats(pid)


def search_graph(scope: str, query: str, limit: int = 20,
                 include_stale: bool = False) -> Dict[str, Any]:
    """Search canonical Entities and Claims (separate result sections).
    Deterministic fusion: exact canonical key > exact alias > exact
    identifier token > lexical > vector similarity — an exact identifier is
    never outranked by something that merely reads similar. Stale/withdrawn
    objects are excluded unless include_stale."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().knowledge.search_entities(
        pid, query, limit=limit, include_stale=include_stale)


def get_graph_node(scope: str, node_id: str) -> Dict[str, Any]:
    """One graph node in the stable read shape. Accepts entity, claim,
    asset, revision, segment and evidence ids — the source-plane kinds are
    projected from their canonical rows, never duplicated."""
    from .runtime import get_runtime
    from .knowledge.errors import GraphNodeNotFound
    knowledge = get_runtime().knowledge
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return {"node": knowledge.get_node(pid, node_id)}
        except GraphNodeNotFound as exc:
            last = exc
    raise last or ValueError(f"graph node not found: {node_id}")


def expand_graph(scope: str, node_id: str, depth: int = 2,
                 direction: str = "both",
                 relation_types: Optional[List[str]] = None,
                 include_stale: bool = False) -> Dict[str, Any]:
    """Bounded, deterministic BFS expansion around one Entity node (hard
    caps on depth, nodes and edges; the result says when it truncated)."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().knowledge.expand_node(
        pid, node_id, depth=depth, direction=direction,
        relation_types=relation_types or None, include_stale=include_stale)


def find_graph_path(scope: str, source: str, target: str, max_depth: int = 6,
                    direction: str = "both",
                    include_stale: bool = False) -> Dict[str, Any]:
    """Bounded shortest-path discovery between two Entities, with Relation
    evidence summaries. Honest outcomes: found / no-path / truncated — a
    missing edge is never invented. This is generic graph reachability, NOT
    the Phase 6 formal Requirement Traceability."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().knowledge.find_path(
        pid, source, target, max_depth=max_depth, direction=direction,
        include_stale=include_stale)


def list_engineering_entities(scope: str, entity_type: Optional[str] = None,
                              lifecycle_status: str = "active",
                              limit: int = 50) -> Dict[str, Any]:
    """List canonical engineering Entities (bounded). ``lifecycle_status``
    defaults to active; pass ``stale`` or empty for history. Every entity
    entered the graph through deterministic projection, explicit manual
    creation or explicit candidate promotion — never automatically."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().knowledge.list_entities(
        pid, entity_type=entity_type,
        lifecycle_status=(lifecycle_status or None), limit=limit)


def get_engineering_entity(scope: str, entity_id: str) -> Dict[str, Any]:
    """One Entity with its aliases, bindings, claims and relations."""
    from .runtime import get_runtime
    from .knowledge.errors import EntityNotFound
    knowledge = get_runtime().knowledge
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return {"entity": knowledge.get_entity(pid, entity_id)}
        except EntityNotFound as exc:
            last = exc
    raise last or ValueError(f"entity not found: {entity_id}")


def get_engineering_claim(scope: str, claim_id: str) -> Dict[str, Any]:
    """One Claim with its evidence joins. Every active claim carries at
    least one Evidence citation verified against the immutable store."""
    from .runtime import get_runtime
    from .knowledge.errors import ClaimNotFound
    knowledge = get_runtime().knowledge
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return {"claim": knowledge.get_claim(pid, claim_id)}
        except ClaimNotFound as exc:
            last = exc
    raise last or ValueError(f"claim not found: {claim_id}")


def get_engineering_relation(scope: str, relation_id: str) -> Dict[str, Any]:
    """One Relation with its evidence joins, state and provenance.
    ``relation_state`` says how it is believed (explicit / inferred /
    confirmed / rejected / stale / superseded); ``possibly-related`` is
    never presented as anything stronger."""
    from .runtime import get_runtime
    from .knowledge.errors import RelationNotFound
    knowledge = get_runtime().knowledge
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return {"relation": knowledge.get_relation(pid, relation_id)}
        except RelationNotFound as exc:
            last = exc
    raise last or ValueError(f"relation not found: {relation_id}")


#: Additive read-only knowledge-graph tools (v2 Phase 5). Registered
#: ALONGSIDE everything above — 26 + 9 = 35 in total.
KNOWLEDGE_TOOLS: List[Callable[..., Any]] = [
    get_graph_stats, search_graph, get_graph_node, expand_graph,
    find_graph_path, list_engineering_entities, get_engineering_entity,
    get_engineering_claim, get_engineering_relation,
]

KNOWLEDGE_TOOL_NAMES = tuple(fn.__name__ for fn in KNOWLEDGE_TOOLS)


# ---------------------------------------------------------------------------
# Traceability + conflict tools (OpenMind v2 Phase 6) — ADDITIVE and
# STRICTLY READ-ONLY.
#
# Deliberately absent: anything that changes a trace policy, refreshes
# traceability, scans conflicts, promotes a conflict candidate, resolves or
# dismisses anything, or mutates the canonical graph. Every one of those is
# an explicit CLI (or REST) verb requiring a caller-supplied actor; Claude
# Code drives them through its shell where the command is visible. Every
# result is bounded, workspace-scoped and stamped with the Knowledge
# Revision and policy checksum it was computed against.
# ---------------------------------------------------------------------------
def trace_requirement(scope: str, entity_id: str,
                      include_stale: bool = False,
                      max_paths: int = 10) -> Dict[str, Any]:
    """FORMAL Requirement traceability under the workspace's active
    Traceability Policy (not generic graph reachability — that stays
    ``find_graph_path``). Returns policy-validated paths per kind, stage
    coverage, gaps, ambiguities and every traversal cap. A missing link is
    returned as a gap, never invented."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.trace_requirement(
        pid, entity_id, include_stale=include_stale, max_paths=max_paths)


def trace_code(scope: str, entity_id: str,
               include_stale: bool = False) -> Dict[str, Any]:
    """Reverse Code trace: upstream requirements/design/interfaces and
    downstream tests/results for one code-component / code-symbol /
    configuration / database-object / message-topic entity. Untraced code
    is reported as ``orphan: true, classification: "untraced"`` — a fact,
    never "invalid"."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.trace_code(
        pid, entity_id, include_stale=include_stale)


def trace_test(scope: str, entity_id: str,
               include_stale: bool = False) -> Dict[str, Any]:
    """Reverse Test trace: verified requirements, implementation targets
    and supporting evidence for one test-case / test-result entity, with an
    honest ``untraced`` status when no requirement path exists."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.trace_test(
        pid, entity_id, include_stale=include_stale)


def get_trace_path(scope: str, trace_id: str) -> Dict[str, Any]:
    """One PERSISTED trace path (tr_...) with its ordered steps and
    evidence joins, exactly as the last refresh validated it."""
    from .runtime import get_runtime
    from .traceability.errors import TracePathNotFound
    traceability = get_runtime().traceability
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return traceability.get_trace_path(pid, trace_id)
        except TracePathNotFound as exc:
            last = exc
    raise last or ValueError(f"trace path not found: {trace_id}")


def get_traceability_coverage(scope: str) -> Dict[str, Any]:
    """The latest CURRENT coverage snapshot: per-stage and per-requirement
    ratios with honest null percentages on zero denominators, and the
    policy-driven status. ``snapshot: null`` when no refresh has run."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.get_coverage(pid)


def list_traceability_gaps(scope: str, gap_type: Optional[str] = None,
                           status: Optional[str] = None,
                           limit: int = 100) -> Dict[str, Any]:
    """Traceability gaps (bounded): missing stages, stale/broken paths,
    ambiguity, orphans — first-class governance data the engine returns
    instead of inventing links. Filter by gap_type and status
    (open/resolved/accepted/dismissed/stale)."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.list_gaps(
        pid, gap_type=gap_type, status=status, limit=limit)


def list_engineering_conflicts(scope: str, status: Optional[str] = None,
                               category: Optional[str] = None,
                               limit: int = 100) -> Dict[str, Any]:
    """Canonical engineering conflicts (bounded): deterministic
    comparable-fact detections, promoted conflict candidates and manual
    records, with their lifecycle status. Governance verbs stay on the
    CLI."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().traceability.list_conflicts(
        pid, status=status, category=category, limit=limit)


def get_engineering_conflict(scope: str, conflict_id: str) -> Dict[str, Any]:
    """One conflict with its object joins, verified evidence quotes and
    complete decision history (every action doubly audited in the conflict
    ledger and the Knowledge Decision ledger)."""
    from .runtime import get_runtime
    from .traceability.errors import ConflictNotFound
    traceability = get_runtime().traceability
    last: Optional[Exception] = None
    for pid in _pids(scope):
        try:
            return traceability.get_conflict(pid, conflict_id)
        except ConflictNotFound as exc:
            last = exc
    raise last or ValueError(f"conflict not found: {conflict_id}")


#: Additive read-only traceability/conflict tools (v2 Phase 6). Registered
#: ALONGSIDE everything above — 35 + 8 = 43 in total.
TRACE_TOOLS: List[Callable[..., Any]] = [
    trace_requirement, trace_code, trace_test, get_trace_path,
    get_traceability_coverage, list_traceability_gaps,
    list_engineering_conflicts, get_engineering_conflict,
]

TRACE_TOOL_NAMES = tuple(fn.__name__ for fn in TRACE_TOOLS)


# ---------------------------------------------------------------------------
# Git Overlay tools (v2 Phase 7) — additive, READ-ONLY, provisional
# ---------------------------------------------------------------------------
# Every one of these reports OVERLAY output, which is a provisional projection
# onto a snapshot of the canonical Base Workspace. None of them mutates Git or
# canonical knowledge; overlays are created/refreshed/reconciled/deleted only
# from the CLI or REST, never here. Projected gaps are NOT canonical gaps and
# projected conflicts are NOT canonical conflicts.
def list_git_overlays(scope: str, state: Optional[str] = None) -> Dict[str, Any]:
    """List this workspace's Git overlays (branch/PR/commit-range/working-tree/
    change-set) with their state and revision. Overlays are provisional views;
    the canonical Base Workspace is unchanged. Read-only."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.list_overlays(pid, state=state)


def get_git_overlay(scope: str, overlay_id: str) -> Dict[str, Any]:
    """One Git overlay's identity, state, overlay revision and pinned Base
    coordinates (Base Knowledge Revision, policy checksum). Provisional;
    read-only; never mutates Git or canonical knowledge."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.get_overlay(pid, overlay_id)


def get_git_diff_summary(scope: str, overlay_id: str) -> Dict[str, Any]:
    """The overlay's changed-file summary and per-file change taxonomy
    (added/modified/deleted/renamed/copied/type-changed, binary/symlink/
    submodule/LFS). Read-only; no Git mutation, no remote contact."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.get_diff_summary(pid, overlay_id)


def search_git_overlay(scope: str, overlay_id: str, query: str,
                       limit: int = 10) -> Dict[str, Any]:
    """Composed overlay search over changed content, labelled base /
    overlay-before / overlay-after, with a maskedBaseHits count. Read-only."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.search_overlay(pid, overlay_id, query,
                                                 limit=limit)


def get_git_overlay_evidence(scope: str, overlay_id: str,
                             evidence_id: str) -> Dict[str, Any]:
    """One immutable overlay Evidence citation (oev_...): its git-blob-range /
    git-worktree-range locator, bounded excerpt and content hash. Overlay
    Evidence is provisional and is never accepted by canonical promotion."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.get_overlay_evidence(pid, overlay_id,
                                                      evidence_id)


def get_change_impact_report(scope: str, overlay_id: str) -> Dict[str, Any]:
    """The deterministic Change Impact Report (schema 1.0.0-draft.1): file/
    segment changes, graph deltas, impacted requirements/tests, trace/gap/
    conflict impact and rule-based risk — rendered from structured records, not
    a model. Projected gaps/conflicts are NOT canonical. Read-only."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.get_impact_report(pid, overlay_id)


def list_impacted_requirements(scope: str, overlay_id: str) -> Dict[str, Any]:
    """Requirements this overlay would affect, via reverse Base traceability
    revalidated against the virtual overlay graph. Provisional projection; the
    canonical trace snapshot is unchanged. Read-only."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.list_impacted_requirements(pid, overlay_id)


def list_impacted_tests(scope: str, overlay_id: str) -> Dict[str, Any]:
    """Tests whose Base trace paths include an object this overlay changed or
    removed. Tests are reported, NEVER executed, and a listed test is never
    claimed sufficient. Read-only; provisional."""
    from .runtime import get_runtime
    pid = _pids(scope)[0]
    return get_runtime().overlays.list_impacted_tests(pid, overlay_id)


#: Additive read-only Git overlay tools (v2 Phase 7). Registered ALONGSIDE
#: everything above — 43 + 8 = 51 in total.
OVERLAY_TOOLS: List[Callable[..., Any]] = [
    list_git_overlays, get_git_overlay, get_git_diff_summary,
    search_git_overlay, get_git_overlay_evidence, get_change_impact_report,
    list_impacted_requirements, list_impacted_tests,
]

OVERLAY_TOOL_NAMES = tuple(fn.__name__ for fn in OVERLAY_TOOLS)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def create_mcp_server(runtime: Optional[Any] = None) -> FastMCP:
    """Build a ``FastMCP`` server exposing :data:`TOOLS`.

    *runtime* is an :class:`~openmind.runtime.OpenMindRuntime`; when omitted the
    process-wide one is created and bootstrapped. Bootstrapping here (rather than
    at import time) is what makes the module importable in a test without opening
    a database, while still guaranteeing that a served process has run its
    migrations.
    """
    if runtime is None:
        from .runtime import get_runtime
        get_runtime()
    else:
        runtime.bootstrap()

    server = FastMCP(SERVER_NAME)
    for fn in TOOLS:
        server.tool()(fn)
    for fn in ASSET_TOOLS:                 # additive read-only Asset tools (Phase 2)
        server.tool()(fn)
    for fn in DOCUMENT_TOOLS:              # additive read-only document tools (Phase 3)
        server.tool()(fn)
    for fn in SEMANTIC_TOOLS:              # additive read-only semantic/lens tools (Phase 4)
        server.tool()(fn)
    for fn in KNOWLEDGE_TOOLS:             # additive read-only graph tools (Phase 5)
        server.tool()(fn)
    for fn in TRACE_TOOLS:                 # additive read-only trace/conflict tools (Phase 6)
        server.tool()(fn)
    for fn in OVERLAY_TOOLS:               # additive read-only git overlay tools (Phase 7)
        server.tool()(fn)
    return server


_mcp: Optional[FastMCP] = None


def __getattr__(name: str) -> Any:
    """Lazily provide the module-level ``mcp`` server.

    PEP 562 module ``__getattr__``: ``mcp_server.mcp`` still resolves for anyone
    who referenced it, but merely importing this module no longer builds a
    server or touches the database.
    """
    if name == "mcp":
        global _mcp
        if _mcp is None:
            _mcp = create_mcp_server()
        return _mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    from .runtime import get_runtime
    create_mcp_server(get_runtime()).run()


if __name__ == "__main__":
    main()
