"""Vector store abstraction. ChromaDB (local persistent) when available, with a
pure-numpy persistent fallback so the app always runs.

Per project: ONE code collection (``code_<pid>``) + ONE cases collection
(``cases_<pid>``). Collections are keyed by project id — physical isolation
(Invariant 9). Embeddings are computed by :mod:`embeddings` (local CPU)
and passed in; the store never computes embeddings itself (so no model
download path inside Chroma, and no egress).
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from . import config

_lock = threading.Lock()
_backend: Optional[str] = None
_chroma_client = None

# Rows drained per delete batch while dropping a collection (see
# _drop_chroma_collection): ~0.5s of GIL hold per batch measured on a real
# 2 GB store, small enough that the app stays responsive throughout.
DROP_BATCH = 500


def _detect_backend() -> str:
    global _backend, _chroma_client
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        try:
            import chromadb
            from chromadb.config import Settings
            config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _chroma_client = chromadb.PersistentClient(
                path=str(config.CHROMA_DIR),
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )
            _backend = "chroma"
        except Exception as exc:
            import sys
            print(f"[vectorstore] chromadb unavailable ({exc!r}); using numpy fallback.",
                  file=sys.stderr)
            _backend = "numpy"
        return _backend


def backend_name() -> str:
    return _detect_backend()


def _drop_chroma_collection(name: str, cancel: Optional[Callable[[], bool]] = None) -> bool:
    """Delete a chroma collection WITHOUT freezing the app.

    The chromadb 1.x rust bindings hold the GIL for the whole duration of a
    call, so a single ``delete_collection`` on a learned project (measured:
    32s for a 35k-chunk collection on a 2 GB store) freezes every Python
    thread — all HTTP requests, the SSE streams, the job worker AND SIGINT
    delivery, so Ctrl+C appears dead. Worse, the store's SQLite runs in
    rollback-journal mode: that one giant transaction rolls back entirely if
    the process is killed mid-way, so an impatient kill means the delete
    NEVER completes across restarts.

    Instead, drain the collection in small id batches — each call holds the
    GIL for only ~0.5s and each batch commits — then drop the empty
    collection (cheap). A kill or shutdown mid-drain loses at most one batch;
    the caller's 'deleting' tombstone re-spawns the cleanup on the next start
    and the drain RESUMES where it stopped.

    Returns True once the collection is gone (or never existed), False if
    *cancel* stopped the drain early or a batch failed (caller retries later).
    """
    try:
        col = _chroma_client.get_collection(name)
    except Exception:
        return True   # no such collection — nothing to drop
    try:
        total = col.count()
        drained = 0
        while True:
            if cancel is not None and cancel():
                print(f"[vectorstore] drop of {name} paused at "
                      f"{drained}/{total} (shutdown); resumes on next start.",
                      flush=True)
                return False
            ids = col.get(limit=DROP_BATCH, include=[]).get("ids") or []
            if not ids:
                break
            col.delete(ids=ids)
            drained += len(ids)
            if drained % 10_000 < DROP_BATCH:
                print(f"[vectorstore] dropping {name}: {drained}/{total}", flush=True)
            time.sleep(0.01)   # deliberate GIL gap: let requests + signals run
        _chroma_client.delete_collection(name)
        return True
    except Exception as exc:
        print(f"[vectorstore] drop of {name} interrupted: {exc!r} "
              f"(cleanup will retry on the next start)", flush=True)
        return False


def drop_collection(collection_name: str,
                    cancel: Optional[Callable[[], bool]] = None) -> bool:
    """Backend-dispatching collection drop (no store instantiation, so the
    delete path never get_or_CREATEs a collection just to remove it).
    Returns False only when the drop was interrupted and should be retried."""
    if _detect_backend() == "chroma":
        return _drop_chroma_collection(collection_name, cancel)
    NumpyStore(collection_name).drop()
    return True


def sweep_orphan_segment_dirs() -> List[str]:
    """Remove HNSW segment directories that no live segment references.

    chromadb 1.x does not delete the on-disk vector-segment directory of the
    legacy persistent layout when a collection is dropped, so every project
    delete used to leak its whole HNSW index on disk. Read the live segment
    ids from chroma.sqlite3 and remove every UUID directory nothing references.

    MUST run BEFORE the chroma client exists in this process (the app calls it
    first thing in lifespan startup, before the job worker / warm-up threads).
    A raw sqlite read concurrent with a rust-binding write DEADLOCKS the whole
    process: the rust call holds the GIL while waiting for our reader's SHARED
    lock, and our reader holds the SHARED lock while waiting for the GIL. The
    _chroma_client guard makes a late call a safe no-op instead of that."""
    removed: List[str] = []
    if _chroma_client is not None:
        print("[vectorstore] segment-dir sweep skipped: chroma client already "
              "open (sweep must run before first use).", flush=True)
        return removed
    db_path = config.CHROMA_DIR / "chroma.sqlite3"
    if not db_path.exists():
        return removed
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                              uri=True, timeout=5)
        try:
            live = {r[0] for r in con.execute("SELECT id FROM segments")}
        finally:
            con.close()
    except Exception as exc:
        # e.g. a hot journal from a mid-delete kill (read-only can't roll it
        # back). The client rolls it back when it opens; next boot sweeps.
        print(f"[vectorstore] segment-dir sweep skipped: {exc!r}", flush=True)
        return removed
    uuid_pat = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    cutoff = time.time() - 300
    for d in config.CHROMA_DIR.iterdir():
        try:
            if not (d.is_dir() and uuid_pat.match(d.name) and d.name not in live):
                continue
            if d.stat().st_mtime > cutoff:   # too fresh -> might be mid-create
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed.append(d.name)
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Common interface
# ---------------------------------------------------------------------------
class _Store:
    def add(self, ids, embeddings, documents, metadatas): ...
    def upsert(self, ids, embeddings, documents, metadatas): ...
    def update_metadatas(self, ids, metadatas): ...
    def delete(self, ids=None, where=None): ...
    def get(self, where=None, ids=None) -> Dict[str, Any]: ...
    def query(self, query_embedding, n_results=10, where=None) -> Dict[str, Any]: ...
    def count(self) -> int: ...
    def reset(self): ...
    def drop(self, cancel=None): ...


# ---------------------------------------------------------------------------
# Chroma backend
# ---------------------------------------------------------------------------
class ChromaStore(_Store):
    def __init__(self, name: str) -> None:
        self.name = name
        self._col = _chroma_client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"})

    def add(self, ids, embeddings, documents, metadatas):
        if not ids:
            return
        self._col.add(ids=ids, embeddings=_aslist(embeddings),
                      documents=documents, metadatas=_clean_metas(metadatas))

    def upsert(self, ids, embeddings, documents, metadatas):
        if not ids:
            return
        self._col.upsert(ids=ids, embeddings=_aslist(embeddings),
                         documents=documents, metadatas=_clean_metas(metadatas))

    def update_metadatas(self, ids, metadatas):
        """Metadata-only update — stored embeddings/documents are untouched, so
        no re-embedding cost is paid for flag flips like ``stale``."""
        if not ids:
            return
        self._col.update(ids=ids, metadatas=_clean_metas(metadatas))

    def delete(self, ids=None, where=None):
        if ids:
            self._col.delete(ids=ids)
        elif where:
            self._col.delete(where=where)

    def get(self, where=None, ids=None, where_document=None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        if ids:
            kwargs["ids"] = ids
        if where_document:
            kwargs["where_document"] = where_document
        res = self._col.get(**kwargs)
        return {
            "ids": res.get("ids", []),
            "documents": res.get("documents", []),
            "metadatas": res.get("metadatas", []),
        }

    def query(self, query_embedding, n_results=10, where=None) -> Dict[str, Any]:
        if self.count() == 0:
            return {"ids": [], "documents": [], "metadatas": [], "distances": []}
        kwargs: Dict[str, Any] = {
            "query_embeddings": [list(query_embedding)],
            "n_results": min(n_results, max(1, self.count())),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        res = self._col.query(**kwargs)
        return {
            "ids": res.get("ids", [[]])[0],
            "documents": res.get("documents", [[]])[0],
            "metadatas": res.get("metadatas", [[]])[0],
            "distances": res.get("distances", [[]])[0],
        }

    def count(self) -> int:
        return self._col.count()

    def reset(self):
        _drop_chroma_collection(self.name)
        self._col = _chroma_client.get_or_create_collection(
            name=self.name, metadata={"hnsw:space": "cosine"})

    def drop(self, cancel=None):
        """Delete the collection WITHOUT recreating it (used on project delete /
        terminate). Drains in short batches so the app never freezes — see
        _drop_chroma_collection. Returns False if *cancel* interrupted it."""
        return _drop_chroma_collection(self.name, cancel)


# ---------------------------------------------------------------------------
# Numpy fallback backend
# ---------------------------------------------------------------------------
class NumpyStore(_Store):
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = config.CHROMA_DIR / "np" / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, ids, embeddings, documents, metadatas):
        self.upsert(ids, embeddings, documents, metadatas)

    def upsert(self, ids, embeddings, documents, metadatas):
        embs = _aslist(embeddings)
        for i, _id in enumerate(ids):
            self._data[_id] = {
                "embedding": list(embs[i]),
                "document": documents[i],
                "metadata": _clean_metas([metadatas[i]])[0],
            }
        self._save()

    def update_metadatas(self, ids, metadatas):
        for i, _id in enumerate(ids):
            if _id in self._data:
                self._data[_id]["metadata"] = _clean_metas([metadatas[i]])[0]
        self._save()

    def delete(self, ids=None, where=None):
        if ids:
            for _id in ids:
                self._data.pop(_id, None)
        elif where:
            doomed = [k for k, v in self._data.items() if _match(v["metadata"], where)]
            for k in doomed:
                self._data.pop(k, None)
        self._save()

    def get(self, where=None, ids=None, where_document=None) -> Dict[str, Any]:
        out_ids, docs, metas = [], [], []
        items = ((k, self._data[k]) for k in (ids or self._data.keys()) if k in self._data)
        for k, v in items:
            if where and not _match(v["metadata"], where):
                continue
            if where_document and not _match_document(v["document"], where_document):
                continue
            out_ids.append(k)
            docs.append(v["document"])
            metas.append(v["metadata"])
        return {"ids": out_ids, "documents": docs, "metadatas": metas}

    def query(self, query_embedding, n_results=10, where=None) -> Dict[str, Any]:
        q = np.asarray(query_embedding, dtype=np.float32)
        qn = q / (np.linalg.norm(q) or 1.0)
        scored = []
        for k, v in self._data.items():
            if where and not _match(v["metadata"], where):
                continue
            e = np.asarray(v["embedding"], dtype=np.float32)
            en = e / (np.linalg.norm(e) or 1.0)
            sim = float(np.dot(qn, en))
            scored.append((1.0 - sim, k, v))
        scored.sort(key=lambda x: x[0])
        scored = scored[:n_results]
        return {
            "ids": [s[1] for s in scored],
            "documents": [s[2]["document"] for s in scored],
            "metadatas": [s[2]["metadata"] for s in scored],
            "distances": [s[0] for s in scored],
        }

    def count(self) -> int:
        return len(self._data)

    def reset(self):
        self._data = {}
        self._save()

    def drop(self, cancel=None):
        self._data = {}
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass
        return True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _aslist(embeddings) -> List[List[float]]:
    if isinstance(embeddings, np.ndarray):
        return embeddings.tolist()
    return [list(e) for e in embeddings]


def _clean_metas(metadatas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chroma metadata values must be scalar (str/int/float/bool). Coerce."""
    out = []
    for m in metadatas:
        cm = {}
        for k, v in (m or {}).items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                cm[k] = v
            elif isinstance(v, (list, tuple)):
                cm[k] = ",".join(str(x) for x in v)
            else:
                cm[k] = str(v)
        out.append(cm)
    return out


def _match_document(doc: str, where_document: Dict[str, Any]) -> bool:
    """Minimal where_document evaluator ($contains / $not_contains)."""
    if not where_document:
        return True
    low = (doc or "").lower()
    for op, val in where_document.items():
        if op == "$contains" and str(val).lower() not in low:
            return False
        if op == "$not_contains" and str(val).lower() in low:
            return False
        if op == "$and":
            if not all(_match_document(doc, c) for c in val):
                return False
        if op == "$or":
            if not any(_match_document(doc, c) for c in val):
                return False
    return True


def _match(meta: Dict[str, Any], where: Dict[str, Any]) -> bool:
    """Minimal where-clause evaluator for the numpy fallback."""
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(_match(meta, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_match(meta, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            for op, val in cond.items():
                mv = meta.get(key)
                if op == "$eq" and mv != val:
                    return False
                if op == "$ne" and mv == val:
                    return False
                if op == "$in" and mv not in val:
                    return False
                if op == "$nin" and mv in val:
                    return False
        else:
            if meta.get(key) != cond:
                return False
    return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_store(collection_name: str) -> _Store:
    if _detect_backend() == "chroma":
        return ChromaStore(collection_name)
    return NumpyStore(collection_name)


def code_collection_name(project_id: str) -> str:
    return f"code_{project_id}"


def cases_collection_name(project_id: str) -> str:
    return f"cases_{project_id}"


def get_code_store(project_id: str) -> _Store:
    return get_store(code_collection_name(project_id))


def get_cases_store(project_id: str) -> _Store:
    return get_store(cases_collection_name(project_id))


def _pid_of(collection_name: str) -> Optional[str]:
    for prefix in ("code_", "cases_"):
        if collection_name.startswith(prefix):
            return collection_name[len(prefix):]
    return None


def drop_orphan_collections(known_project_ids) -> List[str]:
    """Delete ``code_<pid>`` / ``cases_<pid>`` collections whose project id is no
    longer in *known_project_ids* — storage leaked by older delete paths that
    reset-and-recreated collections instead of dropping them, and by interrupted
    deletes. Best-effort; returns the names dropped. Call at startup (no concurrent
    project creation) so a half-created project's collection is never mistaken for
    an orphan."""
    known = set(known_project_ids)
    dropped: List[str] = []
    if _detect_backend() == "chroma":
        try:
            names = [c.name for c in _chroma_client.list_collections()]
        except Exception:
            return dropped
        for name in names:
            pid = _pid_of(name)
            if pid and pid not in known:
                # drained drop (never a one-shot delete_collection): an orphan
                # can be a full-size learned collection, and this runs at
                # startup where a GIL-held freeze would look like a hung boot.
                if _drop_chroma_collection(name):
                    dropped.append(name)
    else:
        npdir = config.CHROMA_DIR / "np"
        if npdir.exists():
            for f in npdir.glob("*.json"):
                pid = _pid_of(f.stem)
                if pid and pid not in known:
                    try:
                        f.unlink()
                        dropped.append(f.stem)
                    except Exception:
                        pass
    return dropped
