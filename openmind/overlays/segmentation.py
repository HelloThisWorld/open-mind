"""Overlay segmentation (spec §19).

Reuses the existing deterministic segmenter
(:func:`openmind.segmentation.segment_source`), which already operates on
``(logical_path, text)`` rather than a live filesystem path — so an overlay can
segment a blob that was never checked out, with no parser logic duplicated.

The added value here is CHANGE CLASSIFICATION: comparing the before-side and
after-side segments of one changed file and labelling each as
``added | modified | deleted | moved | context-changed | unchanged``. A segment
is ``modified`` when a changed line range intersects its source range OR its
content hash changed — so editing one Java method does not mark every method in
the file modified, and a pure rename does not mark unchanged methods modified.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import segmentation as base_seg
from ..git.hunks import ranges_intersect
from ..git.models import ChangedRange
from ..git.vocabularies import SegmentChange, Side


def _decode(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def segment_side(logical_path: str, data: Optional[bytes]) -> List[Dict[str, Any]]:
    """Segment one side's bytes into drafts, or [] when unparseable/absent."""
    text = _decode(data)
    if text is None:
        return []
    try:
        return base_seg.segment_source(logical_path, text)
    except Exception:
        return []


def classify_segments(before_segs: List[Dict[str, Any]],
                      after_segs: List[Dict[str, Any]],
                      before_ranges: List[ChangedRange],
                      after_ranges: List[ChangedRange],
                      *, path_moved: bool = False
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Classify before/after segments. Returns ``(before_out, after_out)`` where
    each segment dict gains a ``change_class`` key.

    Matching is by ``segment_key`` (stable symbol identity) with content-hash
    equality deciding modified-vs-unchanged; a segment present on only one side
    is added/deleted."""
    before_by_key = {s["segment_key"]: s for s in before_segs}
    after_by_key = {s["segment_key"]: s for s in after_segs}

    before_out: List[Dict[str, Any]] = []
    for s in before_segs:
        key = s["segment_key"]
        s = dict(s)
        if key not in after_by_key:
            s["change_class"] = SegmentChange.DELETED
        else:
            other = after_by_key[key]
            if other.get("content_hash") == s.get("content_hash"):
                s["change_class"] = (SegmentChange.MOVED if path_moved
                                     else SegmentChange.UNCHANGED)
            else:
                s["change_class"] = SegmentChange.MODIFIED
        before_out.append(s)

    after_out: List[Dict[str, Any]] = []
    for s in after_segs:
        key = s["segment_key"]
        s = dict(s)
        if key not in before_by_key:
            s["change_class"] = SegmentChange.ADDED
        else:
            other = before_by_key[key]
            if other.get("content_hash") == s.get("content_hash"):
                # Content identical; a changed range that merely brushes context
                # marks it context-changed, otherwise unchanged/moved.
                touched = ranges_intersect(
                    after_ranges, s.get("start_line", 0), s.get("end_line", 0))
                if path_moved:
                    s["change_class"] = SegmentChange.MOVED
                elif touched:
                    s["change_class"] = SegmentChange.CONTEXT_CHANGED
                else:
                    s["change_class"] = SegmentChange.UNCHANGED
            else:
                s["change_class"] = SegmentChange.MODIFIED
        after_out.append(s)
    return before_out, after_out


def changed_after_segments(after_out: List[Dict[str, Any]]
                           ) -> List[Dict[str, Any]]:
    """The after-side segments that actually changed (added/modified) — the ones
    worth embedding and projecting."""
    return [s for s in after_out
            if s.get("change_class") in (SegmentChange.ADDED,
                                         SegmentChange.MODIFIED)]


__all__ = ["segment_side", "classify_segments", "changed_after_segments"]
