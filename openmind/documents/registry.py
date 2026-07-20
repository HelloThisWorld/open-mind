"""The document parser registry: probe → exactly one parser → normalized result.

SELECTION RULES
---------------
1. **Deterministic order.** Each parser registers with an explicit integer
   priority; ties break on name. ``list_parsers()`` returns that order, and the
   order does not depend on import timing, dict iteration or filesystem listing.
2. **Exactly one parser.** ``select()`` returns the single highest-priority
   parser whose ``supports(probe)`` is true.
3. **Ambiguity fails loudly.** Two parsers at the SAME priority both claiming a
   probe raise :class:`AmbiguousParser`. Silently picking one would make the
   result depend on registration order, which is exactly the non-determinism
   rule 1 exists to prevent.
4. **Missing dependency is a per-format failure**, reported as
   ``dependency_unavailable`` — never a crash, and never a fallback to some other
   parser that would produce plausible-looking nonsense.
5. **An unsupported format is ``unsupported``**, never an empty successful parse.

LAZY IMPORTS ARE A CONTRACT, NOT AN OPTIMIZATION
------------------------------------------------
Importing :mod:`openmind.documents` must import NO parser dependency. The
registry stores module paths and imports a parser module only when its probe
matches; the third-party library is imported inside ``parse()``, deeper still.
That is what keeps the dependency-free ``.openmind`` artifact export runnable on
a machine with no document dependencies installed at all.

ISOLATION
---------
:func:`parse` catches every exception a parser can raise and converts it into a
``failed`` :class:`~openmind.documents.models.ParsedDocument`. A malformed
document fails its own job step; it never kills the worker process and never
corrupts another workspace's knowledge.
"""
from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Tuple

from .models import (DocumentParseContext, DocumentProbe, ParsedDocument,
                     failed, unsupported)
from .probe import probe_bytes


class DocumentParser(Protocol):
    """What every document parser implements."""

    name: str
    version: str

    def supports(self, probe: DocumentProbe) -> bool:
        """Whether this parser claims the probed bytes. Must not raise."""
        ...

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        """Produce the normalized document. May raise; the registry contains it."""
        ...


class ParserRegistryError(Exception):
    """Base class for registry-level failures."""


class AmbiguousParser(ParserRegistryError):
    """Two parsers at the same priority both claim one probe."""

    def __init__(self, names: List[str], probe: DocumentProbe) -> None:
        super().__init__(
            f"ambiguous parser selection for {probe.filename or '<bytes>'} "
            f"(detected {probe.detected_media_type or 'unknown'}): "
            f"{', '.join(sorted(names))} all claim it at the same priority. "
            f"Parser selection must be deterministic; give one a distinct "
            f"priority or narrow its supports().")
        self.names = sorted(names)
        self.probe = probe


@dataclass(frozen=True)
class ParserEntry:
    """One registered parser, resolved lazily.

    ``module`` and ``attribute`` are stored instead of an instance so
    registration costs one tuple and no import.
    """
    name: str
    priority: int
    module: str
    attribute: str
    #: The distribution that must be installed for this parser to run, if any.
    #: Reported verbatim in a ``dependency_unavailable`` result.
    distribution: str = ""

    def load(self) -> DocumentParser:
        module = importlib.import_module(self.module, package=__package__)
        return getattr(module, self.attribute)()


_lock = threading.RLock()
_entries: Dict[str, ParserEntry] = {}


def register(entry: ParserEntry) -> None:
    """Register (or replace) a parser entry by name."""
    with _lock:
        _entries[entry.name] = entry


def unregister(name: str) -> None:
    with _lock:
        _entries.pop(name, None)


def list_parsers() -> List[ParserEntry]:
    """Every registered parser in deterministic selection order."""
    with _lock:
        entries = list(_entries.values())
    return sorted(entries, key=lambda e: (e.priority, e.name))


def parser_names() -> Tuple[str, ...]:
    return tuple(e.name for e in list_parsers())


def get_parser(name: str) -> Optional[DocumentParser]:
    """Load one parser by name, or None if it is not registered / not loadable."""
    with _lock:
        entry = _entries.get(name)
    if entry is None:
        return None
    try:
        return entry.load()
    except Exception:
        return None


def select(probe: DocumentProbe) -> Optional[DocumentParser]:
    """The single parser that claims *probe*, or None.

    Raises :class:`AmbiguousParser` when two same-priority parsers both claim it.

    A parser's optional third-party dependency is NOT consulted here. Each parser
    claims its format on the probe's evidence alone and reports
    ``dependency_unavailable`` from its own ``parse()`` — so "this IS a DOCX but
    python-docx is missing" stays distinguishable from "this is not a DOCX", and
    a missing dependency can never make a file fall through to a parser that
    would mangle it.
    """
    claimed: List[Tuple[ParserEntry, DocumentParser]] = []
    winning_priority: Optional[int] = None
    for entry in list_parsers():        # ascending priority; lowest wins
        if winning_priority is not None and entry.priority > winning_priority:
            break                       # nothing further can tie or beat it
        try:
            instance = entry.load()
            claims = bool(instance.supports(probe))
        except Exception:
            # A parser whose module will not import, or whose supports() raises,
            # simply does not claim the probe. One broken parser must not break
            # selection for every other format.
            continue
        if claims:
            claimed.append((entry, instance))
            winning_priority = entry.priority

    if not claimed:
        return None
    if len(claimed) > 1:                # all at winning_priority, by the break
        raise AmbiguousParser([e.name for e, _ in claimed], probe)
    return claimed[0][1]


def parse(content: bytes, context: DocumentParseContext,
          probe: Optional[DocumentProbe] = None) -> ParsedDocument:
    """Probe, select and run a parser. Never raises for a bad document.

    Every outcome is a real :class:`ParsedDocument` carrying an explicit status:
    ``unsupported`` when nothing claims the bytes or a dependency is missing,
    ``failed`` when the parser raised, and ``parsed``/``partial``/``needs-ocr``/
    ``encrypted`` from the parser itself. :meth:`ParsedDocument.finalize` is
    applied here so no parser has to remember to.
    """
    if probe is None:
        probe = probe_bytes(content, filename=context.filename,
                            declared_media_type=context.declared_media_type)

    limits = context.limits
    if len(content) > limits.max_bytes:
        doc = unsupported(
            context.filename,
            f"document is {len(content)} bytes, above the "
            f"{limits.max_bytes}-byte limit")
        doc.add_warning(
            "limit-exceeded",
            f"document size {len(content)} exceeds DOCUMENT_MAX_BYTES "
            f"({limits.max_bytes}); it was not parsed",
            limit="DOCUMENT_MAX_BYTES", observed=len(content),
            allowed=limits.max_bytes)
        return doc.finalize()

    try:
        parser = select(probe)
    except AmbiguousParser as exc:
        doc = unsupported(context.filename, "ambiguous_parser")
        doc.add_warning("ambiguous-parser", str(exc), candidates=exc.names)
        return doc.finalize()

    if parser is None:
        return unsupported(
            context.filename,
            f"no registered parser claims {probe.detected_media_type or 'these bytes'}"
            + (f" ({probe.extension})" if probe.extension else ""),
            media_type=probe.detected_media_type)

    try:
        result = parser.parse(content, context)
    except Exception as exc:            # noqa: BLE001 - isolation is the point
        return failed(context.filename, getattr(parser, "name", "unknown"),
                      getattr(parser, "version", "0"), exc)
    if not isinstance(result, ParsedDocument):
        return failed(
            context.filename, getattr(parser, "name", "unknown"),
            getattr(parser, "version", "0"),
            TypeError(f"parser returned {type(result).__name__}, "
                      f"expected ParsedDocument"))
    return result.finalize()


# ---------------------------------------------------------------------------
# Built-in registrations
#
# Priority ordering, lowest first (checked first). The rule is: the more specific
# and the more strongly signature-identified a format is, the earlier it goes, so
# a generic text parser can never steal a file a structured parser would have
# recognized.
# ---------------------------------------------------------------------------
_BUILTINS: Tuple[ParserEntry, ...] = (
    ParserEntry("pdf", 10, ".pdf_parser", "PdfParser", "pypdf"),
    ParserEntry("docx", 11, ".docx_parser", "DocxParser", "python-docx"),
    ParserEntry("xlsx", 12, ".spreadsheet_parser", "XlsxParser", "openpyxl"),
    ParserEntry("openapi", 20, ".openapi_parser", "OpenApiParser"),
    ParserEntry("json-schema", 21, ".json_schema_parser", "JsonSchemaParser"),
    ParserEntry("sql", 30, ".sql_parser", "SqlParser"),
    ParserEntry("csv", 40, ".csv_parser", "CsvParser"),
    ParserEntry("html", 50, ".html_parser", "HtmlParser"),
    ParserEntry("markdown", 60, ".markdown_parser", "MarkdownParser"),
    ParserEntry("text", 90, ".text_parser", "TextParser"),
)

_installed = False


def install_builtin_parsers() -> None:
    """Register the built-in parsers. Idempotent; imports nothing."""
    global _installed
    with _lock:
        if _installed:
            return
        for entry in _BUILTINS:
            _entries.setdefault(entry.name, entry)
        _installed = True


def reset() -> None:
    """Drop every registration and re-install the built-ins. For tests."""
    global _installed
    with _lock:
        _entries.clear()
        _installed = False
    install_builtin_parsers()


install_builtin_parsers()


__all__ = [
    "DocumentParser", "ParserEntry", "ParserRegistryError", "AmbiguousParser",
    "register", "unregister", "list_parsers", "parser_names", "get_parser",
    "select", "parse", "install_builtin_parsers", "reset",
]
