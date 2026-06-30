#!/usr/bin/env python3
"""Enrich an Open Mind project glossary with standard definitions from Wikipedia.

This script is the NETWORK ACTOR of the `wikipedia-glossary` skill. The Open Mind
application itself never makes web requests (its local-only invariant); this helper,
run explicitly by an agent/user, is the only thing that contacts Wikipedia. It:

  1. reads the already-extracted glossary terms from a running Open Mind server
     (GET /glossary?scope=<id>),
  2. looks up each term's authoritative definition on Wikipedia (REST summary +
     search APIs), tidies the lead paragraph, and keeps it ONLY when the match is
     confident (conservative — a wrong "standard definition" would be worse than
     none, so unmatched terms simply keep their verbatim in-project definition),
  3. writes the confident matches back (POST /glossary/enrich), where they are
     stored as a SEPARATE, ATTRIBUTED field — the verbatim in-project definition is
     never overwritten and no new terms are invented.

Standard library only (urllib/json/argparse) so it runs under any agent runtime
without extra installs. Output is ASCII-only for cross-platform console safety.

Examples
--------
  # list projects to find the scope id
  python enrich_glossary.py --list-projects

  # dry run (no writeback) over a Kafka project, with a domain hint for disambiguation
  python enrich_glossary.py --scope <pid> --context "Apache Kafka distributed systems" --dry-run

  # enrich for real, only a few terms
  python enrich_glossary.py --scope <pid> --context "Apache Kafka" --only ISR,SASL,ACL
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

UA = ("OpenMind-Glossary/1.0 (local code-RAG glossary enrichment; "
      "respects https://meta.wikimedia.org/wiki/User-Agent_policy)")
_PACE = 0.2    # min seconds between HTTP calls — paces bursts under Wikipedia limits


# ---------------------------------------------------------------------------
# HTTP (stdlib)
# ---------------------------------------------------------------------------
def _get_json(url, ua, timeout, retries=3):
    time.sleep(_PACE)
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt >= retries:
                return None        # 404 = no such article; don't retry
            time.sleep(0.7 * (attempt + 1))   # transient (e.g. 429) — back off
        except (urllib.error.URLError, ValueError, OSError):
            if attempt >= retries:
                return None
            time.sleep(0.7 * (attempt + 1))
    return None


def _post_json(url, body, ua, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": ua,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Open Mind (local server) calls
# ---------------------------------------------------------------------------
def list_projects(base, ua, timeout):
    d = _get_json(base.rstrip("/") + "/projects", ua, timeout) or {}
    return d.get("projects", [])


def glossary_list(base, scope, ua, timeout):
    """The scope's glossary: {terms:{term:def}, count, wiki:[already-enriched]}."""
    url = base.rstrip("/") + "/glossary?scope=" + urllib.parse.quote(scope, safe="")
    return _get_json(url, ua, timeout)


def post_enrich(base, scope, entries, ua, timeout):
    return _post_json(base.rstrip("/") + "/glossary/enrich",
                      {"scope": scope, "entries": entries}, ua, timeout)


# ---------------------------------------------------------------------------
# Wikipedia lookup
# ---------------------------------------------------------------------------
def wiki_search(query, lang, ua, timeout, limit=5):
    qs = urllib.parse.urlencode({"action": "query", "list": "search",
                                 "format": "json", "srlimit": limit,
                                 "srsearch": query})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs), ua, timeout)
    if not d:
        return []
    return [it.get("title") for it in d.get("query", {}).get("search", [])
            if it.get("title")]


def wiki_summary(title, lang, ua, timeout):
    url = ("https://%s.wikipedia.org/api/rest_v1/page/summary/%s"
           % (lang, urllib.parse.quote(title, safe="")))
    d = _get_json(url, ua, timeout)
    if not d:
        return None
    return {"title": d.get("title") or title,
            "extract": d.get("extract") or "",
            "description": d.get("description") or "",
            "type": d.get("type") or "",
            "url": ((d.get("content_urls") or {}).get("desktop") or {}).get("page") or ""}


def wiki_intro(title, lang, ua, timeout):
    """The article's FULL introduction section (every lead paragraph before the
    first heading), plain text — this is the 'detailed introduction'. The REST
    summary used for candidate selection is only the short lead sentence; once a
    winner is chosen we fetch its full intro via the extracts API."""
    qs = urllib.parse.urlencode({"action": "query", "prop": "extracts",
                                 "exintro": 1, "explaintext": 1, "redirects": 1,
                                 "format": "json", "titles": title})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs), ua, timeout)
    if not d:
        return ""
    for _, p in ((d.get("query") or {}).get("pages") or {}).items():
        if p.get("extract"):
            return p["extract"]
    return ""


# ---------------------------------------------------------------------------
# Format-preserving content: full article (limited HTML) -> lead + key sections
# (drops History/See also/References/etc.), sanitized to a safe prose tag subset.
# ---------------------------------------------------------------------------
_EXCLUDE_SECTIONS = ("history", "background", "etymology", "origin", "controvers",
                     "criticism", "reception", "legacy", "timeline", "see also",
                     "references", "notes", "footnotes", "external link",
                     "further reading", "bibliography", "gallery", "award",
                     "popular culture", "trivia", "release history", "version history")
_ALLOWED_TAGS = {"p", "b", "i", "em", "strong", "ul", "ol", "li", "dl", "dt", "dd",
                 "h3", "h4", "h5", "h6", "code", "sub", "sup", "blockquote", "br"}
_TAG_MAP = {"h2": "h3", "samp": "code", "tt": "code", "kbd": "code"}
_DROP_BLOCKS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)[^>]*>")
_H2_SPLIT = re.compile(r"(<h2[^>]*>.*?</h2>)", re.I | re.S)


def wiki_html(title, lang, ua, timeout):
    qs = urllib.parse.urlencode({"action": "query", "prop": "extracts",
                                 "redirects": 1, "format": "json", "titles": title})
    d = _get_json("https://%s.wikipedia.org/w/api.php?%s" % (lang, qs), ua, timeout)
    if d is None:
        return None
    for _, p in ((d.get("query") or {}).get("pages") or {}).items():
        return p.get("extract") or ""
    return ""


def _section_filter(html, max_chars):
    parts = _H2_SPLIT.split(html or "")
    out = parts[0] if parts else ""
    for j in range(1, len(parts) - 1, 2):
        htag, body = parts[j], parts[j + 1]
        title = re.sub(r"<[^>]+>", "", htag).strip().lower()
        if any(x in title for x in _EXCLUDE_SECTIONS):
            continue
        if len(out) >= max_chars:
            break
        out += htag + body
    return out


def _sanitize_html(html):
    html = _DROP_BLOCKS.sub("", html or "")

    def _rep(m):
        close, name = m.group(1), m.group(2).lower()
        name = _TAG_MAP.get(name, name)
        if name in _ALLOWED_TAGS:
            return ("</%s>" % name) if close else ("<%s>" % name)
        return ""

    html = _TAG_RE.sub(_rep, html)
    html = re.sub(r"[ \t]+", " ", html)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", html).strip()


def _trim_html(html, max_chars):
    if len(html) <= max_chars:
        return html
    cut = html[:max_chars]
    best = max(cut.rfind(t) + len(t) for t in
               ("</p>", "</li>", "</ul>", "</ol>", "</h3>", "</h4>", "</dd>", "</blockquote>"))
    return cut[:best] if best > 0 else cut


def _html_to_text(html):
    t = re.sub(r"<li[^>]*>", "\n* ", html or "")
    t = re.sub(r"</(p|h3|h4|h5|h6|li|dd|dt|blockquote)>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract_content(title, lang, ua, timeout, max_chars):
    """Lead + conceptual sections as sanitized HTML + plain text. Returns
    (html, text), or None if the fetch failed."""
    raw = wiki_html(title, lang, ua, timeout)
    if raw is None:
        return None
    if not raw:
        return ("", "")
    html = _trim_html(_sanitize_html(_section_filter(raw, max_chars)), max_chars)
    return (html, _html_to_text(html))


def _sentence_trim(s, max_chars):
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    i = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:i + 1] if i > 60 else cut).strip()


def tidy(text, max_chars=2000):
    """Clean an article intro into a stored definition: drop IPA/pronunciation
    parentheticals, normalise whitespace WITHIN each paragraph but KEEP paragraph
    breaks (rendered as blank lines), and keep whole paragraphs up to ~max_chars
    (sentence-trimmed if one paragraph overflows). Deterministic — the article's
    own introduction, never rewritten."""
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


_STOP = {"of", "the", "and", "for", "to", "in", "a", "an", "on", "by", "with",
         "or", "at", "as", "per", "from"}

# Open Mind indexes SOURCE CODE — its glossary terms are technical, so when an
# acronym is ambiguous across domains (ACL = access-control list vs anterior
# cruciate ligament) the computing sense is the intended one. This is an explicit,
# documented domain prior, not a guess: a candidate whose text reads as computing
# gets a boost so it beats an unrelated same-initials article.
_TECH = ("software", "computing", "computer", "programming", "program", "network",
         "protocol", "server", "database", "encryption", "authentication",
         "security", "algorithm", "operating system", "file system", "distributed",
         "hardware", "internet", "application", "message", "kernel", "processor",
         "framework", "replication", "cluster", "storage", "cache", "queue",
         "api", "byte", "data ", "kafka", "broker", "interface", "compiler")


def _tech_boost(*texts):
    hay = " ".join(t.lower() for t in texts if t)
    return 2 if any(w in hay for w in _TECH) else 0


def _is_acronym(term):
    t = (term or "").strip().rstrip("s")
    return 2 <= len(t) <= 7 and t.isupper() and t.isalpha()


def _sig_words(s):
    return [w for w in re.split(r"[\s/_()-]+", s or "") if w and w[0].isalpha()]


def initials_match(acronym, title):
    """True when an article TITLE expands the acronym, e.g.
    'Simple Authentication and Security Layer' -> SASL, 'Access-control list'
    -> ACL. Checks both the full and the stop-word-dropped initials. This is the
    strongest acronym signal — far more precise than 'the letters appear in text'."""
    ac = (acronym or "").upper().rstrip("S")
    if len(ac) < 2:
        return False
    words = _sig_words(title)
    if len(words) < 2:
        return False
    full = "".join(w[0].upper() for w in words)
    sig = "".join(w[0].upper() for w in words if w.lower() not in _STOP)
    return any(initials == ac or initials.startswith(ac) for initials in (full, sig))


def relevant(term, summ):
    """Floor gate for PHRASES (and a weak gate for acronyms): the article must
    plausibly concern the term — the whole phrase, or a majority of its
    significant words, appears in the title/lead."""
    hay = ((summ.get("title") or "") + " " + (summ.get("extract") or "")).lower()
    tl = (term or "").lower()
    if _is_acronym(term):
        return re.search(r"\b" + re.escape(tl) + r"\b", hay) is not None
    if tl and tl in hay:
        return True
    toks = [w for w in re.split(r"[^a-z0-9]+", tl) if len(w) >= 4]
    if not toks:
        return re.search(r"\b" + re.escape(tl) + r"\b", hay) is not None
    hit = sum(1 for w in toks if w in hay)
    return hit >= max(1, (len(toks) + 1) // 2)


def lookup(term, context, lang, ua, timeout, strict=True, min_chars=20, max_chars=2000):
    """Pick the best Wikipedia article for `term`. Candidates come from BOTH a
    bare search (canonical — heavy context skews acronym search badly) and a
    context-hinted search; each is scored by a STRONG signal (the title expands
    the acronym, or contains the term) plus how many context words it shares.
    For acronyms only a STRONG match is accepted — 'the letters appear somewhere'
    is too weak. The highest score wins; ties keep canonical (earlier) order."""
    acro = _is_acronym(term)
    # Bare search gets the canonical article; a context-hinted search helps only
    # for PHRASES (for acronyms it tends to surface junk, and context is already
    # used in scoring below), so add it only then.
    queries = [term]
    if context and not acro:
        queries.append((term + " " + context).strip())
    titles = []
    for q in queries:
        for t in wiki_search(q, lang, ua, timeout):
            if t and t not in titles:
                titles.append(t)
    ctx_tokens = [w for w in re.split(r"[^a-z0-9]+", (context or "").lower())
                  if len(w) >= 4]
    best, best_score = None, -1
    for t in titles[:6]:
        s = wiki_summary(t, lang, ua, timeout)
        if not s or s["type"] == "disambiguation" or not s["extract"]:
            continue
        title_l = s["title"].lower()
        hay = title_l + " " + s["description"].lower() + " " + s["extract"].lower()
        # Acronyms must match by TITLE INITIALS (CA -> 'Certificate authority'),
        # never by substring — 'ca' is a substring of '.ca', 'California', etc.
        strong = initials_match(term, s["title"]) if acro else (term.lower() in title_l)
        if strict:
            if acro and not strong:
                continue                       # acronym needs a strong signal
            if not acro and not strong and not relevant(term, s):
                continue
        ctx_overlap = sum(1 for w in ctx_tokens if w in hay)
        tech = _tech_boost(s["description"], s["extract"])
        # A strong acronym match that is NEITHER computing-related NOR relevant to
        # the user's context is almost certainly the wrong expansion (e.g. ISR ->
        # 'Inuvialuit Settlement Region'). Reject it — keep the local definition
        # rather than attach a confidently-wrong one.
        if strict and acro and tech == 0 and ctx_overlap == 0:
            continue
        # strong title signal + how well it fits the user's context + the
        # computing-domain prior. First occurrence of the max wins, so a genuine
        # tie defers to Wikipedia's own search ranking (canonical/primary topic).
        score = (3 if strong else 0) + ctx_overlap + tech
        if score > best_score:
            best_score, best = score, s
        if score >= 5:        # strong title + computing/context fit — accept now
            break             # (early-exit also keeps HTTP calls low / polite)
    if not best:
        return None
    # Fetch the lead + key sections of the winner as format-preserved HTML (the
    # candidate loop only used the short summary extract).
    content = extract_content(best["title"], lang, ua, timeout, max_chars)
    if content is None:
        return None
    html, text = content
    if len(text) < min_chars:                  # thin/failed -> fall back to the lead
        text = tidy(best.get("extract", ""), max_chars=max_chars)
        html = ""
    if len(text) < min_chars:
        return None
    return {"term": term, "definition": text, "html": html,
            "url": best["url"], "title": best["title"]}


def pinned_lookup(term, title, lang, ua, timeout, max_chars=2000):
    """Fetch an EXPLICIT article the caller pinned for this term (via --map),
    bypassing auto-matching/scoring — the caller asserts the article is correct
    (the honest way to enrich a domain acronym the conservative matcher can't
    disambiguate, e.g. EC -> 'Elliptic-curve cryptography'). Returns None if the
    title doesn't resolve to a real article."""
    s = wiki_summary(title, lang, ua, timeout)
    if not s or not s.get("extract"):
        return None
    content = extract_content(s["title"], lang, ua, timeout, max_chars)
    if content is None:
        return None
    html, text = content
    if not text:
        text = tidy(s.get("extract", ""), max_chars=max_chars)
        html = ""
    if not text:
        return None
    return {"term": term, "definition": text, "html": html,
            "url": s["url"], "title": s["title"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Enrich an Open Mind glossary with "
                                             "Wikipedia standard definitions.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8077",
                    help="Open Mind server (default http://127.0.0.1:8077)")
    ap.add_argument("--scope", help="project id (scope) whose glossary to enrich")
    ap.add_argument("--context", default="",
                    help="domain hint added to each search for disambiguation, "
                         "e.g. 'Apache Kafka distributed systems'")
    ap.add_argument("--lang", default="en", help="Wikipedia language (default en)")
    ap.add_argument("--only", default="",
                    help="comma-separated subset of terms to process")
    ap.add_argument("--skip-enriched", action="store_true",
                    help="skip terms that already have a standard definition — "
                         "the incremental top-up to run after a re-ingest")
    ap.add_argument("--map", default="", dest="pin_map",
                    help="pin terms to exact Wikipedia article titles, e.g. "
                         "'CA=Certificate authority;EC=Elliptic-curve cryptography'. "
                         "Bypasses auto-matching for those terms (you assert the "
                         "article is correct); they are always processed.")
    ap.add_argument("--limit", type=int, default=0,
                    help="max terms to process (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="seconds between Wikipedia lookups (politeness)")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-request timeout")
    ap.add_argument("--min-chars", type=int, default=20,
                    help="reject definitions shorter than this")
    ap.add_argument("--max-chars", type=int, default=3500,
                    help="cap the stored quote length (lead + key sections, kept at "
                         "whole-paragraph/section boundaries; default 3500)")
    ap.add_argument("--no-strict", action="store_true",
                    help="disable the conservative confidence gate (NOT recommended)")
    ap.add_argument("--dry-run", action="store_true",
                    help="look up and print, but do not write back")
    ap.add_argument("--list-projects", action="store_true",
                    help="print project id/name pairs and exit")
    ap.add_argument("--user-agent", default=UA)
    args = ap.parse_args(argv)
    base, ua, to = args.base_url, args.user_agent, args.timeout

    if args.list_projects:
        for p in list_projects(base, ua, to):
            print("%s\t%s\t%s" % (p.get("id", "?"), p.get("state", ""),
                                  p.get("name", "")))
        return 0

    if not args.scope:
        ap.error("--scope is required (use --list-projects to find it)")

    data = glossary_list(base, args.scope, ua, to)
    if data is None:
        print("ERROR: could not read glossary from %s (is Open Mind running?)" % base)
        return 2
    all_terms = list((data.get("terms") or {}).keys())
    terms = list(all_terms)
    enriched = set(data.get("wiki") or [])
    # parse --map pins (term -> explicit article title), case-insensitive on term
    pin = {}
    for pair in args.pin_map.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() and v.strip():
                pin[k.strip().lower()] = v.strip()
    if args.only:
        want = {t.strip().lower() for t in args.only.split(",") if t.strip()}
        terms = [t for t in terms if t.lower() in want]
    if args.skip_enriched:
        dropped = [t for t in terms if t in enriched]
        terms = [t for t in terms if t not in enriched]
        if dropped:
            print("Skipping %d already-enriched term(s): %s"
                  % (len(dropped), ", ".join(sorted(dropped))))
    if args.limit > 0:
        terms = terms[:args.limit]
    # pinned terms are always processed (they bypass filters) — union them in
    for t in all_terms:
        if t.lower() in pin and t not in terms:
            terms.append(t)
    if not terms:
        print("No terms to process for scope %s." % args.scope)
        return 0

    print("Looking up %d term(s) on %s.wikipedia.org%s ..."
          % (len(terms), args.lang, " [dry run]" if args.dry_run else ""))
    entries, matched, skipped = [], [], []
    for i, term in enumerate(terms, 1):
        if i > 1 and args.sleep > 0:
            time.sleep(args.sleep)
        try:
            if term.lower() in pin:
                hit = pinned_lookup(term, pin[term.lower()], args.lang, ua, to,
                                    max_chars=args.max_chars)
            else:
                hit = lookup(term, args.context, args.lang, ua, to,
                             strict=not args.no_strict, min_chars=args.min_chars,
                             max_chars=args.max_chars)
        except Exception as exc:  # noqa: BLE001 - never let one term abort the run
            hit = None
            print("  [%d/%d] %-22s ERROR %s" % (i, len(terms), term, exc))
            continue
        if hit:
            matched.append(term)
            entries.append(hit)
            print("  [%d/%d] %-22s OK   %s" % (i, len(terms), term, hit["title"]))
        else:
            skipped.append(term)
            print("  [%d/%d] %-22s --   no confident match (keeps local definition)"
                  % (i, len(terms), term))

    print("\nMatched %d, skipped %d." % (len(matched), len(skipped)))
    if args.dry_run:
        print("Dry run: nothing written. Re-run without --dry-run to apply.")
        return 0
    if not entries:
        print("Nothing to write.")
        return 0
    try:
        res = post_enrich(base, args.scope, entries, ua, to)
    except Exception as exc:  # noqa: BLE001
        print("ERROR writing back: %s" % exc)
        return 2
    print("Wrote back: updated=%d missing=%d projects=%s"
          % (len(res.get("updated", [])), len(res.get("missing", [])),
             res.get("saved_projects")))
    if res.get("missing"):
        print("  (missing terms were not in the glossary and were not invented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
