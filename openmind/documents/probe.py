"""Format detection: what a document actually IS, not what it is called.

WHY NOT THE EXTENSION
---------------------
Selecting a parser from the suffix is how a ZIP archive renamed ``report.docx``
reaches a DOCX parser, and how a PDF renamed ``notes.txt`` gets decoded as text.
The probe therefore reads the leading bytes and, for ZIP containers, the member
list, so a parser can require **package structure** (``word/document.xml``,
``xl/workbook.xml``) before claiming a file.

The extension is still recorded and still used — as a *tiebreak* between formats
that share a byte signature (JSON vs. YAML vs. plain text all look like text),
never as the sole basis for a binary format.

DETECTION IS EVIDENCE, NOT A VERDICT
------------------------------------
:func:`probe_bytes` reports facts. Deciding which parser those facts justify is
the registry's job, and each parser's ``supports()`` is where the format-specific
requirement lives.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

from .models import DocumentProbe
from .security import zip_member_names

#: How many leading bytes are sampled for signature and text sniffing. Enough to
#: see any magic number and a meaningful text prefix; small enough that probing a
#: large document costs nothing.
HEAD_BYTES = 8192

_MEDIA_BY_EXT = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".rst": "text/x-rst", ".adoc": "text/asciidoc", ".asciidoc": "text/asciidoc",
    ".txt": "text/plain",
    ".html": "text/html", ".htm": "text/html",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "application/yaml", ".yml": "application/yaml",
    ".sql": "application/sql",
    ".pdf": "application/pdf",
    ".docx": ("application/vnd.openxmlformats-officedocument"
              ".wordprocessingml.document"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument"
              ".spreadsheetml.sheet"),
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
}

_DOCX_MARKER = "word/document.xml"
_XLSX_MARKER = "xl/workbook.xml"

_HTML_RE = re.compile(rb"<\s*(!doctype\s+html|html|head|body|div|p|h1)\b",
                      re.IGNORECASE)


def _magic(data: bytes) -> Dict[str, Any]:
    """Signature facts about the leading bytes."""
    facts: Dict[str, Any] = {}
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        facts["zip"] = True
    if data.startswith(b"%PDF-"):
        facts["pdf"] = True
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # Legacy OLE2 compound file: .doc / .xls / .ppt. Recorded so a parser can
        # decline it explicitly rather than mis-claiming a modern OOXML file.
        facts["ole2"] = True
    if data.startswith(b"\xef\xbb\xbf"):
        facts["bom"] = "utf-8"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        facts["bom"] = "utf-16"
    if b"\x00" in data[:1024] and "bom" not in facts:
        # A NUL in the first KB of something claiming to be text is the most
        # reliable binary signal there is.
        facts["binary"] = True
    try:
        data.decode("utf-8")
        facts["utf8"] = True
    except UnicodeDecodeError:
        facts["utf8"] = False
    return facts


def _looks_like_json(head: bytes) -> bool:
    stripped = head.lstrip()
    if not stripped[:1] in (b"{", b"["):
        return False
    try:                                   # a complete small document
        json.loads(head.decode("utf-8", "replace"))
        return True
    except Exception:
        # A truncated head that STARTS like JSON is still JSON-shaped; the
        # parser makes the final call on the full bytes.
        return True


def probe_bytes(data: bytes, filename: str = "",
                declared_media_type: str = "") -> DocumentProbe:
    """Build a :class:`DocumentProbe` from a document's bytes.

    Never raises: an unreadable or truncated file yields a probe whose facts say
    so, so the registry can report ``unsupported`` honestly rather than blowing
    up before any parser was consulted.
    """
    head = bytes(data[:HEAD_BYTES])
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    magic = _magic(head)

    if magic.get("zip"):
        # Reading the central directory is cheap and is what distinguishes a
        # DOCX from an XLSX from a plain ZIP that merely got a docx suffix.
        magic["zip_members"] = zip_member_names(data)

    detected = ""
    if magic.get("pdf"):
        detected = "application/pdf"
    elif magic.get("zip"):
        members = set(magic.get("zip_members") or ())
        if _DOCX_MARKER in members:
            detected = _MEDIA_BY_EXT[".docx"]
        elif _XLSX_MARKER in members:
            detected = _MEDIA_BY_EXT[".xlsx"]
        else:
            detected = "application/zip"
    elif magic.get("ole2"):
        detected = "application/x-ole-storage"
    elif magic.get("binary"):
        detected = "application/octet-stream"
    elif _HTML_RE.search(head):
        detected = "text/html"
    elif _looks_like_json(head):
        detected = "application/json"
    else:
        # Textual, but the flavour (Markdown vs YAML vs plain) is not decidable
        # from bytes alone. The extension is the honest tiebreak here, and it is
        # only ever consulted for text.
        detected = _MEDIA_BY_EXT.get(ext, "text/plain")

    return DocumentProbe(
        filename=base, extension=ext,
        declared_media_type=(declared_media_type or "").strip().lower(),
        detected_media_type=detected, size=len(data), magic=magic, head=head)


def media_type_for_extension(ext: str) -> str:
    return _MEDIA_BY_EXT.get((ext or "").lower(), "")


__all__ = ["probe_bytes", "media_type_for_extension", "HEAD_BYTES",
           "_DOCX_MARKER", "_XLSX_MARKER"]
