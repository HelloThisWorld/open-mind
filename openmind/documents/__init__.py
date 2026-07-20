"""Deterministic enterprise-document ingestion (OpenMind v2 Phase 3).

Importing this package imports NO parser dependency: the registry stores module
paths and imports a parser only when a probe matches it, and each parser imports
its third-party library inside ``parse()``. That is what keeps the
dependency-free ``.openmind`` artifact export runnable on a machine with none of
the document packages installed.

No model is involved anywhere in here. Parsing is byte-deterministic, structure
is read from the file's own markup, and nothing is inferred about meaning —
Requirement, Business Rule and Design Decision extraction are Phase 4.

    from openmind.documents import parse_bytes
    result = parse_bytes(data, logical_key="documents/spec.md",
                         filename="spec.md")
    result.status, result.title, len(result.blocks)
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .models import (DOCUMENT_SCHEMA_VERSION, DocumentBlock, DocumentLimits,
                     DocumentMetadata, DocumentParseContext,
                     DocumentParseWarning, DocumentProbe, ParsedDocument,
                     UnsupportedContent)
from .probe import probe_bytes
from .registry import (AmbiguousParser, DocumentParser, ParserEntry,
                       ParserRegistryError, list_parsers, parse, parser_names,
                       select)


def parse_bytes(content: bytes, *, logical_key: str, filename: str = "",
                workspace_id: str = "", declared_media_type: str = "",
                limits: Optional[DocumentLimits] = None,
                parser_options: Optional[Dict[str, Any]] = None
                ) -> ParsedDocument:
    """Probe, select a parser and parse — the one call most callers need.

    Never raises for a bad document: every outcome is a :class:`ParsedDocument`
    with an explicit status (``parsed``, ``partial``, ``needs-ocr``,
    ``encrypted``, ``unsupported`` or ``failed``).
    """
    context = DocumentParseContext(
        logical_key=logical_key, filename=filename or logical_key,
        workspace_id=workspace_id, declared_media_type=declared_media_type,
        limits=limits or DocumentLimits.from_config(),
        parser_options=dict(parser_options or {}))
    probe = probe_bytes(content, filename=context.filename,
                        declared_media_type=declared_media_type)
    return parse(content, context, probe=probe)


__all__ = [
    "DOCUMENT_SCHEMA_VERSION", "DocumentBlock", "DocumentLimits",
    "DocumentMetadata", "DocumentParseContext", "DocumentParseWarning",
    "DocumentProbe", "ParsedDocument", "UnsupportedContent",
    "AmbiguousParser", "DocumentParser", "ParserEntry", "ParserRegistryError",
    "list_parsers", "parse", "parse_bytes", "parser_names", "probe_bytes",
    "select",
]
