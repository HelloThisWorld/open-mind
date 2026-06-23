"""RAG: tree-sitter semantic chunking, local CPU embeddings, Chroma storage,
and hybrid exact-token-aware retrieval.

Chunking: one chunk per method (oversized split with overlap) + one
class-summary chunk per class, each prefixed with a structural HEADER
(file, package, class, signature, service).

Retrieval: vector top-k + lexical/exact (RRF fuse). A bare identifier/literal
query routes to EXACT token matching (token-boundary, never substring-conflated);
a natural-language query uses the conceptual hybrid. Project-agnostic — works for
any repository.

Incremental indexing is idempotent (Invariant 12): stable content-derived
chunk ids; unchanged files are skipped by the job; changed files are replaced.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from . import config, embeddings, javaparse as jp, machine, tokenmatch, vectorstore, walker


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def _chunk_id(file_path: str, symbol: str, idx: int) -> str:
    raw = f"{file_path}|{symbol}|{idx}"
    return "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]


def _header(file_path: str, package: str, cls: str, symbol: str,
            service: str) -> str:
    return (
        f"// file: {file_path}\n"
        f"// service: {service}\n"
        f"// package: {package}\n"
        f"// class: {cls}\n"
        f"// symbol: {symbol}\n"
    )


def _split_lines(text: str, start_line: int, max_lines: int, overlap: int):
    lines = text.split("\n")
    if len(lines) <= max_lines:
        yield text, start_line, start_line + len(lines) - 1
        return
    i = 0
    while i < len(lines):
        seg = lines[i:i + max_lines]
        yield "\n".join(seg), start_line + i, start_line + i + len(seg) - 1
        if i + max_lines >= len(lines):
            break
        i += max_lines - overlap


def chunk_file(project_id: str, file_path: str, text: str, service: str, repo: str,
               file_topics: List[str], file_hash: str) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    is_java = file_path.lower().endswith(".java")
    package = ""
    parsed_ok = False
    if is_java and jp.available():
        try:
            tree = jp.parse(text)
            root = tree.root_node
            src = bytes(text, "utf8")
            package = jp.get_package(root, src)
            for t in jp.iter_types(root, src):
                cls = t["name"]
                node = t["node"]
                # class-summary chunk: declaration + field/method signatures
                sig_lines = [f"class {cls} ({t['kind']})"]
                for f in jp.iter_fields(node, src):
                    sig_lines.append(f"  field {f['type']} {f['name']}")
                methods = list(jp.iter_methods(node, src))
                for m in methods:
                    sig_lines.append(f"  method {m['signature']}")
                summary_body = "\n".join(sig_lines)
                _add(chunks, project_id, file_path, package, cls,
                     f"{cls} (class summary)", service, repo, file_topics,
                     summary_body, t["start_line"], t["end_line"], "class", file_hash)
                # per-method chunks
                for mi, m in enumerate(methods):
                    body = jp.text(m["node"], src)
                    for seg, sl, el in _split_lines(body, m["start_line"],
                                                    config.CHUNK_MAX_LINES,
                                                    config.CHUNK_OVERLAP_LINES):
                        _add(chunks, project_id, file_path, package, cls,
                             m["signature"], service, repo, file_topics,
                             seg, sl, el, "method", file_hash)
            parsed_ok = True
        except Exception:
            parsed_ok = False
    if not parsed_ok:
        # config / xml / parse-failure: whole-file chunks
        cls = os.path.basename(file_path)
        for seg, sl, el in _split_lines(text, 1, config.CHUNK_MAX_LINES,
                                        config.CHUNK_OVERLAP_LINES):
            _add(chunks, project_id, file_path, package, cls, cls, service, repo,
                 file_topics, seg, sl, el, "file", file_hash)
    # assign stable ids by running index
    for i, c in enumerate(chunks):
        c["id"] = _chunk_id(file_path, c["_symbol"], i)
        c.pop("_symbol", None)
    return chunks


def _add(chunks, project_id, file_path, package, cls, symbol, service, repo,
         topics, body, start_line, end_line, ctype, file_hash):
    header = _header(file_path, package, cls, symbol, service)
    document = header + "\n" + body
    chunks.append({
        "_symbol": symbol,
        "document": document,
        "metadata": {
            "project_id": project_id, "repo": repo, "file_path": file_path,
            "package": package, "class": cls, "symbol": symbol, "service": service,
            "topics_csv": ",".join(topics), "chunk_type": ctype,
            "file_hash": file_hash, "line_range": f"{start_line}-{end_line}",
            "start_line": start_line, "end_line": end_line,
        },
    })


# ---------------------------------------------------------------------------
# Indexing (incremental, idempotent)
# ---------------------------------------------------------------------------
def index_file(project_id: str, store, file_path: str, text: str, service: str,
               repo: str, file_topics: List[str], file_hash: str) -> List[str]:
    chunks = chunk_file(project_id, file_path, text, service, repo, file_topics, file_hash)
    if not chunks:
        return []
    ids = [c["id"] for c in chunks]
    docs = [c["document"] for c in chunks]
    metas = [c["metadata"] for c in chunks]
    vecs = embeddings.embed(docs)
    store.upsert(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
    return ids


def embed_and_upsert(store, chunks: List[Dict[str, Any]]) -> None:
    """Embed a batch of pre-built chunks (typically spanning MANY files) in ONE
    embeddings call and upsert them together. Batching across files is what keeps
    a GPU / throughput-oriented embedding backend fully utilised — embedding one
    small (~10-chunk) file at a time pays a fixed per-call overhead that dominates
    and starves the device. Chunk ids are stable + content-derived, so a re-embed
    of the same chunk is idempotent (Invariant 12)."""
    if not chunks:
        return
    ids = [c["id"] for c in chunks]
    docs = [c["document"] for c in chunks]
    metas = [c["metadata"] for c in chunks]
    vecs = embeddings.embed(docs)
    store.upsert(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)


def delete_file_chunks(store, file_path: str, old_ids: Optional[List[str]] = None) -> None:
    if old_ids:
        store.delete(ids=old_ids)
    else:
        store.delete(where={"file_path": file_path})


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
_TOPIC_RE = re.compile(r"[A-Za-z][\w-]*(?:\.[\w-]+)+")
_CAMEL_RE = re.compile(r"[A-Z][a-zA-Z0-9]*(?:[A-Z][a-zA-Z0-9]*)+")
_UPPER_RE = re.compile(r"[A-Z]{2,}(?:_[A-Z0-9]+)+")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STOP = {"this", "that", "with", "from", "what", "which", "where", "when", "does",
         "service", "topic", "kafka", "code", "find", "show", "class", "method",
         "produce", "produces", "consume", "consumes", "consumer", "producer"}


def extract_tokens(query: str) -> List[str]:
    toks: List[str] = []
    toks += _TOPIC_RE.findall(query)
    toks += _CAMEL_RE.findall(query)
    toks += _UPPER_RE.findall(query)
    for w in _WORD_RE.findall(query):
        if w.lower() not in _STOP and (any(ch.isupper() for ch in w) or "_" in w):
            toks.append(w)
    # de-dup preserving order
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:10]


def _rrf(rank_lists: List[List[str]], k0: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in rank_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k0 + rank + 1)
    return scores


def _lex_candidates(store, term: str, case_sensitive: bool):
    """Candidate superset for token verification. For case-sensitive queries we
    use the store's substring index ($contains) as a cheap prefilter (it is a
    superset of any whole-token match); for case-insensitive we scan all chunks
    (opt-in) since the substring index is case-sensitive on some backends."""
    term = tokenmatch.strip_quotes(term)
    if not term:
        return []
    if case_sensitive:
        res = store.get(where_document={"$contains": term})
    else:
        res = store.get()
    return list(zip(res["ids"], res["documents"], res["metadatas"]))


def retrieve(project_ids: List[str], query: str, k: int = 12,
             case_sensitive: bool = True, subword: bool = False,
             exact: Optional[bool] = None) -> Dict[str, Any]:
    stores = {pid: vectorstore.get_code_store(pid) for pid in project_ids}
    pool: Dict[str, Dict[str, Any]] = {}   # id -> {meta, doc, pid, sources, kinds}

    def remember(cid, doc, meta, pid, source):
        rec = pool.get(cid)
        if rec is None:
            rec = pool[cid] = {"doc": doc, "meta": meta, "pid": pid,
                               "sources": set(), "kinds": set()}
        rec["sources"].add(source)
        return rec

    # Route: a bare identifier/literal query -> EXACT token mode; an NL phrase
    # -> conceptual hybrid. (Caller may override via `exact`.)
    is_exact = tokenmatch.is_exact_token_query(query) if exact is None else exact
    terms = [tokenmatch.strip_quotes(query)] if is_exact else extract_tokens(query)

    # ---- vector leg (candidate only; never conflates tokens) ----
    qvec = embeddings.embed([query])[0].tolist()
    vector_rank: List[str] = []
    for pid, store in stores.items():
        res = store.query(qvec, n_results=max(k * 2, 10))
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
            remember(cid, doc, meta, pid, "vector")
            vector_rank.append(cid)
    vpos = {cid: i for i, cid in enumerate(vector_rank)}

    # ---- lexical leg: TOKEN-BOUNDARY exact (never substring/prefix/suffix) ----
    lex_hits: Dict[str, int] = {}
    for pid, store in stores.items():
        for term in terms:
            for cid, doc, meta in _lex_candidates(store, term, case_sensitive):
                kind = tokenmatch.match_kind(doc, term, case_sensitive=case_sensitive,
                                             subword=subword)
                if kind:
                    rec = remember(cid, doc, meta, pid, "lexical")
                    rec["kinds"].add(kind)
                    lex_hits[cid] = lex_hits.get(cid, 0) + 1

    fused: Dict[str, float] = {}
    if is_exact:
        # Rules 2/3/4: exact-token matches take PRECEDENCE; complete-token ranks
        # above subword; vector only breaks ties. A vector hit is returned ONLY
        # if it ALSO token-matches (it is then already in lex_hits) — so a chunk
        # whose only occurrence is a substring/prefix/suffix, or a purely
        # embedding-similar chunk, is never returned for a token query.
        def _key(cid):
            return (0 if "token" in pool[cid]["kinds"] else 1, vpos.get(cid, 10**9), cid)
        ordered = sorted(lex_hits.keys(), key=_key)
        query_mode = "exact_token"
    else:
        # conceptual: vector + token-precise lexical, RRF-fused
        lexical_rank = [cid for cid, _ in sorted(lex_hits.items(), key=lambda x: -x[1])]
        fused = _rrf([vector_rank, lexical_rank])
        ordered = [cid for cid, _ in sorted(fused.items(), key=lambda x: -x[1])][:k]
        query_mode = "conceptual"

    # dedupe preserving order, cap ~20
    seen, final = set(), []
    for cid in ordered:
        if cid in pool and cid not in seen:
            seen.add(cid)
            final.append(cid)
    final = final[:20]

    # Paths are returned RELATIVE to the project's machine-local root, so search
    # results, Ask grounding/prompt, and saved cases all stay trace-free. For
    # legacy (pre-relearn) chunks the abs path is also baked into the embedded
    # document text, so we scrub the root prefix from the displayed snippet too.
    roots = {pid: machine.project_root(pid) for pid in project_ids}
    code_chunks = []
    for cid in final:
        rec = pool[cid]
        meta = rec["meta"]
        root = roots.get(rec["pid"], "")
        doc = rec["doc"]
        if root:
            doc = doc.replace(root.rstrip("/") + "/", "")
        code_chunks.append({
            "id": cid,
            "file_path": machine.relativize(meta.get("file_path") or "", root),
            "repo": machine.relativize(meta.get("repo") or "", root),
            "service": meta.get("service"),
            "package": meta.get("package"),
            "class": meta.get("class"),
            "symbol": meta.get("symbol"),
            "chunk_type": meta.get("chunk_type"),
            "line_range": meta.get("line_range"),
            "project_id": rec["pid"],
            "score": round(fused.get(cid, 0.0), 5),
            "match": sorted(rec["kinds"]) or None,   # 'token' / 'subword'
            "sources": sorted(rec["sources"]),
            "code": doc,
        })

    return {
        "code_chunks": code_chunks,
        "tokens": terms,
        "query_mode": query_mode,
        "exact_token": is_exact,
        "case_sensitive": case_sensitive,
        "subword": subword,
        "grounding": tokenmatch.GROUNDING_NOTE,
        "backends": {"embeddings": embeddings.backend_name(),
                     "vectorstore": vectorstore.backend_name()},
    }
