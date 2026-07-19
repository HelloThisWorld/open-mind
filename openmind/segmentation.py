"""Deterministic source segmentation for the canonical Asset model.

A Segment is a stable structural unit inside one immutable revision, and every
Segment carries source-locatable Evidence. This module turns one revision's
decoded text into Segment + Evidence drafts ready for
:func:`openmind.db.commit_revision`.

SHARED BOUNDARIES, NOT A REWRITE
--------------------------------
The RAG chunker (:func:`openmind.rag.chunk_file`) and this segmenter derive from
the *same* deterministic boundary primitives — the tree-sitter type/method
facts in :mod:`openmind.javaparse` and the ``CHUNK_MAX_LINES`` /
``CHUNK_OVERLAP_LINES`` line-range split — so their line ranges agree. But
``rag.chunk_file`` is deliberately left byte-for-byte unchanged: the RAG chunk
ids, the embedded header, the chunk metadata and the search response are a stable
contract, and forcing a one-to-one Segment↔chunk mapping would risk regressing
retrieval for no gain. Preserving current retrieval behaviour matters more than
an artificial mapping (see docs/v2/phase-2-asset-model.md §7). A determinism test
asserts the two agree on line ranges so they cannot drift.

CONTENT MODE
------------
* Java methods/constructors and generic line-range segments are ``verbatim`` —
  their content is exactly the source lines they cover.
* The Java class-summary segment is ``derived`` — its content is the generated
  signature summary (the same one the RAG class-summary chunk embeds), never
  misrepresented as verbatim source. Its Evidence still cites the verbatim class
  source range.

RECOVERABILITY
--------------
Every segment's line range and every evidence ``content_hash`` is computed over
the exact line slice ``[startLine, endLine]`` of the revision text, so the
content is recomputable from the immutable content blob after the source file
changes — with no running model involved.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from . import config
from . import javaparse as jp

#: The stored evidence excerpt is a bounded verbatim preview; the authoritative,
#: full content is recovered from the immutable blob at read time. Bounding the
#: stored copy keeps the database small and honours "excerpt is bounded".
EXCERPT_STORE_MAX = 1200


def hash_text_utf8(text: str) -> str:
    """SHA-256 of ``text`` encoded UTF-8 — the same encoding the content blob is
    stored in, so a segment/evidence hash is recomputable from the blob."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def slice_lines(text: str, start_line: int, end_line: int) -> str:
    """The verbatim 1-based inclusive line slice ``[start_line, end_line]`` of
    *text*. Tolerant of out-of-range bounds (clamped), so a stale locator never
    raises here — the caller decides how to report a mismatch."""
    if start_line <= 0 or end_line <= 0:
        return ""
    lines = text.split("\n")
    lo = max(0, start_line - 1)
    hi = min(len(lines), end_line)
    if lo >= hi:
        return ""
    return "\n".join(lines[lo:hi])


def _split_line_ranges(text: str, start_line: int, max_lines: int,
                       overlap: int):
    """Deterministic bounded line-range split. Byte-for-byte the same boundaries
    as :func:`openmind.rag._split_lines` (kept in lockstep by a determinism
    test), so a generic file's segments and its RAG chunks cover identical
    ranges."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        yield start_line, start_line + len(lines) - 1
        return
    i = 0
    while i < len(lines):
        seg = lines[i:i + max_lines]
        yield start_line + i, start_line + i + len(seg) - 1
        if i + max_lines >= len(lines):
            break
        i += max_lines - overlap


def _evidence(logical_key: str, text: str, start_line: int, end_line: int,
              symbol: str) -> Dict[str, Any]:
    """Build an Evidence draft citing the verbatim source range. ``content_hash``
    validates the FULL slice; ``excerpt`` is a bounded preview of it."""
    verbatim = slice_lines(text, start_line, end_line)
    excerpt = verbatim[:EXCERPT_STORE_MAX]
    return {
        "locator": {"kind": "source-range", "file": logical_key,
                    "startLine": start_line, "endLine": end_line, "symbol": symbol},
        "excerpt": excerpt,
        "content_hash": hash_text_utf8(verbatim),
    }


def _java_segments(logical_key: str, text: str) -> List[Dict[str, Any]]:
    """Java segments via tree-sitter, or raise so the caller falls back to the
    generic line-range strategy."""
    tree = jp.parse(text)
    root = tree.root_node
    src = bytes(text, "utf8")
    package = jp.get_package(root, src)
    drafts: List[Dict[str, Any]] = []
    for t in jp.iter_types(root, src):
        cls = t["name"]
        fqcn = f"{package}.{cls}" if package else cls
        node = t["node"]
        s_line, e_line = t["start_line"], t["end_line"]
        # class-summary segment (DERIVED: the signature digest, not raw source) —
        # the same shape the RAG class-summary chunk embeds.
        sig_lines = [f"class {cls} ({t['kind']})"]
        for f in jp.iter_fields(node, src):
            sig_lines.append(f"  field {f['type']} {f['name']}")
        methods = list(jp.iter_methods(node, src))
        for m in methods:
            sig_lines.append(f"  method {m['signature']}")
        summary = "\n".join(sig_lines)
        drafts.append({
            "segment_key": f"type:{fqcn}",
            "segment_type": "type",
            "symbol": fqcn,
            "start_line": s_line, "end_line": e_line,
            "content_hash": hash_text_utf8(summary),
            "content_mode": "derived",
            "metadata": {"kind": t["kind"], "package": package, "class": cls},
            "evidence": _evidence(logical_key, text, s_line, e_line, fqcn),
        })
        # one verbatim segment per method / constructor
        for m in methods:
            is_ctor = getattr(m["node"], "type", "") == "constructor_declaration"
            seg_type = "constructor" if is_ctor else "method"
            signature = " ".join((m["signature"] or "").split())
            member = f"{fqcn}#{signature}"
            ms, me = m["start_line"], m["end_line"]
            verbatim = slice_lines(text, ms, me)
            drafts.append({
                "segment_key": f"{seg_type}:{member}",
                "segment_type": seg_type,
                "symbol": member,
                "start_line": ms, "end_line": me,
                "content_hash": hash_text_utf8(verbatim),
                "content_mode": "verbatim",
                "metadata": {"class": cls, "package": package,
                             "signature": signature},
                "evidence": _evidence(logical_key, text, ms, me, member),
            })
    return drafts


def _generic_segments(logical_key: str, text: str) -> List[Dict[str, Any]]:
    """Deterministic bounded line-range segments for config / non-Java / parse-
    failure files — 1:1 with the generic RAG chunks."""
    basename = logical_key.replace("\\", "/").rsplit("/", 1)[-1]
    drafts: List[Dict[str, Any]] = []
    for i, (sl, el) in enumerate(
            _split_line_ranges(text, 1, config.CHUNK_MAX_LINES,
                               config.CHUNK_OVERLAP_LINES)):
        verbatim = slice_lines(text, sl, el)
        drafts.append({
            "segment_key": f"file-range:{i + 1:06d}",
            "segment_type": "file",
            "symbol": basename,
            "start_line": sl, "end_line": el,
            "content_hash": hash_text_utf8(verbatim),
            "content_mode": "verbatim",
            "metadata": {},
            "evidence": _evidence(logical_key, text, sl, el, basename),
        })
    return drafts


def _disambiguate(drafts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Guarantee ``segment_key`` is unique within a revision (the DB enforces
    ``UNIQUE(revision_id, segment_key)``). Ambiguous/duplicate symbols get a
    deterministic ``@<start_line>`` position suffix rather than a bare index; an
    exact collision at the same line (should not occur) falls back to ordinal."""
    seen: Dict[str, int] = {}
    for i, d in enumerate(drafts):
        d["ordinal"] = i
        key = d["segment_key"]
        if key in seen:
            candidate = f"{key}@{d.get('start_line') or 0}"
            if candidate in seen:
                candidate = f"{candidate}#{i}"
            d["segment_key"] = candidate
        seen[d["segment_key"]] = i
    return drafts


def segment_source(logical_key: str, text: str) -> List[Dict[str, Any]]:
    """Decompose one revision's text into ordered Segment drafts (each carrying an
    Evidence draft), ready for :func:`openmind.db.commit_revision`.

    Deterministic and model-free. Java files use tree-sitter when available and
    fall back to the generic line-range strategy on any parse failure, exactly as
    the RAG chunker does. Always returns at least one segment (an empty file
    yields a single ``file-range:000001``).
    """
    drafts: List[Dict[str, Any]] = []
    if logical_key.lower().endswith(".java") and jp.available():
        try:
            drafts = _java_segments(logical_key, text)
        except Exception:
            drafts = []
    if not drafts:
        drafts = _generic_segments(logical_key, text)
    return _disambiguate(drafts)


__all__ = ["segment_source", "slice_lines", "hash_text_utf8",
           "EXCERPT_STORE_MAX"]
