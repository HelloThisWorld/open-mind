"""Import planning: what should happen to these bytes, decided BEFORE writing.

THE DECISION IS THE PRODUCT
---------------------------
Appending a document is not "write a revision". Five different things could be
correct, and picking the wrong one silently is how a knowledge base quietly
corrupts itself:

``duplicate``          these exact bytes are already a document revision here.
                       Return what exists. No job, no revision, no vector
                       duplicate.
``revision``           the caller named an Asset, or the key resolves to one, and
                       the bytes differ. Mint the next revision.
``new_asset``          nothing matches. Create.
``possible_revision``  the default key is taken by DIFFERENT content and the
                       caller made no explicit choice. **Write nothing** and
                       hand back the ambiguity plus the exact commands that
                       resolve it.
``unsupported``        no parser claims these bytes.

``possible_revision`` is the important one. Two teams' "requirements.docx" are
not the same document, and merging them because their filenames match would
destroy one of them behind an operation that looked like it succeeded. So the
collision is surfaced, not resolved.

IDENTITY
--------
A document under a registered source root keeps the Phase 2 rule: workspace id +
workspace-relative path. A manually attached document gets
``documents/<normalized-filename>``, and ``--new-asset`` gets a deterministic,
READABLE distinct key — ``documents/<stem>--<hash-prefix>.<ext>`` — never an
opaque random name.

NO ABSOLUTE PATH EVER LEAVES THIS MODULE
----------------------------------------
The caller reads the bytes; planning sees content, a filename and options. The
resulting job payload carries a staged blob hash and a filename, never the
machine path the file came from.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from ..domain.types import ImportStatus

#: Where an attached document's logical key lives. A fixed prefix keeps attached
#: documents from colliding with real workspace paths.
ATTACHMENT_PREFIX = "documents"

#: How much of the content hash goes into a `--new-asset` key. Eight hex chars
#: is 4 billion values — plenty to disambiguate documents in one workspace, and
#: short enough that the key stays readable.
_HASH_PREFIX_CHARS = 8

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def normalize_filename(filename: str) -> str:
    """A safe, portable, deterministic file component.

    Strips any directory part (an attached document's origin path is not
    recorded), collapses unsafe characters, and refuses ``.``/``..``. The result
    is a NAME, never a path, so a crafted filename cannot escape the
    ``documents/`` prefix.
    """
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE.sub("-", base).strip("-. ")
    if not base or base in (".", ".."):
        return "document"
    return base[:180]


def default_logical_key(filename: str) -> str:
    return f"{ATTACHMENT_PREFIX}/{normalize_filename(filename)}"


def distinct_logical_key(filename: str, content_hash: str) -> str:
    """The ``--new-asset`` key: readable, deterministic and distinct.

    ``System_Requirements_v3.docx`` + hash ``9f2c...`` becomes
    ``documents/System_Requirements_v3--9f2c1a3b.docx``. Deterministic means
    re-running the same import with ``--new-asset`` targets the same Asset
    instead of creating an endless family of near-duplicates.
    """
    name = normalize_filename(filename)
    stem, ext = os.path.splitext(name)
    suffix = (content_hash or "")[:_HASH_PREFIX_CHARS] or "unknown"
    return f"{ATTACHMENT_PREFIX}/{stem}--{suffix}{ext}"


def normalize_logical_key(key: str) -> str:
    """A caller-supplied ``--logical-key``, made safe and portable.

    Traversal components are dropped rather than rejected-with-an-error, because
    the useful reading of ``../spec.md`` is ``spec.md``; what must never happen
    is a stored key that escapes the workspace.
    """
    raw = (key or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in raw.split("/") if p and p not in (".", "..")]
    cleaned = [_UNSAFE.sub("-", p).strip("-. ") or "part" for p in parts]
    return "/".join(cleaned)[:400] or default_logical_key("document")


def plan(workspace_id: str, *, filename: str, content_hash: str,
         requested_asset_id: str = "", requested_logical_key: str = "",
         new_asset: bool = False, source_kind: str = "attachment",
         workspace_relative_key: str = "", repo: Any = None) -> Dict[str, Any]:
    """Decide what importing these bytes should do. Writes nothing.

    Returns ``{status, logical_key, asset_id, revision, existing_asset,
    content_hash, reason, guidance}``. ``guidance`` is populated only for
    ``possible_revision`` — the concrete commands that resolve the ambiguity.
    """
    from .. import db as db_module
    repository = repo if repo is not None else db_module

    # 1. Exact duplicate anywhere in the workspace. Checked FIRST, before any
    #    key resolution: the same bytes under a different name are still the
    #    same document, and re-importing them must not fork history.
    duplicate = repository.find_document_revision_by_content_hash(
        workspace_id, content_hash)
    if duplicate:
        return {
            "status": ImportStatus.DUPLICATE,
            "logical_key": duplicate.get("logical_key", ""),
            "asset_id": duplicate.get("asset_id", ""),
            "revision": {k: duplicate.get(k) for k in
                         ("id", "sequence", "content_hash", "created_at",
                          "version_label")},
            "content_hash": content_hash,
            "reason": ("these exact bytes are already stored as a document "
                       "revision in this workspace"),
        }

    # 2. An explicitly named Asset wins over every key rule.
    if requested_asset_id:
        return _plan_for_asset(repository, workspace_id, requested_asset_id,
                               content_hash)

    # 3. A workspace file keeps the Phase 2 identity rule.
    if workspace_relative_key:
        return _plan_for_key(repository, workspace_id, workspace_relative_key,
                             content_hash, explicit=True)

    # 4. An explicit --logical-key: find or create that Asset.
    if requested_logical_key:
        return _plan_for_key(repository, workspace_id,
                             normalize_logical_key(requested_logical_key),
                             content_hash, explicit=True)

    # 5. --new-asset: a distinct, deterministic, readable key.
    if new_asset:
        return _plan_for_key(repository, workspace_id,
                             distinct_logical_key(filename, content_hash),
                             content_hash, explicit=True)

    # 6. The default key — the only path that can end in `possible_revision`.
    return _plan_for_key(repository, workspace_id, default_logical_key(filename),
                         content_hash, explicit=False, filename=filename)


def _plan_for_asset(repo: Any, workspace_id: str, asset_id: str,
                    content_hash: str) -> Dict[str, Any]:
    asset = repo.get_asset(workspace_id, asset_id)
    if not asset:
        return {"status": ImportStatus.UNSUPPORTED, "logical_key": "",
                "asset_id": asset_id, "content_hash": content_hash,
                "reason": f"no asset {asset_id!r} in this workspace",
                "error": "asset_not_found"}
    if not repo.is_document_asset(workspace_id, asset_id):
        return {"status": ImportStatus.UNSUPPORTED,
                "logical_key": asset.get("logical_key", ""),
                "asset_id": asset_id, "content_hash": content_hash,
                "reason": ("that asset is not a document (its current revision "
                           "has no parse record), so these bytes would not be a "
                           "revision of the same thing"),
                "error": "not_a_document"}
    current = repo.get_revision(workspace_id, asset.get("current_revision_id")) \
        if asset.get("current_revision_id") else None
    if current and current.get("content_hash") == content_hash:
        return {"status": ImportStatus.DUPLICATE,
                "logical_key": asset["logical_key"], "asset_id": asset_id,
                "revision": current, "content_hash": content_hash,
                "reason": "the asset's current revision already has these bytes"}
    return {"status": ImportStatus.REVISION,
            "logical_key": asset["logical_key"], "asset_id": asset_id,
            "existing_asset": asset, "revision": current,
            "content_hash": content_hash,
            "reason": f"the content differs from the current revision of "
                      f"{asset['logical_key']!r}, so it becomes its next revision"}


def _plan_for_key(repo: Any, workspace_id: str, logical_key: str,
                  content_hash: str, *, explicit: bool,
                  filename: str = "") -> Dict[str, Any]:
    asset = repo.find_asset_by_logical_key(workspace_id, logical_key)
    if not asset:
        return {"status": ImportStatus.NEW_ASSET, "logical_key": logical_key,
                "asset_id": "", "content_hash": content_hash,
                "reason": f"no asset uses the key {logical_key!r} yet"}

    current = repo.get_revision(workspace_id, asset.get("current_revision_id")) \
        if asset.get("current_revision_id") else None
    if current and current.get("content_hash") == content_hash:
        return {"status": ImportStatus.DUPLICATE, "logical_key": logical_key,
                "asset_id": asset["id"], "revision": current,
                "content_hash": content_hash,
                "reason": "the asset's current revision already has these bytes"}

    if explicit:
        return {"status": ImportStatus.REVISION, "logical_key": logical_key,
                "asset_id": asset["id"], "existing_asset": asset,
                "revision": current, "content_hash": content_hash,
                "reason": f"the content differs from the current revision of "
                          f"{logical_key!r}, so it becomes its next revision"}

    # The filename collided but the caller never said these are the same
    # document. Refuse to guess: merging two unrelated documents is not
    # recoverable, and the user is one flag away from saying which they meant.
    return {
        "status": ImportStatus.POSSIBLE_REVISION,
        "logical_key": logical_key,
        "asset_id": "",
        "possible_asset": asset,
        "current_revision": current,
        "content_hash": content_hash,
        "existing_content_hash": (current or {}).get("content_hash", ""),
        "reason": (f"a different document is already stored as {logical_key!r}. "
                   f"OpenMind will not merge two documents just because their "
                   f"filenames match, so nothing was written."),
        "guidance": {
            "as_revision": f"--asset {asset['id']}",
            "as_new_document": "--new-asset",
            "explicit_key": "--logical-key documents/<your-key>",
            "suggested_new_key": distinct_logical_key(
                filename or logical_key.rsplit("/", 1)[-1], content_hash),
        },
    }


__all__ = ["ATTACHMENT_PREFIX", "normalize_filename", "default_logical_key",
           "distinct_logical_key", "normalize_logical_key", "plan"]
