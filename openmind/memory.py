"""Accumulating memory — the experience layer (the headline feature).

A memory is a VERIFIED question/answer pair the user chose to keep. On a new
question we search memory FIRST: a close prior answer is recalled fast and
consistently, so the assistant demonstrably sharpens the more it is used.

Each memory may cite the code files its answer was grounded in; at recall we
re-check each file's content hash and flag the memory "may be stale" if that
code has since changed — so a remembered answer never silently drifts out of
date. Memory follows the same per-project / group scope as everything else.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from . import db, embeddings, vectorstore, walker


def _doc_text(item: Dict[str, Any]) -> str:
    parts = [item.get("question", ""), item.get("answer", "")]
    if item.get("tags"):
        parts.append("tags: " + ", ".join(item["tags"]))
    return "\n".join(p for p in parts if p)


def save_memory(project_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Embed and persist a verified Q&A pair into the project's memory store."""
    item = dict(item)
    mem_id = item.get("id") or db.new_id("mem_")
    item["id"] = mem_id
    item["created_at"] = item.get("created_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime())
    refs = []
    for ref in item.get("file_refs", []):
        ref = dict(ref)
        if not ref.get("file_hash_at_save") and ref.get("file_path"):
            ref["file_hash_at_save"] = walker.hash_file(ref["file_path"])
        refs.append(ref)
    item["file_refs"] = refs

    store = vectorstore.get_memory_store(project_id)
    doc = _doc_text(item)
    meta = {
        "project_id": project_id,
        "question": item.get("question", ""),
        "answer": item.get("answer", ""),
        "tags_csv": ",".join(item.get("tags", [])),
        "file_refs_json": json.dumps(refs),
        "created_at": item["created_at"],
        "stale": False,
    }
    store.upsert(ids=[mem_id], embeddings=embeddings.embed([doc]),
                 documents=[doc], metadatas=[meta])
    return item


def _reconstruct(mid: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    try:
        refs = json.loads(meta.get("file_refs_json", "[]"))
    except Exception:
        refs = []
    return {
        "id": mid,
        "question": meta.get("question", ""),
        "answer": meta.get("answer", ""),
        "tags": [t for t in (meta.get("tags_csv") or "").split(",") if t],
        "file_refs": refs,
        "created_at": meta.get("created_at", ""),
        "project_id": meta.get("project_id", ""),
        "persisted_stale": bool(meta.get("stale")),
    }


def _check_staleness(item: Dict[str, Any]) -> Dict[str, Any]:
    stale = False
    changed = []
    for ref in item.get("file_refs", []):
        fp = ref.get("file_path")
        if not fp:
            continue
        current = walker.hash_file(fp)        # '' if the file was deleted/unreadable
        ref["current_hash"] = current
        ref["changed"] = bool(ref.get("file_hash_at_save")) and current != ref["file_hash_at_save"]
        if ref["changed"]:
            stale = True
            changed.append(fp)
    item["stale"] = stale or bool(item.get("persisted_stale"))
    item["stale_files"] = changed
    return item


def search_memory(project_ids: List[str], query: str, k: int = 5) -> List[Dict[str, Any]]:
    qvec = embeddings.embed([query])[0].tolist()
    hits: List[Dict[str, Any]] = []
    for pid in project_ids:
        store = vectorstore.get_memory_store(pid)
        res = store.query(qvec, n_results=k)
        for mid, meta, dist in zip(res["ids"], res["metadatas"], res["distances"]):
            item = _check_staleness(_reconstruct(mid, meta))
            item["distance"] = dist
            item["similarity"] = round(1.0 - dist, 4)
            hits.append(item)
    hits.sort(key=lambda c: c["distance"])
    return hits[:k]


def list_memory(project_ids: List[str], query: Optional[str] = None,
                k: int = 50) -> List[Dict[str, Any]]:
    if query:
        return search_memory(project_ids, query, k=k)
    out: List[Dict[str, Any]] = []
    for pid in project_ids:
        res = vectorstore.get_memory_store(pid).get()
        for mid, meta in zip(res["ids"], res["metadatas"]):
            out.append(_check_staleness(_reconstruct(mid, meta)))
    out.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return out


def clear_memory(project_id: str) -> None:
    vectorstore.get_memory_store(project_id).reset()


def mark_all_stale(project_id: str) -> None:
    """Flag all memories stale (e.g. after a terminate wipe of the indexed code)."""
    store = vectorstore.get_memory_store(project_id)
    res = store.get()
    for mid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
        meta = dict(meta)
        meta["stale"] = True
        store.upsert(ids=[mid], embeddings=embeddings.embed([doc]),
                     documents=[doc], metadatas=[meta])
