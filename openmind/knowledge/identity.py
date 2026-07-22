"""Deterministic identity for canonical graph objects.

Canonical keys, alias normalization and normalized statement hashes all live
here so the promotion path, the deterministic projector and manual creation
can never derive the same concept two different ways.

Everything is pure string work — no database, no provider, no side effects.
"""
from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")
#: Characters stripped from derived key material (kept deliberately small:
#: the goal is stability, not aggressive slugging that collides distinct
#: names).
_KEY_UNSAFE = re.compile(r"[^a-z0-9._#:/()\[\]{}@$+-]+")


def normalize_alias(alias: str) -> str:
    """Case-folded, whitespace-collapsed form used for exact alias lookup
    and collision detection."""
    return _WHITESPACE.sub(" ", str(alias or "").strip()).casefold()


def normalize_statement(statement: str) -> str:
    """Whitespace-collapsed, case-folded statement text — the dedup basis."""
    return _WHITESPACE.sub(" ", str(statement or "").strip()).casefold()


def statement_hash(statement: str) -> str:
    """SHA-256 of the normalized statement. Identical normalized claims on
    one Entity deduplicate through this."""
    return hashlib.sha256(
        normalize_statement(statement).encode("utf-8")).hexdigest()


def quote_hash(quote: str) -> str:
    """Hash of a normalized evidence quote (join-table identity component,
    matching the Phase 4 candidate-evidence convention)."""
    return hashlib.sha256(
        _WHITESPACE.sub(" ", str(quote or "").strip()).encode("utf-8")
    ).hexdigest()


def _key_component(text: str) -> str:
    """One canonical-key component: trimmed, lower-cased except that
    identifier-looking tokens keep their case (REQ-NC-017 must stay
    readable), unsafe characters collapsed to '-'. Deterministic."""
    clean = _WHITESPACE.sub(" ", str(text or "").strip())
    # Preserve case for explicit identifiers (contains a digit or is
    # dominated by upper-case); otherwise lower-case for stability.
    has_digit = any(c.isdigit() for c in clean)
    upper = sum(1 for c in clean if c.isupper())
    lower = sum(1 for c in clean if c.islower())
    if not has_digit and upper <= lower:
        clean = clean.lower()
    return _KEY_UNSAFE.sub("-", clean.replace(" ", "-")).strip("-")


def entity_key(entity_type: str, *components: str) -> str:
    """``<entity_type>:<component>[:<component>...]`` — the deterministic
    canonical key. Components are normalized individually; empty components
    are dropped."""
    parts = [str(entity_type or "").strip().lower()]
    parts.extend(c for c in (_key_component(c) for c in components) if c)
    return ":".join(parts)


def identifier_entity_key(entity_type: str, identifier: str) -> str:
    """Key for a concept with an explicit stable identifier
    (``requirement:REQ-NC-017``). The identifier's case is preserved."""
    ident = _WHITESPACE.sub("", str(identifier or "").strip())
    return f"{str(entity_type or '').strip().lower()}:{ident}"


def derived_entity_key(entity_type: str, title: str) -> str:
    """Key for a concept WITHOUT a stable identifier: a bounded, normalized
    projection of its title, marked ``derived`` so it can never collide with
    an explicit identifier key."""
    slug = _key_component(title).lower()[:120] or "untitled"
    return f"{str(entity_type or '').strip().lower()}:derived:{slug}"


def asset_entity_key(entity_type: str, asset_id: str) -> str:
    """``code-component:asset:a_123`` — Asset-anchored deterministic keys."""
    return f"{str(entity_type or '').strip().lower()}:asset:{asset_id}"


def symbol_entity_key(asset_id: str, symbol: str) -> str:
    """``code-symbol:asset:a_123:pkg.Class#method(Sig)`` — symbol case is
    load-bearing and preserved verbatim (whitespace removed)."""
    sym = _WHITESPACE.sub("", str(symbol or "").strip())
    return f"code-symbol:asset:{asset_id}:{sym}"


def interface_entity_key(method: str, path: str) -> str:
    """``interface:POST:/name-check`` — HTTP method upper-cased, path kept
    verbatim (paths are case-sensitive)."""
    m = str(method or "").strip().upper()
    p = _WHITESPACE.sub("", str(path or "").strip())
    return f"interface:{m}:{p}"


def configuration_entity_key(asset_id: str, config_key: str) -> str:
    key = _WHITESPACE.sub("", str(config_key or "").strip())
    return f"configuration:asset:{asset_id}:{key}"


def database_object_entity_key(schema_hint: str, object_name: str) -> str:
    """``database-object:database-schema:PASSENGER`` — SQL object names keep
    their case (quoted identifiers are case-sensitive)."""
    hint = _key_component(schema_hint) or "database-schema"
    name = _WHITESPACE.sub("", str(object_name or "").strip())
    return f"database-object:{hint}:{name}"


def message_topic_entity_key(topic: str) -> str:
    return f"message-topic:{_WHITESPACE.sub('', str(topic or '').strip())}"


#: An identifier-looking stable key: letters+digits with -_./ separators and
#: at least one digit or one separator (REQ-NC-017, ORD.4, NC_CHECK-2) —
#: NOT a plain English word. Used when deciding whether a candidate's
#: stable_key is an explicit identifier or just a title echo.
_IDENTIFIER_RE = re.compile(r"^(?=.*[0-9._/-])[A-Za-z0-9._/-]{2,80}$")


def looks_like_identifier(value: str) -> bool:
    v = str(value or "").strip()
    return bool(v) and not v.count(" ") and bool(_IDENTIFIER_RE.match(v))


__all__ = [
    "normalize_alias", "normalize_statement", "statement_hash", "quote_hash",
    "entity_key", "identifier_entity_key", "derived_entity_key",
    "asset_entity_key", "symbol_entity_key", "interface_entity_key",
    "configuration_entity_key", "database_object_entity_key",
    "message_topic_entity_key", "looks_like_identifier",
]
