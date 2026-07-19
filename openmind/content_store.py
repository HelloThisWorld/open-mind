"""Immutable, content-addressed blob store for canonical Asset content.

WHY THIS EXISTS
---------------
An :class:`~openmind.domain.types.Evidence` citation must stay readable after
the source file it came from has changed on disk. The live file is not a
reliable source of historical content, and Chroma is a lossy retrieval
projection (headers, truncation, derived summaries) — neither can reconstruct
the exact bytes of a prior revision. So every observed revision's exact bytes
are snapshotted here, keyed by their SHA-256, and the database stores only the
hash.

LAYOUT
------
``data/<workspace_id>/objects/<first-2-hex>/<sha256-hex>``

The blob lives INSIDE the workspace data directory, so deleting the workspace
(``shutil.rmtree(config.project_dir(id))``) reclaims its blobs with everything
else, and terminating the workspace can drop just ``objects/`` via
:func:`clear_workspace`.

INVARIANTS
----------
1. Blob identity is the SHA-256 of the exact bytes.
2. Writes are atomic: a temp file in the same directory + ``os.replace``.
3. An existing matching blob is reused (writing it again is a no-op).
4. A hash mismatch on read raises :class:`ContentCorruption` — never returns
   silently-wrong bytes.
5. The database stores only the blob hash, never an absolute blob path.
6. Old revision blobs are retained; nothing here ever prunes.
7. Binary content round-trips even when Phase 2 cannot parse it.

This module depends only on :mod:`config` and the domain error, so it is
testable with no Chroma, no FastAPI and no database.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from . import config
from .domain.errors import ContentCorruption

#: Read blobs in bounded chunks so verifying a large file never loads it whole
#: just to hash it.
_READ_CHUNK = 1 << 20  # 1 MiB


def hash_bytes(data: bytes) -> str:
    """The canonical blob identity: SHA-256 hex of the exact bytes."""
    return hashlib.sha256(data).hexdigest()


def objects_dir(workspace_id: str) -> Path:
    """The workspace's blob root: ``data/<workspace_id>/objects``."""
    return config.project_dir(workspace_id) / "objects"


def _blob_path(workspace_id: str, blob_hash: str) -> Path:
    # A two-hex fan-out keeps any single directory from holding every blob.
    prefix = blob_hash[:2] if len(blob_hash) >= 2 else "00"
    return objects_dir(workspace_id) / prefix / blob_hash


def put(workspace_id: str, data: bytes) -> str:
    """Store *data* and return its SHA-256 hex. Idempotent: an identical blob is
    reused, so re-ingesting the same content writes nothing.

    Returns the blob hash the database should persist. The write is atomic — a
    reader never sees a half-written blob.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("content_store.put expects bytes")
    blob_hash = hash_bytes(bytes(data))
    dest = _blob_path(workspace_id, blob_hash)
    if dest.exists():
        return blob_hash                       # invariant 3: reuse
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Temp file in the SAME directory so os.replace is a same-filesystem atomic
    # rename. The name is unique per CALL (pid + a random token), so two writers
    # of the same not-yet-present blob never interleave writes into one temp
    # file; each fully writes its own temp and atomically replaces the single
    # content-addressed path with identical bytes — last writer simply wins.
    tmp = dest.parent / f".{blob_hash}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)                  # invariant 2: atomic
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return blob_hash


def exists(workspace_id: str, blob_hash: str) -> bool:
    """Whether a blob with this hash is present (does not verify integrity)."""
    return bool(blob_hash) and _blob_path(workspace_id, blob_hash).is_file()


def get(workspace_id: str, blob_hash: str) -> bytes:
    """Return the exact bytes for *blob_hash*.

    Raises :class:`ContentCorruption` if the blob is missing or if its bytes no
    longer hash to *blob_hash* (invariant 4).
    """
    path = _blob_path(workspace_id, blob_hash)
    if not path.is_file():
        raise ContentCorruption(
            f"content blob missing: {blob_hash}", blob_hash=blob_hash,
            details={"workspace_id": workspace_id})
    data = path.read_bytes()
    actual = hash_bytes(data)
    if actual != blob_hash:
        raise ContentCorruption(
            f"content blob {blob_hash} is corrupt (bytes hash to {actual})",
            blob_hash=blob_hash,
            details={"workspace_id": workspace_id, "computed": actual})
    return data


def verify(workspace_id: str, blob_hash: str) -> bool:
    """True iff the blob exists and its bytes still hash to *blob_hash*.

    Never raises — a missing or corrupt blob returns ``False`` — so callers that
    only want a health check do not have to catch :class:`ContentCorruption`.
    """
    path = _blob_path(workspace_id, blob_hash)
    if not path.is_file():
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest() == blob_hash


def clear_workspace(workspace_id: str) -> None:
    """Remove every blob for a workspace (its whole ``objects/`` tree).

    Used by workspace TERMINATE, which wipes learned data but keeps the
    workspace. Workspace DELETE removes the entire data directory, which
    includes ``objects/``, so it does not need this.
    """
    root = objects_dir(workspace_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


__all__ = ["hash_bytes", "objects_dir", "put", "get", "exists", "verify",
           "clear_workspace"]
