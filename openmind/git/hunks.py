"""Changed-hunk extraction (spec §15).

Parses the ``@@ -a,b +c,d @@`` headers of a zero-context unified diff
(``git diff --unified=0``) into normalized 1-based changed ranges. This module
is pure text work — no subprocess, no database — so it is exhaustively testable
on captured diff strings.

Rules enforced here:

* line numbers are 1-based;
* a ``-a,b`` hunk contributes a before-side range, ``+c,d`` an after-side one;
* a count of 0 marks an insertion/deletion point (Git emits ``+c,0`` for a
  pure deletion and ``-a,0`` for a pure insertion) — represented with count 0
  and never given a fake width;
* binary files have no hunks and therefore no ranges (the caller must not ask);
* a malformed header makes the whole parse fail honestly
  (:class:`MalformedDiff`) rather than inventing ranges.
"""
from __future__ import annotations

import difflib
import re
from typing import List, Tuple

from .errors import GitError
from .models import ChangedRange

#: ``@@ -oldStart[,oldCount] +newStart[,newCount] @@[ optional section]``
_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@")


class MalformedDiff(GitError):
    """A unified-diff hunk header could not be parsed. The file's ranges are
    reported as failed rather than guessed."""

    code = "malformed_diff"
    exit_code = 4
    http_status = 500


def parse_unified_hunks(diff_text: str) -> Tuple[List[ChangedRange],
                                                 List[ChangedRange]]:
    """Return ``(before_ranges, after_ranges)`` from a single file's
    zero-context unified diff body.

    Only the ``@@`` headers are read; with ``--unified=0`` the counts on the
    header are exactly the changed line counts, so the +/- body lines do not
    need to be walked."""
    before: List[ChangedRange] = []
    after: List[ChangedRange] = []
    for line in diff_text.splitlines():
        if not line.startswith("@@"):
            continue
        m = _HUNK_RE.match(line)
        if not m:
            raise MalformedDiff(
                "unparseable unified-diff hunk header",
                details={"line": line[:200]})
        old_start = int(m.group("os"))
        old_count = int(m.group("oc")) if m.group("oc") is not None else 1
        new_start = int(m.group("ns"))
        new_count = int(m.group("nc")) if m.group("nc") is not None else 1
        if old_count > 0:
            before.append(ChangedRange(start=old_start, count=old_count))
        if new_count > 0:
            after.append(ChangedRange(start=new_start, count=new_count))
    return _normalize(before), _normalize(after)


def _normalize(ranges: List[ChangedRange]) -> List[ChangedRange]:
    """Sort by start and merge touching/overlapping ranges — deterministic and
    minimal so downstream intersection tests are cheap."""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: (r.start, r.count))
    merged: List[ChangedRange] = [ordered[0]]
    for r in ordered[1:]:
        last = merged[-1]
        if r.start <= last.end + 1:
            new_end = max(last.end, r.end)
            merged[-1] = ChangedRange(start=last.start,
                                      count=new_end - last.start + 1)
        else:
            merged.append(r)
    return merged


def changed_ranges_from_text(before_text: str, after_text: str
                             ) -> Tuple[List[ChangedRange], List[ChangedRange]]:
    """Deterministic changed ranges computed directly from before/after text.

    Used for working-tree sides that have no committed blob object to feed to
    ``git diff`` (spec §16: unstaged/untracked content is read from the
    worktree). ``difflib`` opcodes give the same "which line spans changed"
    answer git's ``--unified=0`` does, on the exact bytes already snapshotted,
    so the result is reproducible and needs no extra subprocess."""
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    before: List[ChangedRange] = []
    after: List[ChangedRange] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            before.append(ChangedRange(start=i1 + 1, count=i2 - i1))
        if j2 > j1:
            after.append(ChangedRange(start=j1 + 1, count=j2 - j1))
    return _normalize(before), _normalize(after)


def ranges_intersect(ranges: List[ChangedRange], start: int, end: int) -> bool:
    """True if any range intersects the inclusive ``[start, end]`` span. Used
    to decide whether a changed line touches a segment's source range."""
    return any(r.intersects(start, end) for r in ranges)


__all__ = ["parse_unified_hunks", "changed_ranges_from_text",
           "ranges_intersect", "MalformedDiff"]
