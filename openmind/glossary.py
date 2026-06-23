"""Deterministic glossary — a FIRST-CLASS, build-time artifact.

WHY THIS EXISTS (design intent)
-------------------------------
Acronym/term resolution is the canonical RAG failure mode: a definition lives in
one line of one document, and similarity retrieval simply fails to surface it for
a query like "what does ISR mean?" — that single chunk ranks below a dozen
noisier-but-denser ones, so the model never sees it and either stays silent or
guesses. We refuse to leave term resolution to chance.

So the glossary is NOT similarity-retrieved chunks. It is a deterministic,
PERSISTED MAP — term -> {definition, source_file, line_number, content_hash} —
extracted ONCE at indexing time and simply QUERIED thereafter. At query time an
exact-token lookup hits this map directly and pulls ONLY the relevant entry.

THE HARD RULE: VERBATIM, NEVER GENERATED
----------------------------------------
A term's definition is the EXACT original text taken from an authoritative source
in the project. The model is NEVER asked to write, rewrite, summarize, paraphrase,
or "improve" a definition. Extraction is pure, boundary-checked pattern matching —
this module uses NO LLM at all. If no authoritative definition exists for a term
it is reported "no authoritative definition found" — never filled in by a guess.
Reliability comes from exact-token extraction, not from model judgment.

AUTHORITATIVE SOURCES (priority order; the best source wins on a collision)
---------------------------------------------------------------------------
  1. Dedicated glossary / acronym / terminology files (GLOSSARY.md, ACRONYMS.*, ...)
  2. Structured definition patterns anywhere — markdown tables ("| Term | Definition |"),
     definition lists ("Term" then ": definition"), and "TERM: definition" /
     "TERM - definition" lines
  3. README and the docs/ folder (acronym expansions like "In-Sync Replicas (ISR)")
  4. Doc-comments in code (Javadoc / line / block / hash comments)

Every entry records provenance — the source file, the 1-based line, and the SHA-1
content hash of the file it came from — so the definition shown in the UI is
always traceable back to (and can jump to) the real file:line it came from.

NO PROJECT ASSUMPTIONS
----------------------
There is NO hardcoded term list. We extract only what a project's authoritative
sources actually contain; a project with none yields an empty-but-honest glossary
(never fabricated terms). This works for a monolith or a multi-module system alike.

INCREMENTAL UPKEEP
------------------
:func:`build_glossary` reuses the prior artifact keyed by per-file content hash: a
source whose hash is unchanged is NEVER re-scanned — its entries are carried over
verbatim. Editing a definition source re-extracts only that file and refreshes its
provenance/hash. This reuses the exact same per-file hash the RAG index maintains.

OPTIONAL EXTERNAL STANDARD DEFINITION (kept strictly separate)
-------------------------------------------------------------
An entry MAY additionally carry a ``standard_definition`` — an authoritative
definition of the term looked up from an external reference (Wikipedia) by the
``wikipedia-glossary`` search skill and written back via :func:`set_standard_definition`.
This is a SEPARATE, ATTRIBUTED field; it never replaces the verbatim in-project
``definition`` above, and the UI labels it and footnotes its provenance. The
verbatim-never-generated contract for the project's own definition is unchanged.
Two honesty guarantees hold: (1) a standard definition is only ever attached to a
term the project's OWN sources already surfaced — no new terms are invented; and
(2) Open Mind itself makes NO web request — the skill (run by the agent) is the
only network actor, preserving the local-only invariant. The field is keyed to the
term, so an incremental re-extract of a changed source preserves it.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import tokenmatch

# Bump when the stored entry SHAPE changes — a prior artifact written under a
# different schema is ignored (fully re-extracted) rather than carried over
# half-migrated. The current entry shape is:
#   term -> {term, definition, source_file, line_number, content_hash, source_kind}
SCHEMA_VERSION = 3

# Optional, additive fields holding an external (Wikipedia) standard definition.
# They are kept separate from the verbatim in-project definition and are carried
# across incremental rebuilds (keyed to the term, not to file content). Because
# they are purely additive/optional, adding them does NOT change SCHEMA_VERSION.
#   standard_status: "matched" (has a standard_definition) | "none" (looked up,
#     no confident match). Its PRESENCE means the term was ATTEMPTED — this is the
#     completeness marker the auto-enrichment recovery relies on (a term with no
#     status and no definition has never been attempted and must still be done).
_STD_FIELDS = ("standard_definition", "standard_html", "standard_source",
               "standard_url", "standard_title", "standard_retrieved",
               "standard_status", "standard_attempted_at")

# Sources that DEFINE terms. Docs are scanned whole; code is scanned in its
# COMMENTS only (Javadoc / line / block / hash comments) so identifiers in code
# are never mistaken for prose definitions.
_DOC_EXTS = {".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt", ".html", ".htm"}
_SLASH_COMMENT_EXTS = {".java", ".scala", ".kt", ".kts", ".groovy", ".gradle",
                       ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go",
                       ".cs", ".rs", ".css", ".scss", ".less", ".c", ".h", ".cpp"}
_HASH_COMMENT_EXTS = {".py", ".rb", ".yml", ".yaml", ".properties", ".sh", ".toml"}

# Small closed-class words skipped when checking acronym/initials consistency.
_STOP = {"of", "the", "and", "for", "to", "in", "a", "an", "on", "by", "with",
         "or", "at", "as", "&", "per", "from"}

# An acronym in parentheses: "(ISR)". We look BACK over the preceding words for the
# shortest span whose initials match — robust to leading filler.
_PAREN_ACRO = re.compile(r"\(([A-Z]{2,7}s?)\)")
_WORD = re.compile(r"[A-Za-z][\w&.'+-]*")
# Acronym-then-expansion:  "ISR (in-sync replicas)"
_REVERSE = re.compile(r"\b([A-Z]{2,7}s?)\s*\(([A-Za-z][^)]{3,80})\)")
# A definition line (docs only): "Term: description" / "Term — description" /
# "Term - description". The term is captured non-greedily; the delimiter is a
# colon, an en/em dash, or a spaced hyphen.
_DEFLINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|`)?([A-Z][A-Za-z0-9][\w /+.&-]{0,48}?)(?:\*\*|`)?"
    r"\s*(?::|\s—|\s–|\s-)\s+(\S.{0,300})$")
# A markdown definition-list continuation: ": definition" (term is the line above).
_DEFLIST = re.compile(r"^\s*[:~]\s+(\S.{0,300})$")
_HTML_TAG = re.compile(r"<[^>]+>")

# Filenames that mark a file as a DEDICATED terminology source (highest priority).
_GLOSSARY_FILE = re.compile(
    r"(glossar|acronym|terminolog|abbreviation|nomenclature|definitions?)", re.I)
# Generic header/label words that are NOT terms (so a table header or a prose
# lead-in like "Note:" is not mistaken for a defined term).
_HEADER_WORDS = {
    "term", "terms", "acronym", "acronyms", "abbreviation", "abbreviations",
    "name", "key", "field", "column", "parameter", "property", "option",
    "setting", "variable", "word", "concept", "definition", "meaning",
    "description", "expansion", "value", "note", "notes", "warning", "example",
    "see", "todo", "fixme", "usage", "default", "type", "returns", "param",
}

# Lower rank = more authoritative. Used to break ties when a term is defined more
# than once, both within one file and across files.
_KIND_RANK = {"table": 0, "deflist": 1, "defline": 1,
              "acronym": 2, "reverse-acronym": 2}


# ---------------------------------------------------------------------------
# Comment / line iteration (preserves 1-based line numbers for provenance)
# ---------------------------------------------------------------------------
def _scan_lines(text: str, ext: str) -> Iterable[Tuple[int, str]]:
    """Yield (line_no, scannable_text). Docs yield prose (HTML stripped); code
    yields only the text inside comments. Line numbers always refer to the
    ORIGINAL file so provenance is exact."""
    is_doc = ext in _DOC_EXTS
    is_html = ext in (".html", ".htm")
    slash = ext in _SLASH_COMMENT_EXTS
    hashc = ext in _HASH_COMMENT_EXTS
    in_block = False
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw
        if is_doc:
            yield i, (_HTML_TAG.sub(" ", line) if is_html else line)
            continue
        if slash:
            seg = ""
            if in_block:
                end = line.find("*/")
                if end == -1:
                    seg = line
                else:
                    seg = line[:end]
                    in_block = False
                    rest = line[end + 2:]
                    line = rest  # a comment may close then reopen on one line
            if not in_block:
                bstart = line.find("/*")
                lstart = line.find("//")
                if bstart != -1 and (lstart == -1 or bstart < lstart):
                    bend = line.find("*/", bstart + 2)
                    if bend == -1:
                        seg += " " + line[bstart + 2:]
                        in_block = True
                    else:
                        seg += " " + line[bstart + 2:bend]
                elif lstart != -1:
                    seg += " " + line[lstart + 2:]
            seg = seg.strip().lstrip("*").strip()
            if seg:
                yield i, seg
        elif hashc:
            idx = line.find("#")
            if idx != -1:
                seg = line[idx + 1:].strip()
                if seg:
                    yield i, seg


# ---------------------------------------------------------------------------
# Acronym / term validation
# ---------------------------------------------------------------------------
def _norm_acronym(acronym: str) -> str:
    """Drop a trailing plural 's' from an otherwise all-caps acronym (ISRs -> ISR)."""
    if len(acronym) >= 3 and acronym.endswith("s") and acronym[:-1].isupper():
        return acronym[:-1]
    return acronym


def _is_acronym(token: str) -> bool:
    t = _norm_acronym(token)
    return len(t) >= 2 and t[0].isalpha() and t.isupper()


def _initials_ok(acronym: str, expansion: str) -> bool:
    """True when `acronym` is consistent with the initials of the expansion.

    We check the initials of BOTH the full word list and the significant-word
    list (stop-words dropped), accepting either form: "In-Sync Replicas" -> ISR
    needs the FULL list, while "Simple Authentication and Security Layer" -> SASL
    needs the SIGNIFICANT list. This handles both real forms while still rejecting
    noise like "the value (X)".
    """
    ac = _norm_acronym(acronym).upper()
    if len(ac) < 2:
        return False
    words = [w for w in re.split(r"[\s/_-]+", expansion) if w and w[0].isalpha()]
    if len(words) < 2:
        return False
    full = "".join(w[0].upper() for w in words)
    sig = "".join(w[0].upper() for w in words if w.lower() not in _STOP)
    for initials in (full, sig):
        if len(initials) < 2:
            continue
        if ac == initials or initials.startswith(ac):
            return True
        it = iter(initials)            # tolerate skipped words (subsequence)
        if all(ch in it for ch in ac):
            return True
    return False


def _is_term_like(term: str, *, allow_single: bool = False) -> bool:
    """A defline/def-list/table term is accepted only if it reads like a term:
    an acronym, or a Title-Case phrase. A single Title-Case word (e.g. a prose
    lead-in "Note") is accepted ONLY inside a dedicated glossary file, where a
    bare capitalised headword is the norm — elsewhere it is too noisy."""
    term = (term or "").strip()
    if not term:
        return False
    words = term.split()
    if not (1 <= len(words) <= 6):
        return False
    if len(words) == 1 and _is_acronym(words[0]):
        return True
    titleish = all(w[:1].isupper() or w.isupper() for w in words)
    if titleish:
        return len(words) >= 2 or allow_single
    return False


# ---------------------------------------------------------------------------
# Verbatim text extraction helpers
# ---------------------------------------------------------------------------
def _clean_def(s: str) -> str:
    """Trim a captured definition to its VERBATIM core: strip only surrounding
    whitespace and matched surrounding markdown emphasis/code markers. Internal
    text is preserved exactly — never collapsed, reworded, or summarised."""
    s = (s or "").strip()
    for mark in ("**", "`", "*"):
        if len(s) > 2 * len(mark) and s.startswith(mark) and s.endswith(mark):
            s = s[len(mark):-len(mark)].strip()
            break
    return s.rstrip("|").strip()


def _expansion_before(seg: str, paren_start: int, acronym: str) -> Optional[str]:
    """Given the text before "(ACRO)", return the EXACT (verbatim) trailing word
    span whose significant-word initials match the acronym, leading filler
    stripped — or None. Looking back from the paren is far more robust than a
    bounded greedy phrase match: it never grabs the rest of the sentence, and the
    returned text is the literal source slice (not a re-joined approximation)."""
    pre = seg[:paren_start]
    matches = list(_WORD.finditer(pre))
    if not matches:
        return None
    ac = _norm_acronym(acronym)
    cap = min(len(matches), 2 * len(ac) + 3)
    for span in range(1, cap + 1):
        cand = matches[-span:]
        if _initials_ok(ac, " ".join(m.group(0) for m in cand)):
            # strip a leading filler stop-word ONLY when the remaining span still
            # matches the acronym — never drop a word that is an essential initial
            # (e.g. "In" in "In Sync Replicas" -> ISR must be kept).
            while len(cand) > 1 and cand[0].group(0).lower() in _STOP \
                    and _initials_ok(ac, " ".join(m.group(0) for m in cand[1:])):
                cand = cand[1:]
            if cand:
                return pre[cand[0].start():cand[-1].end()]
    return None


def _table_row(line: str) -> Optional[Tuple[str, str]]:
    """Parse a markdown table row "| a | b | ... |" into its first two cells."""
    s = line.strip()
    if not (s.startswith("|") and s.count("|") >= 2):
        return None
    cells = [c.strip() for c in s.strip("|").split("|")]
    if len(cells) < 2:
        return None
    return cells[0].strip(), cells[1].strip()


def _is_separator(cell: str) -> bool:
    """A markdown table separator cell (---, :--:, ===) — never a definition."""
    return bool(re.fullmatch(r"[:\-=\s]+", cell or ""))


def _strip_md(s: str) -> str:
    """Strip surrounding markdown emphasis/code markers and leading list markers
    from a candidate term token."""
    s = re.sub(r"^[-*+]\s+", "", (s or "").strip())
    for mark in ("**", "`", "*"):
        if len(s) > 2 * len(mark) and s.startswith(mark) and s.endswith(mark):
            s = s[len(mark):-len(mark)].strip()
            break
    return s.strip()


# ---------------------------------------------------------------------------
# Extraction (single file)
# ---------------------------------------------------------------------------
def _ext_of(path: str) -> str:
    i = path.rfind(".")
    return path[i:].lower() if i != -1 else ""


def _is_glossary_file(path: str) -> bool:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return _ext_of(path) in _DOC_EXTS and bool(_GLOSSARY_FILE.search(base))


def _definition_source(path: str) -> bool:
    ext = _ext_of(path)
    return ext in _DOC_EXTS or ext in _SLASH_COMMENT_EXTS or ext in _HASH_COMMENT_EXTS


def extract_entries(path: str, text: str, file_hash: str) -> List[Dict[str, Any]]:
    """Deterministically extract glossary entries from ONE source file.

    Each entry is term -> {term, definition (VERBATIM), source_file, line_number,
    content_hash, source_kind}. Within a file, the most authoritative pattern wins
    for a given term (table > definition line/list > acronym expansion)."""
    ext = _ext_of(path)
    if not _definition_source(path):
        return []
    is_doc = ext in _DOC_EXTS
    is_glossary = _is_glossary_file(path)
    best: Dict[str, Dict[str, Any]] = {}

    def offer(term: str, definition: str, line: int, kind: str) -> None:
        term = _norm_acronym((term or "").strip())
        definition = _clean_def(definition)
        if not term or not definition or len(definition) < 2:
            return
        # A genuine expansion never restates the acronym itself. This rejects
        # explanatory parentheticals that merely sit next to the token, e.g.
        # "8 KB (GZIPInputStream uses 0.5 KB by default)" -> not a definition of KB.
        if re.search(rf"\b{re.escape(term)}\b", definition, re.IGNORECASE):
            return
        cand = {"term": term, "definition": definition, "source_file": path,
                "line_number": line, "content_hash": file_hash, "source_kind": kind}
        cur = best.get(term)
        if cur is None or _KIND_RANK.get(kind, 9) < _KIND_RANK.get(cur["source_kind"], 9):
            best[term] = cand

    prev_doc_seg: Optional[str] = None
    for line_no, seg in _scan_lines(text, ext):
        # acronym expansions (any source)
        for m in _PAREN_ACRO.finditer(seg):
            acronym = m.group(1)
            expansion = _expansion_before(seg, m.start(), acronym)
            if expansion:
                offer(acronym, expansion, line_no, "acronym")
        for m in _REVERSE.finditer(seg):
            acronym, expansion = m.group(1), m.group(2)
            if _initials_ok(acronym, expansion):
                offer(acronym, expansion, line_no, "reverse-acronym")
        # structured definition patterns (docs only — code is acronyms-only)
        if is_doc:
            row = _table_row(seg)
            if row:
                term, definition = row
                if (term.lower() not in _HEADER_WORDS and not _is_separator(definition)
                        and _is_term_like(term, allow_single=True)):
                    offer(term, definition, line_no, "table")
            dl = _DEFLIST.match(seg)
            if dl and prev_doc_seg is not None:
                term = _strip_md(prev_doc_seg)
                if (term.lower() not in _HEADER_WORDS
                        and _is_term_like(term, allow_single=is_glossary)):
                    offer(term, dl.group(1), line_no, "deflist")
            dm = _DEFLINE.match(seg)
            if dm:
                term, definition = dm.group(1).strip(), dm.group(2).strip()
                min_len = 1 if is_glossary else 8
                if (term.lower() not in _HEADER_WORDS and len(definition) >= min_len
                        and _is_term_like(term, allow_single=is_glossary)):
                    offer(term, definition, line_no, "defline")
            prev_doc_seg = seg
        else:
            prev_doc_seg = None
    return list(best.values())


# ---------------------------------------------------------------------------
# Priority + build (incremental, hash-keyed) + persist shape
# ---------------------------------------------------------------------------
def _file_rank(source_file: str) -> int:
    """0 = dedicated glossary file, 1 = README/docs folder, 2 = other doc,
    3 = code comment. Lower wins on a term collision across files."""
    if _is_glossary_file(source_file):
        return 0
    low = source_file.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    if _ext_of(source_file) in _DOC_EXTS:
        if base.startswith("readme") or "/docs/" in low or low.startswith("docs/"):
            return 1
        return 2
    return 3


def _priority(entry: Dict[str, Any]) -> Tuple[int, int, str, int]:
    return (_file_rank(entry["source_file"]),
            _KIND_RANK.get(entry.get("source_kind"), 9),
            entry["source_file"], int(entry.get("line_number") or 0))


def build_glossary(files: Iterable[Tuple[str, str, str]],
                   prior: Optional[Dict[str, Any]] = None,
                   cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Build (or incrementally refresh) the glossary artifact.

    `files` is an iterable of (path, text, content_hash). `prior` is the
    previously persisted artifact, if any (ignored when its schema differs, so a
    shape change re-extracts cleanly). A file whose hash matches the prior build
    is NOT re-scanned: its entries are carried over verbatim. On a term collision
    the MOST AUTHORITATIVE source wins (see :func:`_priority`).

    `cancel`, if given, is polled periodically; once it returns True the build
    stops early and returns what it has so far (the caller then aborts the job),
    keeping a Terminate responsive even on a large corpus."""
    prior = prior or {}
    if prior.get("schema") != SCHEMA_VERSION:
        prior = {}     # shape changed -> ignore old artifact, fully re-extract
    prior_hashes: Dict[str, str] = (prior.get("source_hashes") or {})
    prior_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for entry in (prior.get("terms") or {}).values():
        f = entry.get("source_file")
        if f:
            prior_by_file.setdefault(f, []).append(entry)

    chosen: Dict[str, Dict[str, Any]] = {}
    chosen_pri: Dict[str, Tuple[int, int, str, int]] = {}
    source_hashes: Dict[str, str] = {}
    reused = scanned = 0

    # deterministic order: sort by path so ties resolve stably
    candidates = sorted(((p, t, h) for (p, t, h) in files if _definition_source(p)),
                        key=lambda x: x[0])
    for i, (path, text, file_hash) in enumerate(candidates):
        if cancel and (i & 63) == 0 and cancel():
            break
        if prior_hashes.get(path) == file_hash and path in prior_by_file:
            entries = prior_by_file[path]      # unchanged source: never re-parsed
            reused += 1
        else:
            entries = extract_entries(path, text, file_hash)   # changed/new: re-extract
            scanned += 1
        source_hashes[path] = file_hash
        for e in entries:
            pri = _priority(e)
            cur = chosen_pri.get(e["term"])
            if cur is None or pri < cur:
                chosen[e["term"]] = e
                chosen_pri[e["term"]] = pri

    # Carry over external standard definitions (Wikipedia enrichment). These are
    # keyed to the TERM, not to file content, so a re-extract of a changed source
    # must not drop them — they are re-attached to the surviving entry. They stay a
    # separate, attributed field and never replace the verbatim definition.
    prior_terms = (prior.get("terms") or {})
    for term, e in chosen.items():
        pe = prior_terms.get(term)
        # carry BOTH a matched definition AND a "none" attempt marker — otherwise a
        # re-extract would drop the marker and the term would be re-looked-up forever.
        if (pe and (pe.get("standard_definition") or pe.get("standard_status"))
                and not e.get("standard_definition") and not e.get("standard_status")):
            for f in _STD_FIELDS:
                if pe.get(f) is not None:
                    e[f] = pe[f]

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "terms": dict(sorted(chosen.items())),
        "source_hashes": source_hashes,
        "stats": {"term_count": len(chosen), "source_count": len(source_hashes),
                  "sources_scanned": scanned, "sources_reused": reused},
    }


# ---------------------------------------------------------------------------
# Lookup (exact-token, deterministic) + query routing
# ---------------------------------------------------------------------------
def get_glossary(doc: Dict[str, Any], term: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a term against the persisted map — exact-token, no similarity, no
    model guess. With no `term`, return the full term list (term -> definition).

    A present term returns {found: True, term, definition, source_file,
    line_number, content_hash, source_kind}. An absent term returns
    {found: False, ..., message: 'no authoritative definition found ...'} — the
    definition is NEVER fabricated.
    """
    entries: Dict[str, Any] = doc.get("terms", {}) or {}
    if term is None:
        wiki = sorted(k for k, v in entries.items() if v.get("standard_definition"))
        return {"terms": {k: v.get("definition", "") for k, v in entries.items()},
                "count": len(entries), "wiki": wiki}

    q = tokenmatch.strip_quotes(term).strip()
    norm = _norm_acronym(q)
    # exact key, then case-insensitive exact-token, then plural-normalised
    lower = {k.lower(): k for k in entries}
    key = None
    for cand in (q, norm):
        if cand in entries:
            key = cand
            break
        if cand.lower() in lower:
            key = lower[cand.lower()]
            break
    if key is None:
        return {"found": False, "term": q,
                "message": f"no authoritative definition found for '{q}' "
                           "in the indexed project"}
    e = entries[key]
    return {"found": True, "term": e["term"], "definition": e.get("definition", ""),
            "source_file": e.get("source_file"), "line_number": e.get("line_number"),
            "content_hash": e.get("content_hash"), "source_kind": e.get("source_kind", ""),
            "standard_definition": e.get("standard_definition"),
            "standard_html": e.get("standard_html"),
            "standard_source": e.get("standard_source"),
            "standard_url": e.get("standard_url"),
            "standard_title": e.get("standard_title"),
            "standard_retrieved": e.get("standard_retrieved")}


def _resolve_key(entries: Dict[str, Any], term: str) -> Optional[str]:
    """Find the stored key for `term` using the same matching as lookups: exact,
    then case-insensitive, then plural-normalised. Returns None if absent."""
    q = tokenmatch.strip_quotes(term or "").strip()
    norm = _norm_acronym(q)
    lower = {k.lower(): k for k in entries}
    for cand in (q, norm):
        if cand in entries:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def set_standard_definition(doc: Dict[str, Any], term: str, *, definition: str,
                            html: Optional[str] = None, url: Optional[str] = None,
                            title: Optional[str] = None,
                            retrieved: Optional[str] = None) -> bool:
    """Attach (or clear) an EXTERNAL standard definition on an already-extracted
    term, in place. Honesty rules: it is only ever set for a term the project's own
    sources already surfaced (an unknown term is rejected — never invented), it is
    stored in separate, attributed fields, and it NEVER touches the verbatim
    in-project ``definition``. An empty `definition` clears any prior enrichment.

    Returns True if the term exists in this map (and was updated/cleared), else
    False. Open Mind never fetches the text itself — the caller (the search skill)
    supplies it, keeping the app's local-only invariant intact."""
    entries: Dict[str, Any] = doc.get("terms", {}) or {}
    key = _resolve_key(entries, term)
    if key is None:
        return False
    e = entries[key]
    text = (definition or "").strip()
    if not text:                       # empty -> remove any prior enrichment
        for f in _STD_FIELDS:
            e.pop(f, None)
        return True
    stamp = retrieved or time.strftime("%Y-%m-%d", time.localtime())
    e["standard_definition"] = text
    if html and html.strip():
        e["standard_html"] = html.strip()
    else:
        e.pop("standard_html", None)
    e["standard_source"] = "wikipedia"
    if url:
        e["standard_url"] = url
    if title:
        e["standard_title"] = title
    e["standard_retrieved"] = stamp
    e["standard_status"] = "matched"       # attempted, found
    e["standard_attempted_at"] = stamp
    return True


def mark_no_match(doc: Dict[str, Any], term: str, retrieved: Optional[str] = None) -> bool:
    """Record that a term WAS looked up but found no confident standard definition
    (status 'none'). This is the completeness marker for the no-match case — it
    stops the recovery loop from retrying a term endlessly while keeping the term's
    verbatim local definition. Returns False if the term is unknown."""
    entries: Dict[str, Any] = doc.get("terms", {}) or {}
    key = _resolve_key(entries, term)
    if key is None:
        return False
    e = entries[key]
    for f in ("standard_definition", "standard_html", "standard_source",
              "standard_url", "standard_title"):
        e.pop(f, None)
    e["standard_status"] = "none"
    e["standard_attempted_at"] = retrieved or time.strftime("%Y-%m-%d", time.localtime())
    return True


def unattempted_terms(doc: Dict[str, Any]) -> List[str]:
    """Terms that have NEVER been enrichment-attempted (no status marker and no
    legacy standard_definition). This is the work-list the auto-enrichment job and
    its crash-recovery reconciler use; it is empty exactly when enrichment is
    complete for the current term set."""
    out: List[str] = []
    for t, e in (doc.get("terms") or {}).items():
        if e.get("standard_status") or e.get("standard_definition"):
            continue
        out.append(t)
    return out


_ASK_TERM = re.compile(
    r"^\s*(?:what(?:\s+is|\s+are|'s|\s+does)?|define|expand|meaning\s+of|"
    r"what\s+do(?:es)?)\s+(?:the\s+)?(?:acronym|term|abbreviation)?\s*"
    r"[\"']?([A-Za-z][\w.&/+-]*)[\"']?\s*(?:mean|stand\s+for)?\s*\??\s*$",
    re.IGNORECASE)


def looks_like_term_query(question: str) -> Optional[Tuple[str, bool]]:
    """Classify a question for glossary-first routing.

    Returns (token, explicit) or None:
      * explicit=True  -> a definition question ("what does ISR mean?", "define ISR")
                          — route to the glossary even to report "not found".
      * explicit=False -> a bare single identifier/literal token — route to the
                          glossary only if it actually has an entry (else fall
                          through to normal search, since it may be a code symbol).
    """
    q = (question or "").strip()
    m = _ASK_TERM.match(q)
    if m:
        return (m.group(1), True)
    if tokenmatch.is_exact_token_query(q):
        return (tokenmatch.strip_quotes(q), False)
    return None
