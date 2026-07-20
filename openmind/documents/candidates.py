"""Deterministic candidate association between a document and existing knowledge.

WHY "CANDIDATE" AND NOT "RELATION"
----------------------------------
Appending a document to a workspace should immediately tell you what it might
touch. But "this requirement document mentions `NameCheckService`" and "this
requirement is implemented by `NameCheckService`" are completely different
claims, and only the first is observable without semantic verification. So this
module produces the first kind only.

`implements`, `refines`, `verifies` and `contradicts` are deliberately absent
from :class:`~openmind.domain.types.CandidateType`. Every result carries
``status: "candidate"``, and **nothing here is persisted** — candidates are
recomputed on demand, so no unverified assertion can accumulate in the database
and later be mistaken for a fact. Canonical Relations are Phase 4.

SIGNAL ORDER AND CONFIDENCE
---------------------------
Deterministic signals run first and semantic retrieval runs last, because that
ordering is what keeps confidence honest:

    high    an exact, explicit identifier matched a real target
            (a workspace file path, a code symbol, a glossary term)
    medium  an exact match after deterministic normalization
            (an API path + method, a configuration key, a database object)
    low     semantic retrieval only

Nothing above ``low`` is ever produced by similarity. A candidate is only
emitted when its TARGET actually exists in the workspace — a document mentioning
`OrderService` when no such symbol was ever indexed produces nothing, because
there is no relationship to be a candidate for.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..domain.types import (CANDIDATE_STATUS, CandidateConfidence, CandidateType)

#: Hard ceiling on returned candidates, whatever a caller asks for.
MAX_CANDIDATES = 100

#: Requirement-like ids: REQ-NC-017, ADR-12, BR-4711.
_REQUIREMENT_RE = re.compile(r"\b(?:REQ|ADR|BR|NFR|AC|US)-[A-Z0-9]{1,8}"
                             r"(?:-\d{1,6})?\b")
#: Change-request / ticket ids: ABC-1234. Deliberately requires >=2 letters and
#: a plain numeric tail, so it cannot swallow a requirement id.
_TICKET_RE = re.compile(r"\b[A-Z]{2,10}-\d{1,6}\b")
#: Error codes: NC-100, E-4021.
_ERROR_CODE_RE = re.compile(r"\b[A-Z]{1,5}-\d{2,5}\b")
#: Dotted configuration keys / topic names: namecheck.review.timeout.minutes
_CONFIG_KEY_RE = re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_]*){2,}\b")
#: API path, optionally preceded by an HTTP method.
_API_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+(/[\w\-{}/]*)|"
    r"(?<![\w/])(/[a-zA-Z][\w\-]*(?:/[\w\-{}]+)*)")
#: A workspace-relative file path with an extension.
_FILE_RE = re.compile(r"\b(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b")
#: A code-shaped identifier worth looking up as a symbol.
_SYMBOL_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*"
                        r"(?:[A-Z][A-Za-z0-9]*)+\b")

#: Per-signal scan bound. A pathological document must not turn candidate
#: association into an unbounded cross product.
_MAX_MENTIONS_PER_SIGNAL = 40


class Candidate:
    """One observed, unverified connection."""

    __slots__ = ("candidate_type", "confidence", "reason", "document_evidence",
                 "target", "target_evidence", "retrieval_method", "mention")

    def __init__(self, candidate_type: str, confidence: str, reason: str,
                 document_evidence: Dict[str, Any], target: Dict[str, Any],
                 retrieval_method: str, mention: str = "",
                 target_evidence: Optional[Dict[str, Any]] = None) -> None:
        self.candidate_type = candidate_type
        self.confidence = confidence
        self.reason = reason
        self.document_evidence = document_evidence
        self.target = target
        self.target_evidence = target_evidence or {}
        self.retrieval_method = retrieval_method
        self.mention = mention

    def key(self) -> Tuple[str, str, str, str]:
        return (self.candidate_type, self.mention,
                str(self.target.get("kind", "")), str(self.target.get("id", "")))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_type": self.candidate_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "mention": self.mention,
            "document_evidence": dict(self.document_evidence),
            "target": dict(self.target),
            "target_evidence": dict(self.target_evidence),
            "retrieval_method": self.retrieval_method,
            # Never omitted. A consumer must not be able to read a candidate as
            # a confirmed relation just because it forgot to check.
            "status": CANDIDATE_STATUS,
        }


_CONFIDENCE_RANK = {CandidateConfidence.HIGH: 0, CandidateConfidence.MEDIUM: 1,
                    CandidateConfidence.LOW: 2}


def find_candidates(workspace_id: str, asset_id: str, *, limit: int = 30,
                    repo: Any = None, searcher: Any = None) -> Dict[str, Any]:
    """Deterministic candidates for one document Asset's CURRENT revision.

    Reads the document's stored Segments (never re-parses), extracts explicit
    identifiers from their text, and resolves each against what the workspace
    actually knows. Returns
    ``{workspace_id, asset_id, revision_id, candidates, count, signals,
    status}``.
    """
    from .. import db as db_module
    repository = repo if repo is not None else db_module
    limit = max(1, min(int(limit or 30), MAX_CANDIDATES))

    asset = repository.get_asset(workspace_id, asset_id)
    if not asset:
        return {"workspace_id": workspace_id, "asset_id": asset_id,
                "revision_id": "", "candidates": [], "count": 0,
                "signals": {}, "status": CANDIDATE_STATUS}
    revision_id = asset.get("current_revision_id") or ""
    blocks = _document_blocks(repository, workspace_id, revision_id)

    index = _WorkspaceIndex(repository, workspace_id, exclude_asset=asset_id)
    found: Dict[Tuple[str, str, str, str], Candidate] = {}
    signals: Dict[str, int] = {}

    for scan in (_scan_files, _scan_symbols, _scan_requirements,
                 _scan_code_identifiers, _scan_api, _scan_config_keys,
                 _scan_database_objects, _scan_glossary):
        for candidate in scan(blocks, index):
            key = candidate.key()
            existing = found.get(key)
            if existing is None or _better(candidate, existing):
                found[key] = candidate
            signals[candidate.retrieval_method] = \
                signals.get(candidate.retrieval_method, 0) + 1

    deterministic = list(found.values())
    if len(deterministic) < limit:
        for candidate in _scan_semantic(workspace_id, asset, blocks, index,
                                        limit - len(deterministic), searcher):
            key = candidate.key()
            if key not in found:
                found[key] = candidate
                signals[candidate.retrieval_method] = \
                    signals.get(candidate.retrieval_method, 0) + 1

    ordered = sorted(found.values(),
                     key=lambda c: (_CONFIDENCE_RANK.get(c.confidence, 3),
                                    c.candidate_type, c.mention))
    return {
        "workspace_id": workspace_id,
        "asset_id": asset_id,
        "revision_id": revision_id,
        "candidates": [c.as_dict() for c in ordered[:limit]],
        "count": min(len(ordered), limit),
        "total_found": len(ordered),
        "signals": signals,
        # Repeated at the top level so a caller reading only the envelope still
        # sees it.
        "status": CANDIDATE_STATUS,
        "note": ("These are OBSERVED MENTIONS, not verified relationships. "
                 "No candidate asserts that the document implements, refines, "
                 "verifies or contradicts its target."),
    }


def _better(new: Candidate, old: Candidate) -> bool:
    return _CONFIDENCE_RANK.get(new.confidence, 3) < \
        _CONFIDENCE_RANK.get(old.confidence, 3)


def _document_blocks(repo: Any, workspace_id: str,
                     revision_id: str) -> List[Dict[str, Any]]:
    """The document's stored segments with their evidence ids. Bounded."""
    if not revision_id:
        return []
    segments = repo.list_segments(workspace_id, revision_id, limit=1000)
    evidence = repo.evidence_ids_for_revision(workspace_id, revision_id)
    out: List[Dict[str, Any]] = []
    for segment in segments:
        excerpt = ""
        record = repo.get_evidence_for_segment(workspace_id, segment["id"])
        if record:
            excerpt = record.get("excerpt", "")
        out.append({
            "segment_id": segment["id"],
            "evidence_id": evidence.get(segment["id"], ""),
            "block_type": segment["segment_type"],
            "text": excerpt,
            "locator": (record or {}).get("locator", {}),
        })
    return out


class _WorkspaceIndex:
    """What the workspace already knows, loaded once.

    Every lookup is an EXACT match against something that genuinely exists. A
    mention with no real target produces no candidate — an unresolvable
    "candidate" is noise, not a finding.
    """

    def __init__(self, repo: Any, workspace_id: str,
                 exclude_asset: str = "") -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.exclude_asset = exclude_asset
        self._assets: Dict[str, Dict[str, Any]] = {}
        self._symbols: Optional[Dict[str, Dict[str, Any]]] = None
        self._glossary: Optional[Dict[str, Dict[str, Any]]] = None
        self._db_objects: Optional[Dict[str, Dict[str, Any]]] = None
        for key, record in repo.list_asset_index(workspace_id).items():
            if record.get("asset_id") != exclude_asset:
                self._assets[key] = record

    # -- files ----------------------------------------------------------
    def file(self, path: str) -> Optional[Dict[str, Any]]:
        record = self._assets.get(path)
        if record:
            return {"kind": "file", "id": record["asset_id"],
                    "logical_key": path}
        # A document usually cites a bare filename, not a full workspace path.
        # A UNIQUE basename match is still exact; an ambiguous one is not a
        # match at all, because naming the wrong file is worse than no candidate.
        matches = [k for k in self._assets if k.rsplit("/", 1)[-1] == path]
        if len(matches) == 1:
            return {"kind": "file", "id": self._assets[matches[0]]["asset_id"],
                    "logical_key": matches[0]}
        return None

    # -- symbols --------------------------------------------------------
    @property
    def symbols(self) -> Dict[str, Dict[str, Any]]:
        if self._symbols is None:
            self._symbols = {}
            for name, record in self.repo.list_workspace_symbols(
                    self.workspace_id, exclude_asset=self.exclude_asset).items():
                self._symbols[name] = record
        return self._symbols

    def symbol(self, name: str) -> Optional[Dict[str, Any]]:
        return self.symbols.get(name)

    # -- glossary -------------------------------------------------------
    @property
    def glossary(self) -> Dict[str, Dict[str, Any]]:
        if self._glossary is None:
            self._glossary = {}
            try:
                from .. import glossary as glossary_module, mapio
                document = mapio.load_glossary(self.workspace_id)
                for entry in (document or {}).get("terms", []) or []:
                    term = str(entry.get("term") or "").strip()
                    if term:
                        self._glossary[term] = entry
            except Exception:
                self._glossary = {}
        return self._glossary

    # -- database objects ------------------------------------------------
    @property
    def database_objects(self) -> Dict[str, Dict[str, Any]]:
        """Table and view names, taken ONLY from ``sql-object`` segments.

        That restriction matters. A ``.sql`` file ingested through the CODE
        pipeline is segmented into generic line ranges whose symbol is the
        filename, so accepting any symbol from a ``database-schema`` asset would
        make the word "sql" a database object and match it in every document.
        A real object name exists only where the SQL parser structured one.
        """
        if self._db_objects is None:
            self._db_objects = {}
            for name, record in self.symbols.items():
                if record.get("segment_type") != "sql-object":
                    continue
                bare = name.rsplit(".", 1)[-1].strip()
                if len(bare) >= 3:
                    self._db_objects.setdefault(bare.lower(), record)
        return self._db_objects


def _mentions(blocks: Sequence[Dict[str, Any]], pattern: re.Pattern,
              group: int = 0) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield ``(mention, block)`` for each distinct pattern match, bounded."""
    seen: Set[str] = set()
    emitted = 0
    for block in blocks:
        for match in pattern.finditer(block.get("text") or ""):
            value = (match.group(group) or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            emitted += 1
            yield value, block
            if emitted >= _MAX_MENTIONS_PER_SIGNAL:
                return


def _document_evidence(block: Dict[str, Any]) -> Dict[str, Any]:
    return {"segment_id": block.get("segment_id", ""),
            "evidence_id": block.get("evidence_id", ""),
            "block_type": block.get("block_type", ""),
            "locator": dict(block.get("locator") or {})}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def _scan_files(blocks, index) -> Iterable[Candidate]:
    for mention, block in _mentions(blocks, _FILE_RE):
        target = index.file(mention)
        if target is None:
            continue
        yield Candidate(
            CandidateType.MENTIONS_FILE, CandidateConfidence.HIGH,
            f"the document names {mention!r}, which is an indexed workspace file",
            _document_evidence(block), target, "exact-file-path", mention)


def _scan_symbols(blocks, index) -> Iterable[Candidate]:
    for mention, block in _mentions(blocks, _SYMBOL_RE):
        target = index.symbol(mention)
        if target is None:
            continue
        yield Candidate(
            CandidateType.MENTIONS_SYMBOL, CandidateConfidence.HIGH,
            f"the document names {mention!r}, which is an indexed code symbol",
            _document_evidence(block),
            {"kind": "symbol", "id": target.get("segment_id", ""),
             "symbol": mention, "asset_id": target.get("asset_id", ""),
             "logical_key": target.get("logical_key", "")},
            "exact-symbol", mention,
            {"segment_id": target.get("segment_id", ""),
             "evidence_id": target.get("evidence_id", "")})


def _scan_requirements(blocks, index) -> Iterable[Candidate]:
    """Requirement-like ids shared with ANOTHER document.

    A requirement id that appears only in this document connects it to nothing,
    so nothing is emitted for it. The finding is the SHARED id — a test case and
    a specification naming the same requirement.
    """
    for mention, block in _mentions(blocks, _REQUIREMENT_RE):
        for target in _documents_mentioning(index, mention):
            yield Candidate(
                CandidateType.MENTIONS_DOCUMENT, CandidateConfidence.HIGH,
                f"both documents contain the identifier {mention!r}",
                _document_evidence(block), target, "exact-requirement-id",
                mention, {"evidence_id": target.pop("_evidence_id", "")})


def _classify_code(mention: str) -> Optional[Tuple[str, str, str]]:
    """``(candidate_type, retrieval_method, noun)`` for a ``LETTERS-DIGITS`` id.

    ``REQ-NC-017``, ``ABC-1234`` and ``NC-100`` all share one lexical shape, so a
    mention must be classified ONCE and by a stated rule — otherwise the same
    ``NC-100`` comes back as both a ticket and an error code, which reads like
    two findings when there is one.

    The rule, in order:

    1. a known requirement prefix (REQ/ADR/BR/NFR/AC/US) -> requirement;
    2. a project-key shape — at least three letters AND at least four digits,
       the JIRA convention -> ticket;
    3. otherwise -> error code.

    Rules 2 and 3 are a CONVENTION, not a certainty; the emitted reason says so
    rather than asserting the interpretation.
    """
    if _REQUIREMENT_RE.fullmatch(mention):
        return None                        # handled by _scan_requirements
    head, _, tail = mention.partition("-")
    if len(head) >= 3 and len(tail) >= 4 and tail.isdigit():
        return (CandidateType.MENTIONS_DOCUMENT, "exact-ticket-id",
                "change-request or ticket identifier")
    return (CandidateType.MENTIONS_CONFIGURATION, "normalized-error-code",
            "error code")


def _scan_code_identifiers(blocks, index) -> Iterable[Candidate]:
    """Tickets and error codes — one classification per mention (see
    :func:`_classify_code`)."""
    seen: Set[str] = set()
    for pattern in (_TICKET_RE, _ERROR_CODE_RE):
        for mention, block in _mentions(blocks, pattern):
            if mention in seen:
                continue
            seen.add(mention)
            classified = _classify_code(mention)
            if classified is None:
                continue
            candidate_type, method, noun = classified
            confidence = (CandidateConfidence.HIGH
                          if method == "exact-ticket-id"
                          else CandidateConfidence.MEDIUM)
            for target in _documents_mentioning(index, mention):
                yield Candidate(
                    candidate_type, confidence,
                    f"both documents contain {mention!r}, which matches the "
                    f"{noun} convention",
                    _document_evidence(block), target, method, mention,
                    {"evidence_id": target.pop("_evidence_id", "")})


def _scan_api(blocks, index) -> Iterable[Candidate]:
    seen: Set[str] = set()
    emitted = 0
    for block in blocks:
        for match in _API_RE.finditer(block.get("text") or ""):
            method = (match.group(1) or "").upper()
            path = (match.group(2) or match.group(3) or "").strip()
            if not path or len(path) < 2:
                continue
            mention = f"{method} {path}".strip()
            if mention in seen:
                continue
            seen.add(mention)
            for target in _documents_mentioning(index, path):
                emitted += 1
                yield Candidate(
                    CandidateType.MENTIONS_API, CandidateConfidence.MEDIUM,
                    f"both documents describe the API path {path!r}",
                    _document_evidence(block), target, "normalized-api-path",
                    mention, {"evidence_id": target.pop("_evidence_id", "")})
            if emitted >= _MAX_MENTIONS_PER_SIGNAL:
                return


def _scan_config_keys(blocks, index) -> Iterable[Candidate]:
    """Dotted keys: configuration properties and message/topic names alike.

    They are the same lexical shape and the same kind of observation, so they
    share a signal rather than being split by a guess about which one a given
    key is.
    """
    for mention, block in _mentions(blocks, _CONFIG_KEY_RE):
        for target in _documents_mentioning(index, mention):
            yield Candidate(
                CandidateType.MENTIONS_CONFIGURATION, CandidateConfidence.MEDIUM,
                f"both documents reference the key or topic {mention!r}",
                _document_evidence(block), target, "normalized-config-key",
                mention, {"evidence_id": target.pop("_evidence_id", "")})
        symbol = index.symbol(mention)
        if symbol is not None:
            yield Candidate(
                CandidateType.MENTIONS_CONFIGURATION, CandidateConfidence.MEDIUM,
                f"the document references {mention!r}, which appears in "
                f"indexed configuration",
                _document_evidence(block),
                {"kind": "configuration", "id": symbol.get("segment_id", ""),
                 "symbol": mention, "asset_id": symbol.get("asset_id", ""),
                 "logical_key": symbol.get("logical_key", "")},
                "normalized-config-key", mention)


def _scan_database_objects(blocks, index) -> Iterable[Candidate]:
    objects = index.database_objects
    if not objects:
        return
    seen: Set[str] = set()
    for block in blocks:
        for word in re.findall(r"\b[a-z][a-z0-9_]{2,}\b",
                               (block.get("text") or "").lower()):
            if word in seen or word not in objects:
                continue
            seen.add(word)
            target = objects[word]
            yield Candidate(
                CandidateType.MENTIONS_DATABASE_OBJECT,
                CandidateConfidence.MEDIUM,
                f"the document names {word!r}, which is a database object in "
                f"the indexed schema",
                _document_evidence(block),
                {"kind": "database-object", "id": target.get("segment_id", ""),
                 "symbol": word, "asset_id": target.get("asset_id", ""),
                 "logical_key": target.get("logical_key", "")},
                "exact-database-object", word)
            if len(seen) >= _MAX_MENTIONS_PER_SIGNAL:
                return


def _scan_glossary(blocks, index) -> Iterable[Candidate]:
    terms = index.glossary
    if not terms:
        return
    seen: Set[str] = set()
    for block in blocks:
        text = block.get("text") or ""
        for term, entry in terms.items():
            if term in seen or len(term) < 3:
                continue
            from .. import tokenmatch
            if tokenmatch.match_kind(text, term, case_sensitive=True) != "token":
                continue
            seen.add(term)
            yield Candidate(
                CandidateType.MENTIONS_DOCUMENT, CandidateConfidence.HIGH,
                f"the document uses the glossary term {term!r}",
                _document_evidence(block),
                {"kind": "glossary-term", "id": term, "symbol": term,
                 "logical_key": str(entry.get("source_file") or "")},
                "exact-glossary-term", term)
            if len(seen) >= _MAX_MENTIONS_PER_SIGNAL:
                return


def _documents_mentioning(index: _WorkspaceIndex,
                          token: str) -> Iterable[Dict[str, Any]]:
    """Other DOCUMENTS in the workspace whose indexed blocks contain *token* as a
    complete token. Uses the document collection, so it costs one bounded query
    rather than a scan of every stored segment."""
    from .. import document_rag
    try:
        result = document_rag.search(index.workspace_id, token, limit=5)
    except Exception:
        return
    for hit in result.get("hits", []):
        if hit.get("asset_id") == index.exclude_asset:
            continue
        if "exact-identifier" not in (hit.get("retrieval_sources") or []):
            continue
        yield {"kind": "document", "id": hit.get("asset_id", ""),
               "logical_key": hit.get("logical_key", ""),
               "title": hit.get("title", ""),
               "segment_id": hit.get("segment_id", ""),
               "_evidence_id": hit.get("evidence_id", "")}


def _scan_semantic(workspace_id: str, asset: Dict[str, Any],
                   blocks: Sequence[Dict[str, Any]], index: _WorkspaceIndex,
                   budget: int, searcher: Any) -> Iterable[Candidate]:
    """The fallback leg: purely embedding-similar code, labelled LOW.

    Runs last and only fills the remaining budget, so a deterministic finding is
    never displaced by a similarity guess. It is the ONE signal allowed to
    produce a candidate without an exact match, and its confidence says so.
    """
    if budget <= 0 or not blocks:
        return
    query = " ".join((b.get("text") or "")[:200] for b in blocks[:3]).strip()
    if not query:
        return
    search = searcher
    if search is None:
        from .. import rag

        def search(q: str) -> List[Dict[str, Any]]:
            return rag.retrieve([workspace_id], q, k=budget).get(
                "code_chunks", [])
    try:
        results = search(query)
    except Exception:
        return
    for chunk in list(results)[:budget]:
        path = chunk.get("file_path") or ""
        if not path:
            continue
        yield Candidate(
            CandidateType.SIMILAR_CONTENT, CandidateConfidence.LOW,
            "retrieved by embedding similarity only; no explicit identifier "
            "in the document matches this code",
            {"segment_id": blocks[0].get("segment_id", ""),
             "evidence_id": blocks[0].get("evidence_id", ""),
             "block_type": blocks[0].get("block_type", ""),
             "locator": dict(blocks[0].get("locator") or {})},
            {"kind": "code", "id": chunk.get("id", ""), "logical_key": path,
             "symbol": chunk.get("symbol") or ""},
            "semantic-retrieval", path)


__all__ = ["MAX_CANDIDATES", "Candidate", "find_candidates"]
