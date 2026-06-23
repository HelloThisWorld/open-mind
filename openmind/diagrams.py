"""Stage 3b — STRUCTURE-DERIVED DIAGRAMS (Mermaid / Graphviz DOT).

WHY THESE ARE HONEST (design intent)
------------------------------------
Every diagram here is a projection of the deterministic Stage-2 structure map —
the dependency graph, the call/usage graph, the module rollup, and the detected
entry points. None of it is the model "understanding the business flow": a local
model cannot reliably do that multi-hop reasoning, and a guessed flow would not
survive inspection. So we draw only what the extracted graphs already contain.
Node and edge labels are real file/module names and real call symbols; where a
diagram type has nothing to draw, we say so rather than emit an empty or invented
graph.

Output is text-based (Mermaid renders natively on GitHub; DOT for Graphviz), so
the diagrams embed in docs, diff cleanly, and version alongside the code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import langspec

_CAP_NODES = 40            # keep a rendered graph legible; truncation is reported
_FLOW_MAX_NODES = 30
_FLOW_MAX_DEPTH = 3


def _base(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _safe(label: str) -> str:
    return label.replace('"', "'").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Node/edge selection (degree-ranked, capped, truncation reported — no silent cut)
# ---------------------------------------------------------------------------
def _select(edges_raw: List[Tuple[str, str, str]], cap: int):
    deg: Dict[str, int] = {}
    for a, b, _ in edges_raw:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    total = len(deg)
    keep = set(sorted(deg, key=lambda n: (-deg[n], n))[:cap])
    edges = [(a, b, lbl) for a, b, lbl in edges_raw if a in keep and b in keep]
    return keep, edges, total


def _emit(title: str, node_paths, edges, *, directed=True, dashed=False,
          fmt="mermaid", total_nodes=0) -> Dict[str, Any]:
    ids = {p: f"n{i}" for i, p in enumerate(sorted(node_paths))}
    truncated = total_nodes > len(ids)
    note = (f"showing {len(ids)} of {total_nodes} nodes (highest-connectivity first)"
            if truncated else "")
    if fmt == "dot":
        text = _dot(ids, edges, directed)
    else:
        text = _mermaid(ids, edges, dashed)
    return {"title": title, "format": fmt, "text": text, "empty": False,
            "truncated": truncated, "note": note, "node_count": len(ids),
            "edge_count": len(edges)}


def _mermaid(ids: Dict[str, str], edges, dashed: bool) -> str:
    lines = ["flowchart LR"]
    for p, nid in sorted(ids.items(), key=lambda kv: kv[1]):
        lines.append(f'  {nid}["{_safe(_base(p))}"]')
    arrow = "-.->" if dashed else "-->"
    seen = set()
    for a, b, lbl in edges:
        if a not in ids or b not in ids:
            continue
        key = (ids[a], ids[b], lbl)
        if key in seen:
            continue
        seen.add(key)
        if lbl:
            lines.append(f'  {ids[a]} {arrow}|{_safe(lbl)}| {ids[b]}')
        else:
            lines.append(f'  {ids[a]} {arrow} {ids[b]}')
    return "\n".join(lines)


def _dot(ids: Dict[str, str], edges, directed: bool) -> str:
    head = "digraph" if directed else "graph"
    conn = "->" if directed else "--"
    lines = [f"{head} structure {{", "  rankdir=LR;", "  node [shape=box];"]
    for p, nid in sorted(ids.items(), key=lambda kv: kv[1]):
        lines.append(f'  {nid} [label="{_safe(_base(p))}"];')
    seen = set()
    for a, b, lbl in edges:
        if a not in ids or b not in ids or (ids[a], ids[b]) in seen:
            continue
        seen.add((ids[a], ids[b]))
        lines.append(f'  {ids[a]} {conn} {ids[b]};')
    lines.append("}")
    return "\n".join(lines)


def _empty(title: str, reason: str, fmt: str = "mermaid") -> Dict[str, Any]:
    return {"title": title, "format": fmt, "empty": True, "note": reason,
            "text": f"%% {title}: {reason}", "node_count": 0, "edge_count": 0}


# ---------------------------------------------------------------------------
# Diagram builders (each = one projection of the structure map)
# ---------------------------------------------------------------------------
def dependency_diagram(doc: Dict[str, Any], fmt: str = "mermaid") -> Dict[str, Any]:
    """Module-level dependency graph when there are ≥2 modules, else file-level."""
    mod_edges = (doc.get("module_graph") or {}).get("edges", [])
    if len(doc.get("modules", {})) >= 2 and mod_edges:
        raw = [(e["from"], e["to"], "") for e in mod_edges]
        keep, edges, total = _select(raw, _CAP_NODES)
        return _emit("Module dependency graph", keep, edges, fmt=fmt, total_nodes=total)
    raw = [(e["from"], e["to"], "") for e in (doc.get("dependency_graph") or {}).get("edges", [])]
    if not raw:
        return _empty("Dependency graph",
                      "no internal import edges resolved for this project", fmt)
    keep, edges, total = _select(raw, _CAP_NODES)
    return _emit("File dependency graph", keep, edges, fmt=fmt, total_nodes=total)


def call_diagram(doc: Dict[str, Any], fmt: str = "mermaid") -> Dict[str, Any]:
    raw = [(e["from"], e["to"], "") for e in (doc.get("call_graph") or {}).get("edges", [])]
    if not raw:
        return _empty("Call graph", "no cross-file call/usage edges found", fmt)
    keep, edges, total = _select(raw, _CAP_NODES)
    return _emit("Call / usage graph (name-based, cross-file)", keep, edges,
                 dashed=True, fmt=fmt, total_nodes=total)


def entrypoint_flow(doc: Dict[str, Any], fmt: str = "mermaid") -> Dict[str, Any]:
    """Execution-reachable call paths starting from detected entry points (BFS over
    the call graph, bounded). Pure structure — no inferred business flow."""
    entries = [e["file"] for e in doc.get("entry_points", []) if e["kind"] in ("main", "server")]
    if not entries:
        entries = [e["file"] for e in doc.get("entry_points", [])]
    entries = list(dict.fromkeys(entries))
    if not entries:
        return _empty("Entry-point flow", "no entry points detected", fmt)
    adj: Dict[str, List[str]] = {}
    for e in (doc.get("call_graph") or {}).get("edges", []):
        adj.setdefault(e["from"], []).append(e["to"])
    nodes: set = set()
    edges: List[Tuple[str, str, str]] = []
    frontier = [(e, 0) for e in entries[:6]]
    nodes.update(entries[:6])
    seen_edge = set()
    while frontier and len(nodes) < _FLOW_MAX_NODES:
        node, depth = frontier.pop(0)
        if depth >= _FLOW_MAX_DEPTH:
            continue
        for nxt in sorted(set(adj.get(node, []))):
            if (node, nxt) not in seen_edge:
                seen_edge.add((node, nxt))
                edges.append((node, nxt, ""))
            if nxt not in nodes and len(nodes) < _FLOW_MAX_NODES:
                nodes.add(nxt)
                frontier.append((nxt, depth + 1))
    if not edges:
        return _empty("Entry-point flow",
                      "entry points detected but they make no cross-file calls", fmt)
    return _emit("Entry-point flow", nodes, edges, fmt=fmt, total_nodes=len(nodes))


def all_diagrams(doc: Dict[str, Any], fmt: str = "mermaid") -> List[Dict[str, Any]]:
    """The always-available, structure-derived diagram set for any repo."""
    return [dependency_diagram(doc, fmt), call_diagram(doc, fmt), entrypoint_flow(doc, fmt)]


# ===========================================================================
# Interactive graph projection (structured nodes/edges for the UI viewer).
# Same honesty contract as the Mermaid builders above: every node/edge is a real
# file/module/symbol from the deterministic structure map. Nodes carry enough
# metadata (file:line, glossary terms, drill affordance) for the interactive
# viewer; LAYOUT is done client-side. Drill-down ("subflows") is path-prefix
# descent for structure/call and call-graph BFS for flow.
# ===========================================================================
_GRAPH_CAP = 60


def _abs(root: str, rel: str) -> str:
    r = (root or "").replace("\\", "/").rstrip("/")
    return f"{r}/{rel}" if r else rel


def _rel_set(doc: Dict[str, Any]) -> set:
    return {f["path"] for f in (doc.get("files") or [])}


def _top_modules(doc: Dict[str, Any]) -> List[str]:
    segs = set()
    for f in (doc.get("files") or []):
        rel = f["path"]
        segs.add(rel.split("/", 1)[0] if "/" in rel else ".")
    return sorted(segs)


def _archetype(doc: Dict[str, Any]) -> Dict[str, Any]:
    mods = [m for m in _top_modules(doc) if m != "."]
    multi = len(mods) >= 2
    return {"type": "multi-module" if multi else "single-module",
            "module_count": len(mods), "multi": multi}


def _crumbs(focus: str) -> List[Dict[str, str]]:
    out = [{"id": "", "label": "root"}]
    if focus:
        acc: List[str] = []
        for seg in focus.split("/"):
            acc.append(seg)
            out.append({"id": "/".join(acc), "label": seg})
    return out


def _terms_for(files_rel, root: str, gbyfile: Dict[str, List[str]]) -> List[str]:
    terms = set()
    for rel in files_rel:
        terms.update(gbyfile.get(_abs(root, rel), ()))
        terms.update(gbyfile.get(rel, ()))
    return sorted(terms)


def _cap(nodes, edges, cap):
    if len(nodes) <= cap:
        return nodes, edges, len(nodes)
    deg: Dict[str, int] = {}
    for e in edges:
        deg[e["from"]] = deg.get(e["from"], 0) + 1
        deg[e["to"]] = deg.get(e["to"], 0) + 1
    ranked = sorted(nodes, key=lambda n: (-deg.get(n["id"], 0), -n.get("size", 0), n["id"]))
    keep = {n["id"] for n in ranked[:cap]}
    kept = [n for n in nodes if n["id"] in keep]
    kept_e = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return kept, kept_e, len(nodes)


def _file_calls(doc: Dict[str, Any]) -> set:
    s = set()
    for e in (doc.get("call_graph") or {}).get("edges", []):
        s.add(e["from"]); s.add(e["to"])
    return s


def _auto_focus(doc: Dict[str, Any]) -> str:
    """For a monolith whose top level is a single wrapper dir (e.g. 'src'), descend
    through single-child directories so the first view shows real structure."""
    files = doc.get("files") or []
    focus = ""
    for _ in range(6):
        pre = focus + "/" if focus else ""
        kids = {}
        for f in files:
            rel = f["path"]
            if focus and not (rel == focus or rel.startswith(pre)):
                continue
            under = rel[len(pre):] if pre else rel
            if under:
                kids.setdefault(under.split("/")[0], 0)
                kids[under.split("/")[0]] += 1
        dirs = [k for k in kids if any(
            (f["path"][len(pre):] if pre else f["path"]).startswith(k + "/")
            for f in files if (not focus or f["path"].startswith(pre)))]
        if len(kids) == 1 and dirs:
            focus = (pre + next(iter(kids)))
        else:
            break
    return focus


def _rollup(doc, kind, focus, gbyfile):
    """Directory-prefix rollup: group files one level below `focus` and aggregate
    dependency (structure) or call edges between the groups."""
    files = doc.get("files") or []
    root = doc.get("root") or ""
    edges_src = ((doc.get("call_graph") or {}).get("edges", []) if kind == "call"
                 else (doc.get("dependency_graph") or {}).get("edges", []))
    has_calls = _file_calls(doc)
    pre = focus + "/" if focus else ""

    def gid_of(rel):
        if focus and not (rel == focus or rel.startswith(pre)):
            return None
        under = rel[len(pre):] if pre else rel
        if not under:
            return None
        seg = under.split("/", 1)[0]
        return (pre + seg) if pre else seg

    groups: Dict[str, Dict[str, Any]] = {}
    for f in files:
        gid = gid_of(f["path"])
        if not gid:
            continue
        under = f["path"][len(pre):] if pre else f["path"]
        is_dir = "/" in under
        g = groups.setdefault(gid, {"id": gid, "label": under.split("/", 1)[0],
                                    "is_dir": False, "files": [], "lines": 0, "defs": 0})
        g["files"].append(f["path"]); g["lines"] += f.get("lines", 0)
        g["defs"] += len(f.get("defs") or [])
        g["is_dir"] = g["is_dir"] or is_dir
    if not groups:
        return None

    ew: Dict[tuple, int] = {}
    amb: Dict[tuple, bool] = {}
    for e in edges_src:
        a, b = gid_of(e["from"]), gid_of(e["to"])
        if not a or not b or a == b:
            continue
        ew[(a, b)] = ew.get((a, b), 0) + 1
        if e.get("ambiguous"):
            amb[(a, b)] = True

    nodes = []
    for gid, g in sorted(groups.items()):
        is_dir = g["is_dir"]
        rel = None if is_dir else g["files"][0]
        nodes.append({
            "id": gid, "label": g["label"], "type": "dir" if is_dir else "file",
            "size": len(g["files"]), "lines": g["lines"], "defs": g["defs"],
            "drillable": bool(is_dir or (rel in has_calls)),
            "file": None if is_dir else _abs(root, rel), "rel": rel,
            "glossary": _terms_for(g["files"], root, gbyfile),
        })
    edges = [{"from": a, "to": b, "weight": w, "ambiguous": amb.get((a, b), False)}
             for (a, b), w in sorted(ew.items())]
    nodes, edges, total = _cap(nodes, edges, _GRAPH_CAP)
    return {"nodes": nodes, "edges": edges, "total_nodes": total,
            "truncated": total > len(nodes)}


def _file_node(doc, rel, gbyfile, *, center=False):
    root = doc.get("root") or ""
    rec = next((f for f in (doc.get("files") or []) if f["path"] == rel), {})
    return {"id": rel, "label": _base(rel), "type": "file", "center": center,
            "size": len(rec.get("defs") or []) or 1, "lines": rec.get("lines", 0),
            "language": rec.get("language", ""), "drillable": True,
            "file": _abs(root, rel), "rel": rel,
            "glossary": _terms_for([rel], root, gbyfile)}


def _neighborhood(doc, kind, focus, gbyfile):
    """Callers + callees of a single file (the call/usage neighborhood)."""
    edges_src = (doc.get("call_graph") or {}).get("edges", [])
    nbr, raw = set(), []
    for e in edges_src:
        if e["from"] == focus:
            nbr.add(e["to"]); raw.append((focus, e["to"], e.get("symbol", ""), e.get("ambiguous", False)))
        elif e["to"] == focus:
            nbr.add(e["from"]); raw.append((e["from"], focus, e.get("symbol", ""), e.get("ambiguous", False)))
    ids = {focus} | nbr
    nodes = [_file_node(doc, r, gbyfile, center=(r == focus)) for r in sorted(ids)]
    seen, edges = set(), []
    for a, b, lbl, am in raw:
        k = (a, b)
        if k in seen:
            continue
        seen.add(k); edges.append({"from": a, "to": b, "label": lbl, "ambiguous": am})
    nodes, edges, total = _cap(nodes, edges, _GRAPH_CAP)
    return {"nodes": nodes, "edges": edges, "total_nodes": total,
            "truncated": total > len(nodes)}


def _flow(doc, focus, gbyfile):
    """Entry-point execution flow (BFS over the real call graph), or the downstream
    subflow rooted at `focus` when a file node is drilled into."""
    adj: Dict[str, List[str]] = {}
    for e in (doc.get("call_graph") or {}).get("edges", []):
        adj.setdefault(e["from"], []).append(e["to"])
    if focus and focus in _rel_set(doc):
        roots = [focus]
    else:
        roots = [e["file"] for e in doc.get("entry_points", []) if e["kind"] in ("main", "server")]
        if not roots:
            roots = [e["file"] for e in doc.get("entry_points", [])]
        roots = list(dict.fromkeys(roots))[:6]
    if not roots:
        return {"nodes": [], "edges": [], "total_nodes": 0, "truncated": False}
    nodes_ids, edges, seen_e = set(roots), [], set()
    frontier = [(r, 0) for r in roots]
    while frontier and len(nodes_ids) < _FLOW_MAX_NODES:
        node, depth = frontier.pop(0)
        if depth >= _FLOW_MAX_DEPTH:
            continue
        for nxt in sorted(set(adj.get(node, []))):
            if (node, nxt) not in seen_e:
                seen_e.add((node, nxt)); edges.append({"from": node, "to": nxt, "label": ""})
            if nxt not in nodes_ids and len(nodes_ids) < _FLOW_MAX_NODES:
                nodes_ids.add(nxt); frontier.append((nxt, depth + 1))
    nodes = []
    for r in sorted(nodes_ids):
        n = _file_node(doc, r, gbyfile, center=(r in roots))
        # a node with callees beyond what's shown is a drillable SUBFLOW
        n["drillable"] = bool(adj.get(r))
        n["entry"] = r in roots
        nodes.append(n)
    return {"nodes": nodes, "edges": edges, "total_nodes": len(nodes_ids),
            "truncated": len(nodes_ids) >= _FLOW_MAX_NODES}


def interactive_graph(doc: Dict[str, Any], kind: str = "structure",
                      focus: str = "", gbyfile: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """Build a structured, interactive graph for one of: structure | call | flow.

    `focus` drives drill-down: a directory prefix re-rolls one level deeper; a file
    path shows its call neighborhood (structure/call) or its downstream subflow
    (flow). Returns nodes/edges + breadcrumb + archetype + honest truncation."""
    gbyfile = gbyfile or {}
    focus = (focus or "").strip().strip("/")
    arche = _archetype(doc)
    if not (doc.get("files")):
        return {"kind": kind, "focus": focus, "archetype": arche, "breadcrumb": _crumbs(focus),
                "nodes": [], "edges": [], "empty": True, "truncated": False,
                "note": "no indexed source files for this scope", "view": "empty"}

    is_file = focus in _rel_set(doc)
    if kind == "flow":
        g = _flow(doc, focus, gbyfile); view = "flow"
    elif is_file:
        g = _neighborhood(doc, kind, focus, gbyfile); view = "neighborhood"
    else:
        if not focus:
            focus = _auto_focus(doc)
        g = _rollup(doc, kind, focus, gbyfile); view = "rollup"

    if not g or not g["nodes"]:
        reason = {"flow": "no entry points make cross-file calls",
                  "call": "no cross-file call/usage edges here",
                  "structure": "no files under this scope"}.get(kind, "nothing to draw")
        return {"kind": kind, "focus": focus, "archetype": arche, "breadcrumb": _crumbs(focus),
                "nodes": [], "edges": [], "empty": True, "truncated": False,
                "note": reason, "view": view}
    note = (f"showing {len(g['nodes'])} of {g['total_nodes']} nodes "
            "(highest-connectivity first)" if g.get("truncated") else "")
    return {"kind": kind, "focus": focus, "archetype": arche, "breadcrumb": _crumbs(focus),
            "nodes": g["nodes"], "edges": g["edges"], "empty": False,
            "truncated": g.get("truncated", False), "note": note, "view": view}


def node_detail(doc: Dict[str, Any], node_id: str,
                gbyfile: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """Detail for a clicked node: source location, defs, call neighbors, glossary."""
    gbyfile = gbyfile or {}
    root = doc.get("root") or ""
    if node_id in _rel_set(doc):
        rec = next((f for f in doc["files"] if f["path"] == node_id), {})
        callers = sorted({e["from"] for e in (doc.get("call_graph") or {}).get("edges", []) if e["to"] == node_id})
        callees = sorted({e["to"] for e in (doc.get("call_graph") or {}).get("edges", []) if e["from"] == node_id})
        return {"type": "file", "id": node_id, "label": _base(node_id),
                "file": _abs(root, node_id), "rel": node_id, "line": 1,
                "language": rec.get("language", ""), "lines": rec.get("lines", 0),
                "defs": [{"name": d["name"], "kind": d["kind"], "line": d["line"]}
                         for d in (rec.get("defs") or [])][:200],
                "callers": [{"id": c, "label": _base(c)} for c in callers[:60]],
                "callees": [{"id": c, "label": _base(c)} for c in callees[:60]],
                "glossary": _terms_for([node_id], root, gbyfile), "drillable": True}
    # directory node
    pre = node_id + "/" if node_id else ""
    members = [f for f in (doc.get("files") or [])
               if not node_id or f["path"] == node_id or f["path"].startswith(pre)]
    langs: Dict[str, int] = {}
    for f in members:
        langs[f.get("language", "?")] = langs.get(f.get("language", "?"), 0) + 1
    return {"type": "dir", "id": node_id, "label": _base(node_id) or "root",
            "file_count": len(members), "lines": sum(f.get("lines", 0) for f in members),
            "defs": sum(len(f.get("defs") or []) for f in members),
            "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
            "glossary": _terms_for([f["path"] for f in members], root, gbyfile),
            "drillable": True}


# ===========================================================================
# Graphs v2: source-only filtering (#4), call MIND-MAP (#6/#7), flow ENTRY list.
# ===========================================================================
def _is_source(rel: str) -> bool:
    """A real source file in a language we can analyse (drops build/CI/config)."""
    return langspec.language_for(rel) is not None


def _source_paths(doc: Dict[str, Any]) -> set:
    """Source files only. If the repo uses the conventional `src/` layout, keep
    only files under a src tree — that drops build scripts, CI, and tooling
    (.gradle/.yaml/.github/committer-tools/release/checkstyle.py …)."""
    files = [f["path"] for f in (doc.get("files") or []) if _is_source(f["path"])]
    if any("/src/" in ("/" + p) for p in files):
        files = [p for p in files if "/src/" in ("/" + p)]
    return set(files)


def _call_adj(doc: Dict[str, Any], srcset: set):
    adj: Dict[str, set] = {}
    radj: Dict[str, set] = {}
    for e in (doc.get("call_graph") or {}).get("edges", []):
        a, b = e["from"], e["to"]
        if a in srcset and b in srcset and a != b:
            adj.setdefault(a, set()).add(b)
            radj.setdefault(b, set()).add(a)
    return adj, radj


def call_roots(doc: Dict[str, Any], gbyfile: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """Mind-map roots for the call view: entry-point + highest-fan-out source
    files. Each is expandable (lazy `call_children`); dashed `links` mark direct
    call relationships BETWEEN roots."""
    gbyfile = gbyfile or {}
    root = doc.get("root") or ""
    src = _source_paths(doc)
    if not src:
        return {"kind": "call", "roots": [], "links": [], "empty": True,
                "note": "no source files for this scope"}
    adj, radj = _call_adj(doc, src)
    entries = list(dict.fromkeys(e["file"] for e in doc.get("entry_points", []) if e["file"] in src))
    top = sorted(src, key=lambda p: (-len(adj.get(p, ())), p))
    rootsel = list(dict.fromkeys(entries + top))[:14]
    rset = set(rootsel)

    def node(p):
        return {"id": p, "label": _base(p), "file": _abs(root, p), "line": 1,
                "out": len(adj.get(p, ())), "in": len(radj.get(p, ())),
                "entry": p in set(entries), "hasChildren": bool(adj.get(p)),
                "glossary": _terms_for([p], root, gbyfile)}

    roots = [node(p) for p in rootsel]
    links = [{"from": a, "to": b} for a in rootsel for b in sorted(adj.get(a, ())) if b in rset]
    return {"kind": "call", "roots": roots, "links": links, "empty": False,
            "note": f"{len(roots)} entry points / hubs — click to expand callees"}


def call_children(doc: Dict[str, Any], node_id: str, offset: int = 0, limit: int = 7,
                  gbyfile: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """Callees of one node, paginated (the #7 'show 7, +more' affordance)."""
    gbyfile = gbyfile or {}
    root = doc.get("root") or ""
    src = _source_paths(doc)
    adj, _ = _call_adj(doc, src)
    callees = sorted(adj.get(node_id, ()), key=lambda p: (-len(adj.get(p, ())), p))
    total = len(callees)
    page = callees[offset:offset + max(1, limit)]
    children = [{"id": p, "label": _base(p), "file": _abs(root, p), "line": 1,
                 "out": len(adj.get(p, ())), "hasChildren": bool(adj.get(p)),
                 "glossary": _terms_for([p], root, gbyfile)} for p in page]
    return {"id": node_id, "children": children, "total": total, "offset": offset,
            "hasMore": offset + len(page) < total}



def to_markdown(doc: Dict[str, Any]) -> str:
    """Render the diagram set as a Markdown doc page with fenced Mermaid blocks."""
    out = ["# Structure Diagrams\n",
           "_Derived deterministically from the dependency graph, call/usage "
           "graph, and detected entry points — not model-inferred flows._\n"]
    for d in all_diagrams(doc, "mermaid"):
        out.append(f"## {d['title']}")
        if d.get("note"):
            out.append(f"> {d['note']}\n")
        if d["empty"]:
            out.append(f"_{d['note']}._\n")
        else:
            out.append("```mermaid\n" + d["text"] + "\n```\n")
    return "\n".join(out)
