"""MCP server wrapping the Open Mind knowledge layer (stdio transport).

Exposes the read/query surface a client (your editor, agent, or the CLI) needs —
all in-process, all local:
  search, route, dispatch, get_glossary, find_similar_cases, save_case, get_doc,
  propose_fix, apply_fix.

Run:  python -m openmind.mcp_server
      openmind mcp serve            (identical; the CLI calls straight into here)
(register either command as an MCP stdio server in your client).

CONSTRUCTION
------------
The tools are plain module-level functions collected in :data:`TOOLS`, and
:func:`create_mcp_server` registers them on a fresh ``FastMCP``. That means a
test can build a server and inspect its tool set without the import of this
module having already opened a database — the import-time ``db.init_db()`` this
module used to run is now the runtime's job, done once per process by the shared
bootstrap.

The module-level ``mcp`` object is still available and is created on first
attribute access, so anything that referenced it keeps working.

The tools themselves are deliberately NOT routed through the application
services: they are pure, deterministic queries over the glossary, structure,
cases and RAG modules. Wrapping a one-line query module call in a service method
would add indirection without adding a seam.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from . import cases, codemod, docs as docsmod, glossary, machine, mapio, rag, router, scope

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is required: pip install mcp\n" + str(exc))

SERVER_NAME = "open-mind"


def _pids(scope_id: str) -> List[str]:
    pids = scope.resolve(scope_id)
    if not pids:
        raise ValueError(f"unknown or empty scope: {scope_id!r}")
    return pids


def _anchor(file_path: str, scope_id: Optional[str]) -> str:
    """Re-anchor a project-relative path (as returned by search / get_glossary) to
    an absolute path on THIS machine, using the project's machine-local source
    root. Tolerant: an already-absolute path, or a missing scope, is returned
    unchanged."""
    if scope_id and file_path:
        for pid in scope.resolve(scope_id):
            cand = machine.from_rel(pid, file_path)
            if os.path.isfile(cand):
                return cand
    return file_path


# ---------------------------------------------------------------------------
# Tools — argument and response contracts are STABLE; external clients depend
# on them. Changing a name or a returned key is a breaking change.
# ---------------------------------------------------------------------------
def search(scope: str, query: str, k: int = 12, case_sensitive: bool = True,
           subword: bool = False) -> Dict[str, Any]:
    """Hybrid code search (cases-first, then RAG). scope = project id.

    A bare identifier/literal query is matched as an EXACT token (token-boundary;
    never a substring/prefix/suffix of a different token, never embedding-conflated);
    a natural-language query uses the vector+lexical hybrid. Set subword=True to
    also match camelCase/snake_case components; case_sensitive=False to ignore case."""
    pids = _pids(scope)
    case_hits = cases.search_cases(pids, query, k=5)
    result = rag.retrieve(pids, query, k=k, case_sensitive=case_sensitive,
                          subword=subword)
    return {
        "case_hits": case_hits,
        "case_shortcircuit": any(c.get("similarity", 0) >= 0.65 for c in case_hits),
        "code_chunks": result["code_chunks"],
        "query_mode": result["query_mode"],
        "grounding": result["grounding"],
    }


def route(query: str) -> Dict[str, Any]:
    """Agent-style capability routing with deterministic graceful degradation.

    Returns which capability (glossary / structure / search) handles the query, who
    decided (a ready local model — validated against the capability set — else the
    deterministic if-else floor), plus the deterministic fallback and reason. The
    model can never select a capability outside the known set."""
    return router.route(query)


def dispatch(scope: str, query: str) -> Dict[str, Any]:
    """Route the query to one capability and INVOKE it; returns the result plus the
    routing trace. Each capability is deterministic/grounded; the router only chooses."""
    return router.dispatch(_pids(scope), query)


def get_glossary(scope: str, term: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic term/acronym resolution from the persisted glossary map.

    With `term`: an EXACT-TOKEN lookup (no similarity, no model guess) returning
    {definition (verbatim), source_file, line_number, content_hash, source_kind};
    an absent term returns found=False with 'no authoritative definition found in
    the indexed project' (never a fabricated definition). Without `term`: list
    every known term. Acronym/term questions should route here FIRST."""
    return glossary.get_glossary(mapio.merged_glossary(_pids(scope)), term)


def find_similar_cases(problem: str, scope: str, k: int = 5) -> Dict[str, Any]:
    """Search the solved-cases store for similar problems (with staleness flags)."""
    return {"cases": cases.search_cases(_pids(scope), problem, k=k)}


def save_case(scope: str, problem_text: str, resolution_summary: str,
              involved_services: Optional[List[str]] = None,
              involved_topics: Optional[List[str]] = None,
              file_refs: Optional[List[Dict[str, Any]]] = None,
              tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Save a solved case to the (single) project resolved by scope."""
    pids = _pids(scope)
    if len(pids) != 1:
        raise ValueError("save_case requires a single-project scope, got " + str(len(pids)))
    return cases.save_case(pids[0], {
        "problem_text": problem_text, "resolution_summary": resolution_summary,
        "involved_services": involved_services or [], "involved_topics": involved_topics or [],
        "file_refs": file_refs or [], "tags": tags or [],
    })


def get_doc(page: str, scope: str) -> Dict[str, Any]:
    """Fetch a generated documentation page (markdown) with its sync status."""
    doc = docsmod.get_doc(_pids(scope), page)
    if not doc:
        raise ValueError("doc page not found: " + page)
    return doc


def propose_fix(file_path: str, find: str, replace: str,
                scope: Optional[str] = None) -> Dict[str, Any]:
    """Preview a SMALL literal find/replace edit as a unified diff. Writes
    nothing — the human-in-the-loop review step before apply_fix.

    `file_path` may be the project-relative path returned by search/get_glossary;
    pass `scope` to re-anchor it to this machine's source root."""
    return codemod.propose(_anchor(file_path, scope), find, replace)


def apply_fix(file_path: str, find: str, replace: str, test_cmd: str,
              cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """Apply a literal find/replace ONLY if it keeps the test suite green.

    Runs `test_cmd` first (baseline must be green), applies the edit, re-runs the
    tests, and KEEPS the change only on green — otherwise the file is reverted
    byte-for-byte. The model never decides correctness; the tests do.

    `file_path` may be the project-relative path returned by search/get_glossary;
    pass `scope` to re-anchor it to this machine's source root."""
    return codemod.apply_fix(_anchor(file_path, scope), find, replace, test_cmd, cwd=cwd)


#: The published tool set, in registration order. The names are the MCP tool
#: names clients call.
TOOLS: List[Callable[..., Any]] = [
    search, route, dispatch, get_glossary, find_similar_cases, save_case,
    get_doc, propose_fix, apply_fix,
]

TOOL_NAMES = tuple(fn.__name__ for fn in TOOLS)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def create_mcp_server(runtime: Optional[Any] = None) -> FastMCP:
    """Build a ``FastMCP`` server exposing :data:`TOOLS`.

    *runtime* is an :class:`~openmind.runtime.OpenMindRuntime`; when omitted the
    process-wide one is created and bootstrapped. Bootstrapping here (rather than
    at import time) is what makes the module importable in a test without opening
    a database, while still guaranteeing that a served process has run its
    migrations.
    """
    if runtime is None:
        from .runtime import get_runtime
        get_runtime()
    else:
        runtime.bootstrap()

    server = FastMCP(SERVER_NAME)
    for fn in TOOLS:
        server.tool()(fn)
    return server


_mcp: Optional[FastMCP] = None


def __getattr__(name: str) -> Any:
    """Lazily provide the module-level ``mcp`` server.

    PEP 562 module ``__getattr__``: ``mcp_server.mcp`` still resolves for anyone
    who referenced it, but merely importing this module no longer builds a
    server or touches the database.
    """
    if name == "mcp":
        global _mcp
        if _mcp is None:
            _mcp = create_mcp_server()
        return _mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    from .runtime import get_runtime
    create_mcp_server(get_runtime()).run()


if __name__ == "__main__":
    main()
