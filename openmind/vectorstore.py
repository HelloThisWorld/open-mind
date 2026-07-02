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
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from . import config

_lock = threading.Lock()
_backend: Optional[str] = None
_chroma_client = None


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
    def drop(self): ...


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
        try:
            _chroma_client.delete_collection(self.name)
        except Exception:
            pass
        self._col = _chroma_client.get_or_create_collection(
            name=self.name, metadata={"hnsw:space": "cosine"})

    def drop(self):
        """Delete the collection WITHOUT recreating it (used on project delete —
        recreating an empty collection we're about to discard is wasted work)."""
        try:
            _chroma_client.delete_collection(self.name)
        except Exception:
            pass


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

    def drop(self):
        self._data = {}
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass


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
                try:
                    _chroma_client.delete_collection(name)
                    dropped.append(name)
                except Exception:
                    pass
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
