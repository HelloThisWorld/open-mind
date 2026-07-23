"""Git change intelligence (OpenMind v2 Phase 7).

This package is a Git **reader**. It discovers local repositories, captures
coherent canonical Git baselines, resolves refs and merge-bases, extracts
diffs and changed hunks, and snapshots before/after blob content into the
immutable content store — all through exactly one subprocess boundary
(:mod:`openmind.git.command`) that permits only read-only Git commands and
never contacts a remote.

Nothing here mutates a Git repository, and nothing here writes into a
canonical Base Workspace table. The overlay plane that consumes these
primitives lives in :mod:`openmind.overlays`.

Boundaries kept explicit (spec §7):

* command execution      -> command.py
* ref/object validation  -> refs.py
* object interpretation  -> diff.py, hunks.py, snapshots.py, content.py
* repository model        -> repositories.py, models.py
* baseline coherence      -> baseline.py
* persistence            -> store.py
* service surface        -> service.py
"""
from __future__ import annotations

#: Version of the Git adapter. Participates in an overlay's source hash so a
#: change to how diffs/content are read invalidates cached overlay work.
GIT_ADAPTER_VERSION = "1"

__all__ = ["GIT_ADAPTER_VERSION"]
