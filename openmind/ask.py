"""Grounded Ask: assemble a budgeted, source-cited prompt from retrieved code +
the deterministic glossary + solved cases + USER-SUPPLIED attachments, for the
local model to answer (streamed). Plus best-effort local OCR for image attachments.

The local model is NOT vision-capable: images are OCR'd to text (or kept as a
reference) and fed as user-supplied context — the raw image is never sent.
Attached text counts against the SAME context budget as retrieved chunks.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

from . import db

GROUNDED_SYS = (
    "You are Open Mind's grounded assistant for a code knowledge base. Answer "
    "ONLY from the provided SOURCES (retrieved code, deterministic glossary "
    "definitions, solved cases) and the USER-SUPPLIED attachments. "
    "Never invent files, symbols, definitions, or APIs. Cite sources inline "
    "as [n] using the numbered SOURCES. Clearly distinguish facts taken from "
    "USER-SUPPLIED attachments from those in retrieved code. If the SOURCES do "
    "not contain the answer, say so plainly and state what is missing. Be concise."
)

CHARS_PER_TOKEN = 3.2
ANSWER_TOKENS = 1024
MAX_CONTEXT_CHARS = 12000          # cap prefill so the small/slow model stays responsive
                                   # (well within ctx; reserves room for retrieval + answer)


def _budget() -> Tuple[int, int]:
    cfg = db.get_model_config()
    ctx = int(cfg.get("ctx_size", 32768) or 32768)
    total = int(ctx * CHARS_PER_TOKEN)
    reserve = int(ANSWER_TOKENS * CHARS_PER_TOKEN) + 2500   # answer + system/question
    budget = min(max(3000, total - reserve), MAX_CONTEXT_CHARS)
    return budget, int(budget * 0.45)                       # (total, attachment cap)


def _glossary_grounded(question: str, token: str, hit: Dict[str, Any]) -> Dict[str, Any]:
    """Build a grounded result from a SINGLE glossary fact (no retrieval).

    The model is handed exactly one source — the deterministic definition + its
    provenance, or the deterministic 'not found' — so a term/acronym query can
    never be answered by similarity-retrieved noise or a guessed expansion.
    """
    if hit.get("found"):
        definition = hit.get("definition", "")
        body = f"{hit['term']}: {definition}"
        loc = f"{hit.get('source_file', '?')}:{hit.get('line_number', '?')}"
        src = {"n": 1, "kind": "glossary",
               "title": f"glossary: {hit['term']} — {definition}",
               "subtitle": f"{loc} (verbatim; deterministic map; source hash "
                           f"{str(hit.get('content_hash', ''))[:8]})",
               "body": body, "file_path": hit.get("source_file")}
        block = ("[1] GLOSSARY DEFINITION (deterministic exact-token map lookup, "
                 "VERBATIM from the source, NOT similarity retrieval and NOT "
                 f"model-generated):\n{body}\nProvenance: {loc}")
        instruction = ("Answer using ONLY source [1] and cite it as [1]. State the "
                       "definition verbatim and its provenance; add nothing the "
                       "source omits.")
        route = "hit"
    else:
        body = f"The term '{token}' has no authoritative definition in the indexed project."
        src = {"n": 1, "kind": "glossary", "title": f"glossary: {token} (not found)",
               "subtitle": "deterministic map lookup — no entry", "body": body}
        block = (f"[1] GLOSSARY LOOKUP (deterministic):\n{body} Do NOT guess or "
                 "invent a definition.")
        instruction = ("Tell the user no authoritative definition for this term was "
                       "found in the indexed project. Do NOT guess a definition.")
        route = "miss"
    user_content = "# SOURCES\n" + block + f"\n\n# QUESTION\n{question}\n\n" + instruction
    return {
        "messages": [{"role": "system", "content": GROUNDED_SYS},
                     {"role": "user", "content": user_content}],
        "sources": [src],
        "raw": {"glossary": hit, "code_chunks": [], "case_hits": [],
                "query_mode": "glossary"},
        "meta": {"context_chars": len(block), "budget_chars": 0,
                 "attachment_chars": 0, "source_count": 1, "history_turns": 0,
                 "within_budget": True, "glossary_route": route},
    }


def build_grounded(pids: List[str], question: str,
                   attachments: List[Dict[str, Any]], k: int = 12,
                   history: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
    from . import rag, cases as casemod, mapio, glossary
    budget, att_cap = _budget()

    # ---- GLOSSARY-FIRST ROUTING (deterministic term/acronym resolution) ----
    # An acronym/term question is answered from the persisted glossary MAP, never
    # by similarity retrieval: resolve the token exactly, then hand the model a
    # SINGLE fact (definition + provenance) — or, for a real definition question
    # with no entry, the deterministic "not found" (never a guessed expansion).
    term_q = glossary.looks_like_term_query(question)
    if term_q:
        token, explicit = term_q
        hit = glossary.get_glossary(mapio.merged_glossary(pids), token)
        if hit.get("found") or explicit:
            return _glossary_grounded(question, token, hit)

    sources: List[Dict[str, Any]] = []
    blocks: List[str] = []
    used = 0
    n = 0

    # 0) prior conversation turns (multi-turn context), compacted within a small
    # slice of the SAME budget. Most-recent-first; not citable, so not a source.
    hist_block = ""
    hist_turns = 0
    if history:
        hist_cap = min(2000, int(budget * 0.2))
        used_h = 0
        pieces: List[str] = []
        for q, a in reversed(history):
            seg = f"Q: {(q or '').strip()[:300]}\nA: {(a or '').strip()[:500]}"
            if used_h + len(seg) > hist_cap:
                break
            used_h += len(seg)
            pieces.append(seg)
        if pieces:
            hist_turns = len(pieces)
            hist_block = ("# CONVERSATION SO FAR (most recent first; for context only — "
                          "cite SOURCES, not this)\n" + "\n\n".join(pieces) + "\n\n")
            used += used_h

    # 1) USER-SUPPLIED attachments first (highest-priority context, labeled distinctly)
    att_used = 0
    for a in (attachments or []):
        name = a.get("name", "attachment")
        kind = a.get("kind", "file")
        status = a.get("status", "")
        text = (a.get("text") or "").strip()
        n += 1
        if not text:
            sources.append({"n": n, "kind": "user",
                            "title": f"attachment: {name} (user-supplied {kind})",
                            "subtitle": status or "no text extracted — kept as reference",
                            "body": ""})
            continue
        room = max(0, att_cap - att_used)
        mark = "\n…[truncated to fit context budget]"
        if len(text) > room:
            snippet = text[:max(0, room - len(mark))] + mark
            truncated = True
        else:
            snippet, truncated = text, False
        att_used += len(snippet)
        used += len(snippet)
        sources.append({"n": n, "kind": "user",
                        "title": f"attachment: {name} (user-supplied)",
                        "subtitle": status or kind, "body": snippet,
                        "truncated": truncated})
        blocks.append(f"[{n}] USER-SUPPLIED ATTACHMENT — {name}:\n{snippet}")

    # retrieval + cases
    result = rag.retrieve(pids, question, k=k)
    case_hits = casemod.search_cases(pids, question, k=3)

    # 2) solved cases
    for c in case_hits[:3]:
        block = f"problem: {c['problem_text']}\nresolution: {c['resolution_summary']}"
        if used + len(block) > budget:
            break
        n += 1
        used += len(block)
        sources.append({"n": n, "kind": "case",
                        "title": f"solved case (sim {c.get('similarity')})"
                                 + (" — may be stale" if c.get("stale") else ""),
                        "subtitle": ", ".join(c.get("involved_topics", [])),
                        "body": block})
        blocks.append(f"[{n}] SOLVED CASE:\n{block}")

    # 3) retrieved code chunks
    for ch in result["code_chunks"]:
        room = budget - used
        if room < 400:
            break
        code = ch["code"]
        if len(code) > room:
            snippet = code[:max(0, room - 13)] + "\n…[truncated]"
        else:
            snippet = code
        used += len(snippet)
        n += 1
        sources.append({"n": n, "kind": "code",
                        "title": f"{ch['service']} · {ch['symbol']}",
                        "subtitle": f"{ch['file_path']} : {ch['line_range']}",
                        "body": snippet, "file_path": ch["file_path"]})
        blocks.append(f"[{n}] RETRIEVED CODE — {ch['service']} {ch['symbol']} "
                      f"({ch['file_path']}:{ch['line_range']}):\n{snippet}")

    user_content = (
        hist_block
        + "# SOURCES\n" + ("\n\n".join(blocks) if blocks else "(no sources retrieved)")
        + f"\n\n# QUESTION\n{question}\n\n"
        "Answer using ONLY the SOURCES above and cite as [n]. Mark facts from "
        "USER-SUPPLIED attachments distinctly. If the answer is not in the sources, say so."
    )
    messages = [{"role": "system", "content": GROUNDED_SYS},
                {"role": "user", "content": user_content}]
    raw = {
        "code_chunks": result["code_chunks"],
        "case_hits": case_hits,
        "query_mode": result.get("query_mode"),
    }
    meta = {"context_chars": used, "budget_chars": budget,
            "attachment_chars": att_used, "source_count": len(sources),
            "history_turns": hist_turns, "within_budget": used <= budget}
    return {"messages": messages, "sources": sources, "raw": raw, "meta": meta}


# ---------------------------------------------------------------------------
# Image OCR (local, best-effort)
# ---------------------------------------------------------------------------
def ocr_image(data: bytes, name: str = "image") -> Dict[str, Any]:
    """Extract text from an image via local tesseract. Degrades gracefully to a
    'reference' when tesseract isn't installed or finds no text. Never sends the
    raw image to the model."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return {"available": False, "text": "", "chars": 0,
                "status": "image attached; OCR library unavailable — kept as reference"}
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return {"available": False, "text": "", "chars": 0,
                "status": "image attached; tesseract not installed — kept as reference"}
    try:
        img = Image.open(io.BytesIO(data))
        text = (pytesseract.image_to_string(img) or "").strip()
    except Exception as exc:
        return {"available": False, "text": "", "chars": 0,
                "status": f"image attached; OCR failed ({exc}) — kept as reference"}
    if text:
        return {"available": True, "text": text, "chars": len(text),
                "status": f"image attached; text extracted ({len(text)} chars)"}
    return {"available": True, "text": "", "chars": 0,
            "status": "image attached; no text found — kept as reference"}
