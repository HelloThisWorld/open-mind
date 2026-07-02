"""Solved Cases — the experience layer (per-project cases collection).

A case records a problem + resolution + the services/topics/files involved.
On search we look at cases FIRST; a close match can short-circuit full RAG.
At recall we re-check each file_ref's hash and flag the case "may be stale" if
the referenced code changed. Cases are isolated per project (scope).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from . import db, embeddings, machine, vectorstore, walker


def _doc_text(case: Dict[str, Any]) -> str:
    parts = [case.get("problem_text", ""), case.get("resolution_summary", "")]
    if case.get("tags"):
        parts.append("tags: " + ", ".join(case["tags"]))
    if case.get("involved_services"):
        parts.append("services: " + ", ".join(case["involved_services"]))
    if case.get("involved_topics"):
        parts.append("topics: " + ", ".join(case["involved_topics"]))
    return "\n".join(p for p in parts if p)


def save_case(project_id: str, case: Dict[str, Any]) -> Dict[str, Any]:
    case = dict(case)
    case_id = case.get("id") or db.new_id("case_")
    case["id"] = case_id
    case["created_at"] = case.get("created_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime())
    # stamp current file hashes for staleness tracking
    refs = []
    for ref in case.get("file_refs", []):
        ref = dict(ref)
        if not ref.get("file_hash_at_save") and ref.get("file_path"):
            # file_path is stored relative; re-anchor to read it off this machine
            ref["file_hash_at_save"] = walker.hash_file(
                machine.from_rel(project_id, ref["file_path"]))
        refs.append(ref)
    case["file_refs"] = refs

    store = vectorstore.get_cases_store(project_id)
    doc = _doc_text(case)
    vec = embeddings.embed([doc])
    meta = {
        "project_id": project_id,
        "problem_text": case.get("problem_text", ""),
        "resolution_summary": case.get("resolution_summary", ""),
        "services_csv": ",".join(case.get("involved_services", [])),
        "topics_csv": ",".join(case.get("involved_topics", [])),
        "tags_csv": ",".join(case.get("tags", [])),
        "file_refs_json": json.dumps(refs),
        "created_at": case["created_at"],
        "stale": False,
    }
    store.upsert(ids=[case_id], embeddings=vec, documents=[doc], metadatas=[meta])
    return case


def _reconstruct(cid: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    try:
        refs = json.loads(meta.get("file_refs_json", "[]"))
    except Exception:
        refs = []
    return {
        "id": cid,
        "problem_text": meta.get("problem_text", ""),
        "resolution_summary": meta.get("resolution_summary", ""),
        "involved_services": [s for s in (meta.get("services_csv") or "").split(",") if s],
        "involved_topics": [t for t in (meta.get("topics_csv") or "").split(",") if t],
        "tags": [t for t in (meta.get("tags_csv") or "").split(",") if t],
        "file_refs": refs,
        "created_at": meta.get("created_at", ""),
        "project_id": meta.get("project_id", ""),
        # a persisted flag (e.g. set by mark_all_stale after a terminate wipe);
        # honored by _check_staleness so it isn't silently recomputed away.
        "persisted_stale": bool(meta.get("stale")),
    }


def _check_staleness(case: Dict[str, Any]) -> Dict[str, Any]:
    stale = False
    changed = []
    pid = case.get("project_id", "")
    for ref in case.get("file_refs", []):
        fp = ref.get("file_path")
        if not fp:
            continue
        rel = machine.to_rel(pid, fp)         # display: relative only (legacy bridge)
        ref["file_path"] = rel
        # '' if the file was deleted/unreadable; re-anchor the relative path to disk
        current = walker.hash_file(machine.from_rel(pid, rel))
        ref["current_hash"] = current
        # changed if the live content differs from save time — including the file
        # having been DELETED (current=='' != the saved hash).
        ref["changed"] = bool(ref.get("file_hash_at_save")) and current != ref["file_hash_at_save"]
        if ref["changed"]:
            stale = True
            changed.append(rel)
    case["stale"] = stale or bool(case.get("persisted_stale"))
    case["stale_files"] = changed
    return case


def search_cases(project_ids: List[str], query: str, k: int = 5) -> List[Dict[str, Any]]:
    qvec = embeddings.embed([query])[0].tolist()
    hits: List[Dict[str, Any]] = []
    for pid in project_ids:
        store = vectorstore.get_cases_store(pid)
        res = store.query(qvec, n_results=k)
        for cid, meta, dist in zip(res["ids"], res["metadatas"], res["distances"]):
            case = _reconstruct(cid, meta)
            case = _check_staleness(case)
            case["distance"] = dist
            case["similarity"] = round(1.0 - dist, 4)
            hits.append(case)
    hits.sort(key=lambda c: c["distance"])
    return hits[:k]


def list_cases(project_ids: List[str], query: Optional[str] = None,
               k: int = 50) -> List[Dict[str, Any]]:
    if query:
        return search_cases(project_ids, query, k=k)
    out: List[Dict[str, Any]] = []
    for pid in project_ids:
        store = vectorstore.get_cases_store(pid)
        res = store.get()
        for cid, meta in zip(res["ids"], res["metadatas"]):
            out.append(_check_staleness(_reconstruct(cid, meta)))
    out.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return out


def clear_cases(project_id: str) -> None:
    vectorstore.get_cases_store(project_id).reset()


def mark_all_stale(project_id: str) -> None:
    """Flag all cases stale (e.g. after a terminate wipe of code)."""
    store = vectorstore.get_cases_store(project_id)
    res = store.get()
    ids, metas = [], []
    for cid, meta in zip(res["ids"], res["metadatas"]):
        meta = dict(meta)
        meta["stale"] = True
        ids.append(cid)
        metas.append(meta)
    store.update_metadatas(ids=ids, metadatas=metas)
