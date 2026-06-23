"""In-app Wikipedia glossary enrichment ENGINE (deterministic, audited egress).

This is the single source of truth the PIPELINE uses to *guarantee* enrichment
runs on every train (see jobs._run_enrich + reconcile_enrichment). It is the same
algorithm the portable `wikipedia-glossary` skill uses, but in-process: it fetches
through :func:`netguard.guarded_external_request` (audited) and writes results +
completeness markers directly into the glossary map.

Honesty contract (unchanged): the verbatim in-project definition is never touched;
the Wikipedia text is stored in a separate, attributed field; no new terms are
invented (only already-extracted terms are looked up); and a term with no confident
match is recorded as `standard_status="none"` so it is not retried forever.

Matching is conservative (a wrong standard definition is worse than none): acronyms
match only by TITLE INITIALS plus a computing-domain prior; the first
high-confidence hit wins; per-project `pins` (term -> exact article) override the
matcher for domain acronyms it cannot disambiguate.

NOTE: section-aware extraction that drops History/Controversy/Outlook (plan part C)
is a follow-up; this version stores the article's full introduction section.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from html import unescape
from typing import Any, Callable, Dict, List, Optional

from . import db, glossary, mapio, netguard

# Wikimedia's robot policy returns 403 to contact-less clients (httpx especially);
# a UA that identifies the tool and references the policy passes. Override via env.
_UA = os.environ.get(
    "OPENMIND_ENRICH_UA",
    "OpenMind-Glossary/1.0 (local code-RAG glossary enrichment; "
    "respects https://meta.wikimedia.org/wiki/User-Agent_policy)")
_PACE = 0.2          # min seconds between HTTP calls — pace bursts under wiki limits
_TIMEOUT = 20.0
_MAXCHARS = 3500     # richer than a thin lead: intro + key sections, format-preserved

# Section titles to DROP (history / appendix / non-definitional) — we keep the
# lead + conceptual sections so the quote stays substantive but not bloated.
_EXCLUDE_SECTIONS = ("history", "background", "etymology", "origin", "controvers",
                     "criticism", "reception", "legacy", "timeline", "see also",
                     "references", "notes", "footnotes", "external link",
                     "further reading", "bibliography", "gallery", "award",
                     "popular culture", "trivia", "release history", "version history")
# Safe prose tag subset to render (Wikipedia limited-HTML is already prose-only:
# no scripts/images/tables/links). Everything else is unwrapped to its text; ALL
# attributes are stripped, so no style/onclick/href survives.
_ALLOWED_TAGS = {"p", "b", "i", "em", "strong", "ul", "ol", "li", "dl", "dt", "dd",
                 "h3", "h4", "h5", "h6", "code", "sub", "sup", "blockquote", "br"}
_TAG_MAP = {"h2": "h3", "samp": "code", "tt": "code", "kbd": "code"}
_DROP_BLOCKS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)[^>]*>")
_H2_SPLIT = re.compile(r"(<h2[^>]*>.*?</h2>)", re.I | re.S)


class Unavailable(Exception):
    """Raised when Wikipedia could not be reached at all for a term — so the term
    is left UN-attempted (retried later) instead of being wrongly marked 'none'
    on a transient network failure / rate-limit / policy block."""

_STOP = {"of", "the", "and", "for", "to", "in", "a", "an", "on", "by", "with",
         "or", "at", "as", "per", "from"}
# Open Mind indexes SOURCE CODE — its glossary terms are technical, so when an
# acronym is ambiguous across domains the computing sense is the intended one.
# Computing-domain prior. Two broadly-ambiguous tokens are deliberately EXCLUDED:
# "security" (appears in PRISON articles — MDC matched a detention center) and a
# bare "data " (too generic). Genuine tech terms still carry "computer", "queue",
# "protocol", "authentication", etc., so recall for real matches is preserved.
_TECH = ("software", "computing", "computer", "programming", "program", "network",
         "protocol", "server", "database", "encryption", "authentication",
         "algorithm", "operating system", "file system", "distributed",
         "hardware", "internet", "application", "message", "kernel", "processor",
         "framework", "replication", "cluster", "storage", "cache", "queue",
         "api", "byte", "kafka", "broker", "interface", "compiler")


# ---------------------------------------------------------------------------
# HTTP (audited via netguard)
# ---------------------------------------------------------------------------
def _get_json(url: str, retries: int = 2) -> Optional[Dict[str, Any]]:
    for attempt in range(retries + 1):
        time.sleep(_PACE)
        try:
            r = netguard.guarded_external_request(
                "GET", url, timeout=_TIMEOUT,
                headers={"User-Agent": _UA, "Accept": "application/json"})
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError("http %d" % r.status_code)
            r.raise_for_status()
            return r.json()
        except netguard.ExfiltrationBlocked:
            return None          # egress disabled -> no enrichment, cleanly
        except Exception:        # noqa: BLE001 - transient; back off and retry
            if attempt >= retries:
                return None
            time.sleep(0.7 * (attempt + 1))
    return None


def wiki_search(query: str, lang: str, limit: int = 5) -> List[str]:
    qs = urllib.parse.urlencode({"action": "query", "list": "search",
                                 "format": "json", "srlimit": limit,
                                 "srsearch": query})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs))
    if d is None:
        return None            # fetch FAILED (network/policy) — distinct from no results
    return [it.get("title") for it in d.get("query", {}).get("search", [])
            if it.get("title")]


def wiki_summary(title: str, lang: str) -> Optional[Dict[str, Any]]:
    url = ("https://%s.wikipedia.org/api/rest_v1/page/summary/%s"
           % (lang, urllib.parse.quote(title, safe="")))
    d = _get_json(url)
    if not d:
        return None
    return {"title": d.get("title") or title,
            "extract": d.get("extract") or "",
            "description": d.get("description") or "",
            "type": d.get("type") or "",
            "url": ((d.get("content_urls") or {}).get("desktop") or {}).get("page") or ""}


def wiki_intro(title: str, lang: str) -> str:
    """The article's full introduction section (plain text)."""
    qs = urllib.parse.urlencode({"action": "query", "prop": "extracts",
                                 "exintro": 1, "explaintext": 1, "redirects": 1,
                                 "format": "json", "titles": title})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs))
    if not d:
        return ""
    for _, p in ((d.get("query") or {}).get("pages") or {}).items():
        if p.get("extract"):
            return p["extract"]
    return ""


def wiki_html(title: str, lang: str) -> Optional[str]:
    """The FULL article as Wikipedia 'limited HTML' (clean prose: <p>/<b>/<ul>/
    <h2>… — no links/images/tables/infoboxes). None if the fetch failed."""
    qs = urllib.parse.urlencode({"action": "query", "prop": "extracts",
                                 "redirects": 1, "format": "json", "titles": title})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs))
    if d is None:
        return None
    for _, p in ((d.get("query") or {}).get("pages") or {}).items():
        return p.get("extract") or ""
    return ""


def _section_filter(html: str, max_chars: int) -> str:
    """Keep the lead + conceptual sections, dropping History/See also/References/
    etc. Splits on top-level <h2> headings (TextExtracts emits one per section)."""
    parts = _H2_SPLIT.split(html or "")
    out = parts[0] if parts else ""          # everything before the first <h2> = lead
    for j in range(1, len(parts) - 1, 2):
        htag, body = parts[j], parts[j + 1]
        title = re.sub(r"<[^>]+>", "", htag).strip().lower()
        if any(x in title for x in _EXCLUDE_SECTIONS):
            continue
        if len(out) >= max_chars:
            break
        out += htag + body
    return out


def _sanitize_html(html: str) -> str:
    """Reduce to the safe prose subset: drop script/style, rewrite a few tags
    (h2→h3, samp→code), unwrap any other tag to its text, and STRIP ALL
    attributes (so no style/onclick/href can survive)."""
    html = _DROP_BLOCKS.sub("", html or "")

    def _rep(m):
        close, name = m.group(1), m.group(2).lower()
        name = _TAG_MAP.get(name, name)
        if name in _ALLOWED_TAGS:
            return ("</%s>" % name) if close else ("<%s>" % name)
        return ""

    html = _TAG_RE.sub(_rep, html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html).strip()
    return html


def _trim_html(html: str, max_chars: int) -> str:
    """Trim overflow at a CLOSED block boundary so we never cut mid-tag."""
    if len(html) <= max_chars:
        return html
    cut = html[:max_chars]
    best = max(cut.rfind(t) + len(t) for t in
               ("</p>", "</li>", "</ul>", "</ol>", "</h3>", "</h4>", "</dd>", "</blockquote>"))
    return cut[:best] if best > 0 else cut


def _html_to_text(html: str) -> str:
    """A plain-text rendering of the kept HTML (for the stored text field / search)."""
    t = re.sub(r"<li[^>]*>", "\n• ", html or "")
    t = re.sub(r"</(p|h3|h4|h5|h6|li|dd|dt|blockquote)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract_content(title: str, lang: str, max_chars: int) -> Optional[Any]:
    """The substantive, FORMAT-PRESERVED quote for an article: lead + conceptual
    sections, sanitized HTML + a plain-text rendering. Returns (html, text), or
    None if the fetch failed (so the caller can leave the term un-attempted)."""
    raw = wiki_html(title, lang)
    if raw is None:
        return None
    if not raw:
        return ("", "")
    html = _trim_html(_sanitize_html(_section_filter(raw, max_chars)), max_chars)
    return (html, _html_to_text(html))


# ---------------------------------------------------------------------------
# Tidy + matching (mirrors the portable skill script)
# ---------------------------------------------------------------------------
def _sentence_trim(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    i = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:i + 1] if i > 60 else cut).strip()


def tidy(text: str, max_chars: int = _MAXCHARS) -> str:
    s = re.sub(r"\s*\((?:[^()]*?(?:/[^()]*?/|listen|pronounced|ⓘ)[^()]*?)\)", "",
               text or "")
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", s)]
    paras = [p for p in paras if p]
    out = ""
    for p in paras:
        cand = (out + "\n\n" + p) if out else p
        if out and len(cand) > max_chars:
            break
        out = cand
    return _sentence_trim((out or s).strip(), max_chars)


def _is_acronym(term: str) -> bool:
    t = (term or "").strip().rstrip("s")
    return 2 <= len(t) <= 7 and t.isupper() and t.isalpha()


def _sig_words(s: str) -> List[str]:
    return [w for w in re.split(r"[\s/_()-]+", s or "") if w and w[0].isalpha()]


def initials_match(acronym: str, title: str) -> bool:
    ac = (acronym or "").upper().rstrip("S")
    if len(ac) < 2:
        return False
    words = _sig_words(title)
    if len(words) < 2:
        return False
    full = "".join(w[0].upper() for w in words)
    sig = "".join(w[0].upper() for w in words if w.lower() not in _STOP)
    return any(initials == ac or initials.startswith(ac) for initials in (full, sig))


def _tech_boost(*texts: str) -> int:
    hay = " ".join(t.lower() for t in texts if t)
    return 2 if any(w in hay for w in _TECH) else 0


def relevant(term: str, summ: Dict[str, Any]) -> bool:
    hay = ((summ.get("title") or "") + " " + (summ.get("extract") or "")).lower()
    tl = (term or "").lower()
    if _is_acronym(term):
        return re.search(r"\b" + re.escape(tl) + r"\b", hay) is not None
    if tl and tl in hay:
        return True
    toks = [w for w in re.split(r"[^a-z0-9]+", tl) if len(w) >= 4]
    if not toks:
        return re.search(r"\b" + re.escape(tl) + r"\b", hay) is not None
    return sum(1 for w in toks if w in hay) >= max(1, (len(toks) + 1) // 2)


def lookup(term: str, context: str, lang: str = "en", *, local_def: str = "",
           max_chars: int = _MAXCHARS, strict: bool = True,
           min_chars: int = 20) -> Optional[Dict[str, Any]]:
    acro = _is_acronym(term)
    # The in-project definition of an acronym is its EXPANSION (PBE ->
    # "Password-Based Encryption"). A correct article shares those words; a
    # homonym ("Programming by example") shares none — so the local definition is
    # the strongest disambiguator we have. Require a candidate to overlap it.
    local_sig = [w for w in re.split(r"[^a-z0-9]+", (local_def or "").lower())
                 if len(w) >= 4 and w not in _STOP]
    need = (len(local_sig) + 1) // 2
    queries = [term]
    if context and not acro:
        queries.append((term + " " + context).strip())
    titles: List[str] = []
    fetched = False
    for q in queries:
        res = wiki_search(q, lang)
        if res is None:
            continue              # this query failed to fetch — try the next
        fetched = True
        for t in res:
            if t and t not in titles:
                titles.append(t)
    if not fetched:
        # could not reach Wikipedia at all -> do NOT report 'no match' (which would
        # wrongly mark the term attempted); signal unavailability so it is retried.
        raise Unavailable("could not reach Wikipedia for %r" % term)
    ctx_tokens = [w for w in re.split(r"[^a-z0-9]+", (context or "").lower())
                  if len(w) >= 4]
    best, best_score = None, -1
    for t in titles[:6]:
        s = wiki_summary(t, lang)
        if not s or s["type"] == "disambiguation" or not s["extract"]:
            continue
        title_l = s["title"].lower()
        hay = title_l + " " + s["description"].lower() + " " + s["extract"].lower()
        strong = initials_match(term, s["title"]) if acro else (term.lower() in title_l)
        if strict:
            if acro and not strong:
                continue
            if not acro and not strong and not relevant(term, s):
                continue
        ctx_overlap = sum(1 for w in ctx_tokens if w in hay)
        tech = _tech_boost(s["description"], s["extract"])
        def_overlap = sum(1 for w in local_sig if w in hay)
        if strict and acro:
            if local_sig:
                if def_overlap < need:
                    continue               # article inconsistent with in-project meaning
            elif tech == 0 and ctx_overlap == 0:
                continue                   # no local signal -> fall back to domain prior
        score = (3 if strong else 0) + ctx_overlap + tech + def_overlap
        if score > best_score:
            best_score, best = score, s
        if score >= 5:
            break
    if not best:
        return None
    content = extract_content(best["title"], lang, max_chars)
    if content is None:
        raise Unavailable("could not fetch content for %r" % term)
    html, text = content
    if len(text) < min_chars:                  # thin/failed -> fall back to the lead
        text = tidy(best.get("extract", ""), max_chars=max_chars)
        html = ""
    if len(text) < min_chars:
        return None
    return {"term": term, "definition": text, "definition_html": html,
            "url": best["url"], "title": best["title"]}


def pinned_lookup(term: str, title: str, lang: str = "en",
                  max_chars: int = _MAXCHARS) -> Optional[Dict[str, Any]]:
    """Fetch an explicitly pinned article (caller asserts it is correct)."""
    s = wiki_summary(title, lang)
    if not s or not s.get("extract"):
        return None
    content = extract_content(s["title"], lang, max_chars)
    if content is None:
        raise Unavailable("could not fetch content for %r" % term)
    html, text = content
    if not text:
        text = tidy(s.get("extract", ""), max_chars=max_chars)
        html = ""
    if not text:
        return None
    return {"term": term, "definition": text, "definition_html": html,
            "url": s["url"], "title": s["title"]}


# ---------------------------------------------------------------------------
# Project enrichment (the pipeline entry point)
# ---------------------------------------------------------------------------
def infer_context(project: Optional[Dict[str, Any]]) -> str:
    """Auto-infer the disambiguation context for a project (its name). Editable
    via project meta `enrich_context`."""
    if not project:
        return ""
    return (project.get("name") or "").strip()


def enrich_project(project_id: str, *, context: Optional[str] = None,
                   only: Optional[List[str]] = None,
                   pins: Optional[Dict[str, str]] = None,
                   block: Optional[List[str]] = None,
                   skip_enriched: bool = True, lang: str = "en",
                   max_chars: int = _MAXCHARS,
                   cancel: Optional[Callable[[], bool]] = None,
                   on_progress: Optional[Callable[[int, int, str, Optional[str]], None]] = None
                   ) -> Dict[str, Any]:
    """Look up standard definitions for a project's glossary terms and write them
    back with completeness markers, saving incrementally (so a crash/cancel keeps
    progress and the recovery reconciler resumes only the un-attempted terms).

    `skip_enriched` (default) processes only un-attempted terms; `only` restricts
    to a subset; `pins` (term -> article) override the matcher and are always
    processed. Terms that error (e.g. a network blip) are LEFT un-attempted so a
    later run retries them. Returns {matched, none, total, terms}."""
    doc = mapio.load_glossary(project_id)
    terms_map = doc.get("terms") or {}
    if not terms_map:
        return {"matched": 0, "none": 0, "total": 0, "terms": 0}
    pins = {k.lower(): v for k, v in (pins or {}).items() if k and v}
    block = {b.lower() for b in (block or []) if b}
    if only:
        want = {t.lower() for t in only}
        work = [t for t in terms_map if t.lower() in want]
    elif skip_enriched:
        work = glossary.unattempted_terms(doc)
    else:
        work = list(terms_map.keys())
    for t in terms_map:                      # pinned terms always processed
        if t.lower() in pins and t not in work:
            work.append(t)
    if context is None:
        context = infer_context(db.get_project(project_id))

    matched = none = 0
    total = len(work)
    dirty = False
    for i, term in enumerate(work, 1):
        if cancel and cancel():
            break
        tl = term.lower()
        if tl in block and tl not in pins:   # project-internal acronym -> never guess
            glossary.mark_no_match(doc, term)
            none += 1
            dirty = True
            if i % 5 == 0:
                mapio.save_glossary(project_id, doc)
                dirty = False
            if on_progress:
                on_progress(i, total, term, None)
            continue
        try:
            if tl in pins:
                hit = pinned_lookup(term, pins[tl], lang, max_chars)
            else:
                hit = lookup(term, context or "", lang, max_chars=max_chars,
                             local_def=(terms_map.get(term) or {}).get("definition", ""))
        except Exception:                    # noqa: BLE001 - leave un-attempted; retry later
            if on_progress:
                on_progress(i, total, term, None)
            continue
        if hit:
            glossary.set_standard_definition(doc, term, definition=hit["definition"],
                                             html=hit.get("definition_html"),
                                             url=hit.get("url"), title=hit.get("title"))
            matched += 1
            result = hit.get("title")
        else:
            glossary.mark_no_match(doc, term)
            none += 1
            result = None
        dirty = True
        if i % 5 == 0:
            mapio.save_glossary(project_id, doc)
            dirty = False
        if on_progress:
            on_progress(i, total, term, result)
    if dirty:
        mapio.save_glossary(project_id, doc)
    return {"matched": matched, "none": none, "total": total, "terms": len(terms_map)}
