"""Capability router — agent-style invocation with deterministic graceful degradation.

A request is dispatched to exactly one capability — **glossary** lookup, **structure**
query, or code **search**. A DETERMINISTIC if-else classifier always decides first
(the always-available floor). When a local model is ready it MAY refine the choice,
but its answer is VALIDATED against the known capability set and accepted only if it
is one of them — an unavailable, slow, or off-spec model silently degrades to the
deterministic rule. The model never invents a capability or overrides the safe
default unchecked.

This is the project's thesis applied to control flow: let the model help where it is
reliable, but keep a deterministic floor so behaviour never depends on the model
being right. See SKILL.md units under ``skills/`` for the capability contracts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import glossary, llm_client, mapio, rag, structure, tokenmatch

CAPABILITIES = ("glossary", "structure", "search")

# Deterministic structure-intent cues (lowercased substring match). Kept explicit
# and auditable — this is the floor the system can never fall below.
_STRUCT_CUES = (
    "call site", "caller", "callee", "who calls", "calls ", "depend", "dependency",
    "import", "graph", "structure", "module", "entry point", "entrypoint", "flow",
    "architecture", "relationship", "references", "uses ", "used by",
)


def _heuristic_route(query: str) -> Dict[str, str]:
    """Pure deterministic classifier — always available, no model required."""
    q = (query or "").lower().strip()
    if not q:
        return {"capability": "search", "reason": "empty query -> default search"}
    if tokenmatch.is_exact_token_query(query) or glossary.looks_like_term_query(query):
        return {"capability": "glossary",
                "reason": "bare identifier / 'what is X' acronym query -> glossary"}
    if any(cue in q for cue in _STRUCT_CUES):
        return {"capability": "structure",
                "reason": "mentions code structure / relationships -> structure map"}
    return {"capability": "search", "reason": "natural-language query -> code search"}


def _model_route(query: str) -> str:
    """Optional model assist: classify into ONE capability word. Returns '' on any
    failure. The caller VALIDATES the result against CAPABILITIES before trusting it."""
    try:
        msgs = [
            {"role": "system", "content":
             "You are a router. Classify the user request into EXACTLY ONE of these "
             "capabilities and reply with ONLY that single lowercase word:\n"
             "- glossary: define a term or acronym\n"
             "- structure: code structure, callers/callees, dependencies, modules, flow\n"
             "- search: find code by concept or identifier"},
            {"role": "user", "content": query},
        ]
        out = llm_client.chat(msgs, max_tokens=4, temperature=0.0, timeout=10.0)
        return (out or "").strip().lower().split()[0] if out else ""
    except Exception:
        return ""


def route(query: str, *, use_model: Optional[bool] = None) -> Dict[str, Any]:
    """Decide which capability handles *query*. ALWAYS returns a valid capability.

    ``use_model=None`` (default) auto-detects model readiness. A model pick is taken
    ONLY if it is one of :data:`CAPABILITIES`; otherwise the deterministic choice
    stands (graceful degradation). The trace records who decided + the floor choice."""
    det = _heuristic_route(query)
    decided, decided_by = det["capability"], "deterministic"
    if use_model is None:
        use_model = llm_client.is_ready()
    if use_model:
        label = _model_route(query)
        decided_by = ("model" if label in CAPABILITIES
                      else "deterministic (model unavailable or off-spec)")
        if label in CAPABILITIES:
            decided = label
    return {
        "capability": decided,
        "decided_by": decided_by,
        "deterministic_fallback": det["capability"],
        "reason": det["reason"],
        "capabilities": list(CAPABILITIES),
    }


def dispatch(project_ids: List[str], query: str,
             *, use_model: Optional[bool] = None) -> Dict[str, Any]:
    """Route, then INVOKE the chosen capability and return its result + the routing
    trace. Every capability is itself deterministic / grounded; the router only
    chooses which one runs."""
    decision = route(query, use_model=use_model)
    cap = decision["capability"]
    if cap == "glossary":
        result: Any = glossary.get_glossary(mapio.merged_glossary(project_ids),
                                            tokenmatch.strip_quotes(query))
    elif cap == "structure":
        result = structure.overview(mapio.merged_structure(project_ids))
    else:
        result = rag.retrieve(project_ids, query)
    return {"routed_to": cap, "routing": decision, "result": result}
