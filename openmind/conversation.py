"""Ask conversation history — the UI-memory layer (per-scope, bounded, durable).

CONCEPTUAL DISTINCTION (do not conflate with :mod:`openmind.cases`):

* This module is UI MEMORY: the recent Ask exchanges retained so the user can
  switch tabs, reload the page, restart the app, and ask follow-ups. It is
  automatic, bounded (last :data:`db.ASK_HISTORY_CAP` per scope), and keyed by
  *scope* (a project id) so histories never cross.

* A SOLVED CASE (openmind/cases.py) is a curated knowledge asset — an exchange the
  user deliberately PROMOTES via the save icon, embedded into the separate cases
  RAG layer to be matched against FUTURE problems.

History is transient-ish; cases are intentional and durable. :func:`case_from_exchange`
is the one bridge: it shapes a saved-case payload out of a retained exchange.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import db

CAP = db.ASK_HISTORY_CAP


def _slim_attachments(atts: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Display-only attachment view (drops the heavy user-supplied text body)."""
    return [{"name": a.get("name"), "kind": a.get("kind"), "status": a.get("status", "")}
            for a in (atts or [])]


def create(scope_id: str, project_id: Optional[str], pids: List[str],
           scope_desc: Dict[str, Any], question: str,
           attachments: Optional[List[Dict[str, Any]]] = None,
           k: int = 12) -> Dict[str, Any]:
    """Create a queued exchange. The full attachment text is retained until the
    answer completes (the worker needs it to ground), then slimmed away."""
    ex_id = db.new_id("ask_")
    payload = {
        "question": question or "",
        "attachments": [{"name": a.get("name"), "kind": a.get("kind"),
                         "status": a.get("status", ""), "text": a.get("text", "")}
                        for a in (attachments or [])],
        "answer": "",
        "meta": None,
        "grounding_used": False,
        "grounding_kinds": [],
        "saved_case_id": None,
        "error": "",
        "pids": pids,
        "k": k,
        "scope_desc": scope_desc,
    }
    return db.ask_create(ex_id, scope_id, project_id, payload, status="queued")


def get(exchange_id: str) -> Optional[Dict[str, Any]]:
    return db.ask_get(exchange_id)


def history(scope_id: str) -> List[Dict[str, Any]]:
    """The retained thread for a scope (oldest-first), attachment text stripped."""
    out: List[Dict[str, Any]] = []
    for ex in db.ask_list(scope_id):
        ex = dict(ex)
        ex["attachments"] = _slim_attachments(ex.get("attachments"))
        out.append(ex)
    return out


def public(ex: Dict[str, Any]) -> Dict[str, Any]:
    """A single exchange shaped for the API (attachment text stripped)."""
    ex = dict(ex)
    ex["attachments"] = _slim_attachments(ex.get("attachments"))
    return ex


def clear(scope_id: str) -> None:
    db.ask_clear(scope_id)


def context_pairs(scope_id: str, exclude_id: Optional[str]) -> List[Tuple[str, str]]:
    """Prior COMPLETED (question, answer) pairs for multi-turn context, oldest-first.

    The worker passes these to build_grounded, which compacts them within a small
    slice of the context budget and re-retrieves fresh sources for the new turn.
    """
    pairs: List[Tuple[str, str]] = []
    for ex in db.ask_list(scope_id):
        if ex.get("id") == exclude_id:
            continue
        if ex.get("status") == "done" and ex.get("question") and ex.get("answer"):
            pairs.append((ex["question"], ex["answer"]))
    return pairs


def case_from_exchange(ex: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a solved-case payload from a retained exchange (provenance).

    Records the question (problem), the answer (resolution), the cited code
    file-refs (cases.save_case stamps their content hashes for staleness), the
    involved topics/services, and the grounding kinds used.
    """
    meta = ex.get("meta") or {}
    sources = meta.get("sources") or []
    raw = meta.get("raw") or {}

    file_refs: List[Dict[str, Any]] = []
    seen = set()
    for s in sources:
        fp = s.get("file_path")
        if fp and fp not in seen:
            seen.add(fp)
            file_refs.append({"file_path": fp, "symbol": s.get("title", "")})

    topics = list(raw.get("touched_topics") or [])
    services = [s.get("name") for s in (raw.get("services") or []) if s.get("name")]
    grounding = ex.get("grounding_kinds") or []
    tags = ["from-ask"]
    if grounding:
        tags.append("grounding:" + ",".join(grounding))

    return {
        "problem_text": ex.get("question") or "(question not recorded)",
        "resolution_summary": ex.get("answer") or "",
        "involved_topics": topics,
        "involved_services": services,
        "file_refs": file_refs,
        "tags": tags,
    }
