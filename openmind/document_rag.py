"""Document retrieval over the per-workspace ``documents_<id>`` collection.

WHY A SEPARATE MODULE AND A SEPARATE COLLECTION
-----------------------------------------------
:mod:`openmind.rag` and the MCP ``search`` tool are a stable external contract:
their chunk ids, metadata keys and response shape are what editor clients depend
on. Adding document chunks to the code collection would silently change what
every existing ``search`` call returns. So documents get their own collection and
their own retrieval, and ``search`` stays code-oriented and byte-for-byte
unchanged.

THE RANKING RULE THAT MATTERS
-----------------------------
An exact **Requirement ID, API path, error code or configuration key must never
be displaced by a merely embedding-similar result.** Someone searching
``REQ-NC-017`` wants that requirement, not a paragraph that reads like it. So
retrieval runs three legs and fuses them:

* **vector** — conceptual similarity (candidates only);
* **lexical** — token-BOUNDARY matching via :mod:`openmind.tokenmatch`, so
  ``REQ-NC-017`` never matches ``REQ-NC-0170`` and ``ack`` never matches
  ``acked``;
* **exact identifier** — when the whole query IS an identifier, a hit that
  contains it as a complete token is PROMOTED above every semantic-only hit,
  rather than merely scored higher.

Fusion is Reciprocal Rank Fusion, the same deterministic scheme the code RAG
uses, so the two behave alike where they overlap.

EVERY HIT IS CITABLE
--------------------
A hit carries its ``segment_id`` and ``evidence_id``, so a caller can go straight
from a search result to the exact stored block text via
``AssetService.get_evidence`` — no re-parsing, no guessing which part of the
document the answer came from.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import db, embeddings, tokenmatch, vectorstore

#: Hard ceiling on returned hits, whatever a caller asks for.
MAX_HITS = 50
#: How much of a block's text is returned as the excerpt. The full text is
#: available through get_evidence; a search response must stay bounded.
EXCERPT_CHARS = 400

#: Identifier shapes worth treating as exact even inside a longer sentence.
#: Deliberately narrow and explicit — each is a real convention, not a guess:
#:   REQ-NC-017 / ABC-1234   requirement and ticket ids
#:   NC-100                  error codes
#:   a.b.c                   dotted configuration keys and topic names
#:   /name-check             API paths
_ID_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,9}){1,3}\b"),
    re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_]*){2,}\b"),
    re.compile(r"(?<![\w/])/[a-zA-Z][\w\-]*(?:/[\w\-{}]+)*"),
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "what", "which", "where",
         "when", "does", "from", "into", "must", "should", "document", "section",
         "requirement", "requirements", "page", "how"}


def extract_identifiers(query: str) -> List[str]:
    """Explicit identifiers inside a free-text query, in a stable order.

    ``"what is the manual review timeout for REQ-NC-017?"`` -> ``["REQ-NC-017"]``.
    These are what get PROMOTED; ordinary words only inform the conceptual leg.
    """
    found: List[str] = []
    seen: Set[str] = set()
    for pattern in _ID_PATTERNS:
        for match in pattern.findall(query or ""):
            token = match.strip().rstrip(".,;:")
            if token and token not in seen:
                seen.add(token)
                found.append(token)
    return found[:10]


def extract_terms(query: str) -> List[str]:
    """Lexical terms for the token-matching leg: the identifiers plus any
    distinctive words. Stop-words are dropped so a natural-language question does
    not lexically match every document in the workspace."""
    terms = extract_identifiers(query)
    seen = set(terms)
    for word in _WORD_RE.findall(query or ""):
        if word.lower() in _STOP or word in seen:
            continue
        seen.add(word)
        terms.append(word)
    return terms[:12]


def matches_term(text: str, term: str, *, case_sensitive: bool = True) -> bool:
    """Whether *term* occurs in *text* as a COMPLETE token.

    Delegates to :mod:`openmind.tokenmatch` for identifiers and dotted literals.
    API paths need their own rule: ``/`` is not an identifier character, so
    ``tokenmatch`` cannot see ``/name-check`` as one token, and a plain substring
    test would match ``/name-checker`` too. The boundary here is "the next
    character is not part of a path segment", which keeps ``/name-check`` from
    matching ``/name-check-v2`` while still matching ``/name-check/{caseId}`` —
    the sub-path genuinely IS under the queried path.
    """
    if not term:
        return False
    if not term.startswith("/"):
        return tokenmatch.match_kind(text, term,
                                     case_sensitive=case_sensitive) is not None
    haystack = text if case_sensitive else text.lower()
    needle = term if case_sensitive else term.lower()
    start = 0
    while True:
        at = haystack.find(needle, start)
        if at < 0:
            return False
        before = haystack[at - 1] if at > 0 else " "
        after_index = at + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else " "
        if not (before.isalnum() or before in "-_") and \
                not (after.isalnum() or after in "-_"):
            return True
        start = at + 1


def _rrf(rank_lists: Iterable[List[str]], k0: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in rank_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k0 + rank + 1)
    return scores


def _where(asset_type: Optional[str], parser: Optional[str],
           block_type: Optional[str], logical_key: Optional[str],
           asset_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """A Chroma ``where`` clause for the supported filters, or None.

    Chroma needs ``$and`` for more than one condition; a single condition must be
    passed bare, which is why this is not just a dict comprehension.
    """
    clauses = []
    for field, value in (("asset_type", asset_type), ("parser_name", parser),
                         ("block_type", block_type),
                         ("logical_key", logical_key), ("asset_id", asset_id)):
        if value:
            clauses.append({field: {"$eq": value}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _active_asset_ids(workspace_id: str) -> Set[str]:
    """Assets whose document projection is live AND whose Asset is active.

    A removed document keeps its Revisions, Segments, Evidence and blobs, but its
    chunks are deleted from the collection — this set is the backstop for the
    window between those two facts, and for any chunk a failed commit stranded.
    """
    index = db.list_document_index(workspace_id)
    if not index:
        return set()
    active: Set[str] = set()
    for asset_id in index:
        asset = db.get_asset(workspace_id, asset_id)
        if asset and asset.get("state") == "active":
            active.add(asset_id)
    return active


def _excerpt(document: str) -> str:
    """Strip the structural header the chunk was embedded with, and bound it.

    The header (``// document: ...``) is there to put the title and section into
    the EMBEDDING; showing it back to a reader as if it were document content
    would be misleading.
    """
    body = document or ""
    lines = body.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("// "):
            start = i + 1
            continue
        break
    text = "\n".join(lines[start:]).strip()
    return text[:EXCERPT_CHARS]


def search(workspace_id: str, query: str, limit: int = 20, *,
           asset_type: Optional[str] = None, parser: Optional[str] = None,
           block_type: Optional[str] = None, logical_key: Optional[str] = None,
           asset_id: Optional[str] = None,
           include_removed: bool = False,
           case_sensitive: bool = True) -> Dict[str, Any]:
    """Bounded, evidence-cited document retrieval.

    Returns ``{query, hits, count, identifiers, query_mode, filters,
    grounding}``. Removed Assets are excluded unless *include_removed*.
    """
    limit = max(1, min(int(limit or 20), MAX_HITS))
    store = vectorstore.get_documents_store(workspace_id)
    where = _where(asset_type, parser, block_type, logical_key, asset_id)
    allowed = None if include_removed else _active_asset_ids(workspace_id)

    pool: Dict[str, Dict[str, Any]] = {}

    def remember(cid: str, document: str, meta: Dict[str, Any],
                 source: str) -> Optional[Dict[str, Any]]:
        if allowed is not None and meta.get("asset_id") not in allowed:
            return None
        record = pool.get(cid)
        if record is None:
            record = pool[cid] = {"doc": document, "meta": meta,
                                  "sources": set(), "kinds": set()}
        record["sources"].add(source)
        return record

    # ---- vector leg (candidates only) ------------------------------------
    vector_rank: List[str] = []
    try:
        qvec = embeddings.embed([query])[0].tolist()
        result = store.query(qvec, n_results=max(limit * 3, 15), where=where)
        for cid, doc, meta in zip(result["ids"], result["documents"],
                                  result["metadatas"]):
            if remember(cid, doc, meta, "vector") is not None:
                vector_rank.append(cid)
    except Exception:
        # Retrieval must degrade, not fail: with no embedding backend the lexical
        # and exact legs still answer, which is exactly the case an exact
        # Requirement-ID lookup depends on.
        vector_rank = []

    # ---- lexical leg: TOKEN-BOUNDARY matching ----------------------------
    identifiers = extract_identifiers(query)
    terms = extract_terms(query)
    lexical_hits: Dict[str, int] = {}
    exact_hits: Set[str] = set()
    if terms:
        scan = store.get(where=where)
        for cid, doc, meta in zip(scan["ids"], scan["documents"],
                                  scan["metadatas"]):
            matched = 0
            for term in terms:
                if matches_term(doc, term, case_sensitive=case_sensitive):
                    matched += 1
                    if term in identifiers:
                        exact_hits.add(cid)
            if matched:
                record = remember(cid, doc, meta, "lexical")
                if record is not None:
                    record["kinds"].add("token")
                    lexical_hits[cid] = matched
                else:
                    exact_hits.discard(cid)

    lexical_rank = [cid for cid, _ in
                    sorted(lexical_hits.items(), key=lambda kv: (-kv[1], kv[0]))]
    fused = _rrf([vector_rank, lexical_rank])

    # When the WHOLE query is one identifier, only chunks that actually contain
    # it are returned — an embedding-similar paragraph is not a hit for
    # "REQ-NC-017", it is a different requirement. This mirrors the code RAG's
    # exact-token mode, so both surfaces answer an identifier the same way.
    bare_identifier = tokenmatch.is_exact_token_query(query) or (
        len(identifiers) == 1 and identifiers[0] == query.strip())
    if bare_identifier and lexical_hits:
        candidates = [cid for cid in pool if cid in lexical_hits]
        query_mode = "exact_token"
    else:
        candidates = list(pool.keys())
        query_mode = "exact_identifier" if exact_hits else "conceptual"

    # An exact identifier match is PROMOTED, not merely up-weighted: a semantic
    # near-miss must never outrank the requirement the user literally named.
    ordered = sorted(
        candidates,
        key=lambda cid: (0 if cid in exact_hits else 1,
                         -fused.get(cid, 0.0),
                         -lexical_hits.get(cid, 0),
                         cid))

    hits = [_hit(cid, pool[cid], fused.get(cid, 0.0), cid in exact_hits)
            for cid in ordered[:limit]]
    return {
        "workspace_id": workspace_id,
        "query": query,
        "hits": hits,
        "count": len(hits),
        "identifiers": identifiers,
        "query_mode": query_mode,
        "filters": {"asset_type": asset_type, "parser": parser,
                    "block_type": block_type, "logical_key": logical_key,
                    "asset_id": asset_id, "include_removed": include_removed},
        "grounding": tokenmatch.GROUNDING_NOTE,
    }


def _hit(chunk_id: str, record: Dict[str, Any], score: float,
         exact: bool) -> Dict[str, Any]:
    meta = record["meta"] or {}
    sources = sorted(record["sources"])
    if exact:
        sources = sorted(set(sources) | {"exact-identifier"})
    heading = str(meta.get("heading_path") or "")
    return {
        "chunk_id": chunk_id,
        "asset_id": meta.get("asset_id", ""),
        "revision_id": meta.get("revision_id", ""),
        "segment_id": meta.get("segment_id", ""),
        "evidence_id": meta.get("evidence_id", ""),
        "logical_key": meta.get("logical_key", ""),
        "title": meta.get("title", ""),
        "asset_type": meta.get("asset_type", ""),
        "block_type": meta.get("block_type", ""),
        "parser": meta.get("parser_name", ""),
        "heading_path": [p for p in heading.split(" > ") if p],
        "locator": _locator_from_meta(meta),
        "excerpt": _excerpt(record["doc"]),
        "score": round(float(score), 6),
        "retrieval_sources": sources,
    }


def _locator_from_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the block's portable locator from the chunk metadata.

    Chroma stores scalars only, so the locator is flattened on write and
    reassembled here — with only the fields that are meaningful for its kind, so
    a spreadsheet hit does not come back carrying ``page: 0``.
    """
    kind = str(meta.get("locator_kind") or "")
    locator: Dict[str, Any] = {"kind": kind,
                               "document": meta.get("logical_key", "")}
    if kind == "text-range":
        locator["startLine"] = int(meta.get("start_line") or 0)
        locator["endLine"] = int(meta.get("end_line") or 0)
    elif kind == "pdf-block":
        locator["page"] = int(meta.get("page") or 0)
    elif kind == "spreadsheet-range":
        locator["sheet"] = str(meta.get("sheet") or "")
    elif kind == "json-pointer":
        locator["pointer"] = str(meta.get("json_pointer") or "")
    heading = str(meta.get("heading_path") or "")
    if heading:
        locator["headingPath"] = [p for p in heading.split(" > ") if p]
    return locator


def search_knowledge(workspace_ids: List[str], query: str, *,
                     code_limit: int = 12,
                     document_limit: int = 12) -> Dict[str, Any]:
    """Code and document candidates, returned SEPARATELY.

    The separation is the contract. A document hit and a code hit appearing
    together is retrieval, not a relationship: this never claims that the
    document *implements*, *refines* or *verifies* the code. Establishing that
    needs semantic verification, which is Phase 4.
    """
    from . import rag

    pids = [p for p in workspace_ids if p]
    code: Dict[str, Any] = {"hits": []}
    try:
        result = rag.retrieve(pids, query, k=code_limit)
        code = {"hits": result.get("code_chunks", [])[:code_limit],
                "query_mode": result.get("query_mode", "")}
    except Exception as exc:
        code = {"hits": [], "error": f"{type(exc).__name__}: {exc}"}

    documents: Dict[str, Any] = {"hits": []}
    doc_hits: List[Dict[str, Any]] = []
    for pid in pids:
        found = search(pid, query, limit=document_limit)
        doc_hits.extend(found["hits"])
    doc_hits.sort(key=lambda h: -h["score"])
    documents = {"hits": doc_hits[:document_limit]}

    return {
        "query": query,
        "workspace_ids": pids,
        "code": code,
        "documents": documents,
        "grounding": {
            "codeCount": len(code["hits"]),
            "documentCount": len(documents["hits"]),
            "note": ("Code and document results are retrieved independently. "
                     "A document result appearing beside a code result is NOT a "
                     "claim that one implements, refines or verifies the other."),
        },
    }


__all__ = ["MAX_HITS", "EXCERPT_CHARS", "extract_identifiers", "extract_terms",
           "search", "search_knowledge"]
