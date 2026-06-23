"""General token-boundary matching for precise identifier/literal retrieval.

The lexical retrieval leg must match a query term ONLY as a COMPLETE token
(identifier or dotted/dashed literal), or — when explicitly enabled — as a
COMPLETE camelCase/snake_case subword COMPONENT. It must NEVER match a term as a
substring / prefix / suffix / cross-boundary fragment of a DIFFERENT token.

This is a single, data-driven mechanism (no hardcoded term lists, no per-case
special handling). Every confusion class — substring (ack vs acked), prefix
(user vs userId), suffix (Service vs MyService), numeric/version (service1 vs
service10, topicV2 vs topicV20), case (ISR vs isr), camel/snake components,
near-identical short tokens (ISR vs ISO), and exact literals (offsets vs offset)
— is handled by the same boundary rules below.
"""
from __future__ import annotations

import re
from typing import List, Optional, Set

# A complete identifier token: a maximal run of identifier characters.
_IDENT = re.compile(r"[A-Za-z0-9_]+")
# A complete dotted/dashed literal: identifier runs joined by '.' or '-'
# (e.g. "orders.created.v2", "payment-events", "com.acme.OrderEvent").
_DOTTED = re.compile(r"[A-Za-z0-9_]+(?:[.\-][A-Za-z0-9_]+)+")
# camelCase / acronym / digit splitter for subword components.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
# A query that is a single identifier/literal token (optionally quoted).
_QUERY_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def strip_quotes(s: str) -> str:
    return (s or "").strip().strip('"').strip("'").strip()


def identifier_tokens(text: str) -> Set[str]:
    return set(_IDENT.findall(text or ""))


def dotted_tokens(text: str) -> Set[str]:
    return set(_DOTTED.findall(text or ""))


def subword_components(token: str) -> Set[str]:
    """camelCase + snake_case + digit-boundary components of one identifier.

    getUserName -> {get, User, Name};  user_name -> {user, name};
    getHTTPServer -> {get, HTTP, Server};  service10 -> {service, 10}.
    """
    comps: Set[str] = set()
    for part in token.split("_"):
        if part:
            comps.add(part)
            comps.update(_CAMEL.findall(part))
    return comps


def is_dotted_term(term: str) -> bool:
    return ("." in term) or ("-" in term)


def is_exact_token_query(query: str) -> bool:
    """True when the WHOLE query is a single identifier/ID/enum/literal token
    (no natural-language sentence) — route these to exact token matching."""
    q = strip_quotes(query)
    if not q or " " in q or "\t" in q:
        return False
    return bool(_QUERY_TOKEN.match(q))


def match_kind(text: str, term: str, *, case_sensitive: bool = True,
               subword: bool = False) -> Optional[str]:
    """How `term` occurs in `text`:
      'token'   — a complete identifier or dotted/dashed literal token,
      'subword' — a complete camel/snake component (only if subword=True),
      None      — not present as a whole token (substring/prefix/suffix only).
    """
    term = strip_quotes(term)
    if not term:
        return None
    fold = (lambda s: s) if case_sensitive else (lambda s: s.lower())
    q = fold(term)

    if is_dotted_term(term):
        toks = dotted_tokens(text) | identifier_tokens(text)
        return "token" if q in {fold(t) for t in toks} else None

    idents = identifier_tokens(text)
    if q in {fold(t) for t in idents}:
        return "token"
    if subword:
        for tok in idents:
            if q in {fold(c) for c in subword_components(tok)}:
                return "subword"
    return None


def token_match(text: str, term: str, *, case_sensitive: bool = True,
                subword: bool = False) -> bool:
    return match_kind(text, term, case_sensitive=case_sensitive, subword=subword) is not None


def count_token(text: str, term: str, *, case_sensitive: bool = True) -> int:
    """Count EXACT whole-token occurrences of `term` in `text` (never a
    substring/prefix/suffix of a different token). Mirrors :func:`match_kind`'s
    boundary rule but returns the occurrence COUNT — e.g. an action name appearing
    0 vs 6 times in a log, with no near-token confusion (ISR vs ISO)."""
    term = strip_quotes(term)
    if not term:
        return 0
    fold = (lambda s: s) if case_sensitive else (lambda s: s.lower())
    q = fold(term)
    toks = _DOTTED.findall(text or "") if is_dotted_term(term) else _IDENT.findall(text or "")
    return sum(1 for t in toks if fold(t) == q)


GROUNDING_NOTE = (
    "Each query token is an EXACT identifier/literal. Distinct tokens "
    "(e.g. ack vs acked, user vs userId, service1 vs service10) are different "
    "and must NEVER be conflated; do not treat a substring/prefix/suffix or "
    "embedding-similar form as the queried token."
)
