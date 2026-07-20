"""Shared block assembly for every parser.

Ten parsers each enforcing "cap the block count, cap the block length, downgrade
to partial, record the exact warning, keep the heading stack consistent" would be
ten chances to get one of those wrong, and the wrong ones would be the silent
ones. So it is written once here, and a parser only decides WHAT a block is.

WHAT THE BUILDER GUARANTEES
---------------------------
* Ordinals are dense and assigned in emission order.
* ``block_key`` is unique within the document (a deterministic ``#n`` suffix
  resolves a collision — never a random id, because the key is hashed into the
  Revision's ``structure_hash``).
* A block longer than ``max_block_chars`` is cut at the limit AND recorded as a
  truncation warning naming the block, and the document becomes ``partial``.
  Truncation is never silent.
* Once ``max_blocks`` is reached, further blocks are refused, the document
  becomes ``partial``, and exactly ONE limit warning is emitted (not one per
  refused block, which would turn a bounded document into an unbounded warning
  list).
* The heading stack yields each block's ``heading_path`` and ``parent_key``, so
  a parser never maintains that bookkeeping itself.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..domain.types import ContentMode, DocumentBlockType
from .models import DocumentBlock, ParsedDocument

#: Where a truncated block's text is cut. Kept as its own constant so the marker
#: is testable and identical everywhere.
TRUNCATION_MARKER = "\n[truncated by OpenMind: block exceeded the size limit]"


class DocumentBuilder:
    """Accumulates blocks into a :class:`ParsedDocument` under the parse limits."""

    def __init__(self, doc: ParsedDocument, *, max_blocks: int,
                 max_block_chars: int, root_key: str = "doc") -> None:
        self.doc = doc
        self.max_blocks = max(1, int(max_blocks))
        self.max_block_chars = max(1, int(max_block_chars))
        self.root_key = root_key
        self._keys: Dict[str, int] = {}
        self._limit_reported = False
        #: (level, block_key, heading text) for each open heading, outermost first.
        self._headings: List[Tuple[int, str, str]] = []

    # -- state --------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.doc.blocks)

    @property
    def full(self) -> bool:
        """Whether the block budget is spent — and, if so, RECORD that.

        Reading this is not a neutral query. Every parser consults it as its
        early-exit guard, so the moment it first answers True is the exact moment
        content starts being dropped. Reporting here (rather than only inside
        :meth:`add`) is what stops a parser that breaks out of its loop from
        producing a silently truncated document still labelled ``parsed``.
        """
        if self.count >= self.max_blocks:
            self._report_block_limit()
            return True
        return False

    @property
    def heading_path(self) -> List[str]:
        return [text for _, _, text in self._headings]

    @property
    def current_parent(self) -> str:
        """The innermost open heading's key, or the root."""
        return self._headings[-1][1] if self._headings else self.root_key

    def push_heading(self, level: int, key: str, text: str) -> None:
        """Open a heading at *level*, closing any equal or deeper ones first.

        Called AFTER the heading's own block is added, so the heading block's own
        ``heading_path`` names its ancestors and not itself.
        """
        while self._headings and self._headings[-1][0] >= level:
            self._headings.pop()
        self._headings.append((int(level), key, text))

    # -- emission -----------------------------------------------------------
    def add(self, block_type: str, text: str, *, key: str,
            locator: Optional[Dict[str, Any]] = None,
            content_mode: str = ContentMode.VERBATIM,
            metadata: Optional[Dict[str, Any]] = None,
            parent_key: Optional[str] = None,
            heading_path: Optional[List[str]] = None,
            indexable: Optional[bool] = None) -> Optional[DocumentBlock]:
        """Append one block, or return None when the block budget is spent.

        ``indexable`` defaults to "content-bearing and not a structural
        container", which is the rule the vector projection relies on.
        """
        if self.full:
            self._report_block_limit()
            return None

        text = "" if text is None else str(text)
        if len(text) > self.max_block_chars:
            keep = max(0, self.max_block_chars - len(TRUNCATION_MARKER))
            text = text[:keep] + TRUNCATION_MARKER
            self.doc.mark_partial(
                "limit-exceeded",
                f"block {key!r} exceeded DOCUMENT_MAX_BLOCK_CHARS "
                f"({self.max_block_chars}) and was truncated",
                limit="DOCUMENT_MAX_BLOCK_CHARS", block=key,
                allowed=self.max_block_chars)

        if indexable is None:
            indexable = (block_type not in DocumentBlockType.CONTAINERS
                         and bool(text.strip()))

        block = DocumentBlock(
            block_key=self._unique(key),
            block_type=block_type,
            ordinal=self.count,
            text=text,
            parent_key=(self.current_parent if parent_key is None else parent_key),
            heading_path=(list(self.heading_path) if heading_path is None
                          else list(heading_path)),
            content_mode=content_mode,
            locator=dict(locator or {}),
            metadata=dict(metadata or {}),
            indexable=bool(indexable))
        self.doc.blocks.append(block)
        return block

    def add_root(self, title: str, locator: Dict[str, Any], *,
                 metadata: Optional[Dict[str, Any]] = None) -> DocumentBlock:
        """The single ``document`` root block. Always stored, never indexed: it
        is scaffolding, and embedding a title on its own dilutes retrieval."""
        block = DocumentBlock(
            block_key=self.root_key, block_type=DocumentBlockType.DOCUMENT,
            ordinal=self.count, text=title or "", parent_key="",
            heading_path=[], content_mode=ContentMode.DERIVED,
            locator=dict(locator), metadata=dict(metadata or {}),
            indexable=False)
        self.doc.blocks.append(block)
        self._keys[self.root_key] = 1
        return block

    # -- internals ----------------------------------------------------------
    def _unique(self, key: str) -> str:
        """A collision-free block key. Deterministic: the same input sequence
        always produces the same keys, because the suffix counts prior uses of
        that exact key rather than a global counter."""
        key = key or "block"
        seen = self._keys.get(key, 0)
        self._keys[key] = seen + 1
        return key if seen == 0 else f"{key}#{seen}"

    def _report_block_limit(self) -> None:
        if self._limit_reported:
            return
        self._limit_reported = True
        self.doc.mark_partial(
            "limit-exceeded",
            f"document reached DOCUMENT_MAX_BLOCKS ({self.max_blocks}); the "
            f"remaining structure was not extracted",
            limit="DOCUMENT_MAX_BLOCKS", allowed=self.max_blocks)


def slug(text: str, limit: int = 48) -> str:
    """A short, deterministic, filesystem-safe fragment of *text*.

    Used inside block keys so a key reads as ``h2-interfaces-namecheck`` rather
    than ``h2-7``. Lower-cased, non-alphanumerics collapsed to single hyphens.
    """
    out: List[str] = []
    prev_dash = False
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash and out:
            out.append("-")
            prev_dash = True
        if len(out) >= limit:
            break
    return "".join(out).strip("-")


__all__ = ["DocumentBuilder", "TRUNCATION_MARKER", "slug"]
