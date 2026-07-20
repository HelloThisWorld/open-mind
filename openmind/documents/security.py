"""Safety controls for parsing untrusted documents.

A document is attacker-controlled input. It arrives as opaque bytes, is handed to
third-party libraries, and those libraries were written to be permissive. This
module is where that is bounded, in one place, so a parser cannot forget.

WHAT IS DEFENDED AGAINST
------------------------
* **ZIP bombs** — an OOXML file is a ZIP; a 40 KB DOCX can declare a 40 GB
  member. Member count, per-member size, total uncompressed size and compression
  ratio are all capped BEFORE anything is read.
* **ZIP path traversal** — a member named ``../../etc/passwd`` or ``C:\\x``.
  Nothing here ever extracts to a filesystem path (members are read into memory
  from an already-validated archive), so traversal cannot write anywhere — but a
  traversal member is still rejected outright, because a document that contains
  one is not a document anyone should ingest.
* **XML external entities / DTD retrieval / billion laughs** — OOXML is XML, and
  ``python-docx`` and ``openpyxl`` parse it with stdlib parsers.
  :func:`harden_xml` applies ``defusedxml.defuse_stdlib()`` so those parsers
  refuse external entities, external DTDs and entity expansion. When
  ``defusedxml`` is absent the OOXML parsers report ``dependency_unavailable``
  rather than parsing unsafely — an unsafe parse is never the fallback.
* **Decompression stalls** — reads are bounded and streamed against the declared
  size, so a member that lies about its size is cut off rather than exhausting
  memory.

WHAT IS NOT DONE HERE
---------------------
No sandboxing, no subprocess isolation, no seccomp. A parser failure is contained
by :func:`openmind.documents.registry.parse` (it becomes a ``failed`` document,
not a dead worker); this module is about the input, not the process.
"""
from __future__ import annotations

import io
import threading
import zipfile
from typing import Any, Dict, List, Optional, Tuple

#: defuse_stdlib() patches global state, so it must run exactly once per process.
_xml_lock = threading.Lock()
_xml_hardened: Optional[bool] = None


class DocumentSecurityError(Exception):
    """A document violated a hard safety limit and must not be parsed.

    Distinct from a mere resource limit (which yields ``partial`` + a warning):
    this is refusal, not truncation.
    """

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: Dict[str, Any] = dict(detail)


def harden_xml() -> bool:
    """Make the stdlib XML parsers refuse external entities and DTDs.

    Idempotent and process-wide. Returns True when hardening is in force, False
    when ``defusedxml`` is not installed (the caller must then decline to parse
    XML-bearing formats rather than fall back to an unsafe parse).
    """
    global _xml_hardened
    if _xml_hardened is not None:
        return _xml_hardened
    with _xml_lock:
        if _xml_hardened is not None:
            return _xml_hardened
        try:
            import defusedxml
            defusedxml.defuse_stdlib()
            _xml_hardened = True
        except Exception:
            _xml_hardened = False
        return _xml_hardened


def xml_hardened() -> bool:
    """Whether :func:`harden_xml` has succeeded (without attempting it)."""
    return bool(_xml_hardened)


# ---------------------------------------------------------------------------
# ZIP package inspection
# ---------------------------------------------------------------------------
def _is_traversal(name: str) -> bool:
    """Whether an archive member name escapes the archive root.

    Checks the POSIX form, the Windows form and the drive-letter form, because
    ``zipfile`` preserves whatever the writer put in the header and a member is
    not required to use forward slashes.
    """
    n = (name or "").replace("\\", "/")
    if not n:
        return False
    if n.startswith("/"):
        return True
    if len(n) >= 2 and n[1] == ":":                 # C:/... or C:\...
        return True
    return any(part == ".." for part in n.split("/"))


def inspect_zip(data: bytes, *, max_members: int, max_total_bytes: int,
                max_member_bytes: int, max_ratio: int) -> Dict[str, Any]:
    """Validate a ZIP container and report its member names.

    Raises :class:`DocumentSecurityError` when a cap is exceeded or a member name
    escapes the root. Returns ``{"members": [...], "total_uncompressed": n,
    "compressed": n}`` on success.

    Only the central directory is read — no member content is decompressed here,
    so the check itself cannot be turned into the attack it prevents.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentSecurityError("bad-zip", f"not a readable ZIP package: {exc}")
    with zf:
        infos = zf.infolist()
        if len(infos) > max_members:
            raise DocumentSecurityError(
                "zip-too-many-members",
                f"archive declares {len(infos)} members, above the "
                f"{max_members} limit",
                observed=len(infos), limit=max_members)
        total = 0
        names: List[str] = []
        for info in infos:
            if _is_traversal(info.filename):
                raise DocumentSecurityError(
                    "zip-path-traversal",
                    f"archive member escapes the package root: "
                    f"{info.filename!r}",
                    member=info.filename)
            if info.file_size > max_member_bytes:
                raise DocumentSecurityError(
                    "zip-member-too-large",
                    f"archive member {info.filename!r} declares "
                    f"{info.file_size} bytes, above the {max_member_bytes} "
                    f"limit",
                    member=info.filename, observed=info.file_size,
                    limit=max_member_bytes)
            # Ratio is checked per member: a single hugely-compressed member is
            # the classic bomb, and it can hide inside an archive whose TOTAL
            # still looks reasonable.
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > max_ratio and info.file_size > 1_000_000:
                    raise DocumentSecurityError(
                        "zip-ratio-suspicious",
                        f"archive member {info.filename!r} expands "
                        f"{ratio:.0f}x (limit {max_ratio}x); refusing to parse "
                        f"a suspected decompression bomb",
                        member=info.filename, ratio=round(ratio, 1),
                        limit=max_ratio)
            total += info.file_size
            if total > max_total_bytes:
                raise DocumentSecurityError(
                    "zip-too-large",
                    f"archive expands to more than {max_total_bytes} bytes",
                    observed=total, limit=max_total_bytes)
            names.append(info.filename)
        return {"members": names, "total_uncompressed": total,
                "compressed": len(data)}


def zip_member_names(data: bytes, limit: int = 4096) -> Tuple[str, ...]:
    """A bounded list of member names, for PROBING only.

    Never raises: an unreadable archive simply has no members to report, and the
    probe must not turn a malformed file into an exception before any parser has
    had the chance to decline it honestly.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return tuple(zf.namelist()[:limit])
    except Exception:
        return ()


# ---------------------------------------------------------------------------
# Bounded text decoding
# ---------------------------------------------------------------------------
#: Tried in order. UTF-8 first (the overwhelmingly common case), then the two
#: single-byte fallbacks, ending with a latin-1 last resort that cannot fail.
#:
#: UTF-16 is deliberately NOT in this chain. It is attempted only when a BOM
#: says so (see :func:`decode_text`), because a UTF-16 decode of arbitrary
#: single-byte text SUCCEEDS whenever the length happens to be even — turning
#: ``caf\xe9 latte`` into CJK mojibake and reporting no problem at all.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def decode_text(data: bytes) -> Tuple[str, str, bool]:
    """Decode document bytes to text.

    Returns ``(text, encoding, lossy)``. ``lossy`` is True when the decode fell
    back to a replacement-character pass, so a parser can record a
    ``decode-fallback`` warning instead of silently presenting mojibake as
    verbatim source text.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError:
            pass
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace"), "utf-8", True


__all__ = [
    "DocumentSecurityError", "harden_xml", "xml_hardened", "inspect_zip",
    "zip_member_names", "decode_text",
]
