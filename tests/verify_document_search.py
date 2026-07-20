"""Document retrieval, combined knowledge search and candidate association.

The properties under test are the ones a knowledge base is useless without:
an exact identifier must win over similar-looking text, every hit must be
citable, code and document results must stay separated, and a candidate must
never be reported as a confirmed relationship.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from pathlib import Path  # noqa: E402

from openmind import db, document_rag  # noqa: E402
from openmind.domain.types import (CANDIDATE_STATUS,  # noqa: E402
                                   CandidateConfidence, CandidateType)
from openmind.runtime import get_runtime  # noqa: E402

_results = []
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


runtime = get_runtime()
SRC = Path(tempfile.mkdtemp(prefix="om_search_"))
(SRC / "NameCheckService.java").write_text(
    "package com.example.namecheck;\n"
    "public class NameCheckService {\n"
    "    public ScreeningResult screen(ScreeningRequest request) {\n"
    "        return watchList.match(request.normalizedName());\n    }\n}\n",
    encoding="utf-8")
DOCS = SRC / "docs"
DOCS.mkdir()
for name in ("sample-requirements.md", "sample-cases.csv", "sample-design.html",
             "sample-schema.sql"):
    shutil.copy(FIXTURES / name, DOCS / name.replace("sample-", ""))
WS = runtime.workspaces.create("doc-search", path=str(SRC))["id"]
runtime.ingest.start(WS, wait=True, timeout=900)

# The SQL schema needs to be a DOCUMENT (structured sql-object segments) for the
# database-object signal; the code pipeline only produces line-range segments.
runtime.documents.add_document(WS, str(FIXTURES / "sample-schema.sql"),
                               logical_key="documents/schema.sql", wait=True,
                               timeout=900)

REQ_ASSET = db.find_asset_by_logical_key(WS, "docs/requirements.md")["id"]


# ---------------------------------------------------------------------------
# 1. Exact identifiers
# ---------------------------------------------------------------------------
result = runtime.documents.search(WS, "REQ-NC-017", limit=10)
check("1: an exact Requirement ID is found", result["count"] > 0)
check("1: the query is routed to exact-token mode",
      result["query_mode"] == "exact_token")
check("1: the identifier is reported back",
      result["identifiers"] == ["REQ-NC-017"])
check("1: every hit actually CONTAINS the identifier",
      all("REQ-NC-017" in h["excerpt"] for h in result["hits"]))
check("1: an exact hit is labelled as such",
      all("exact-identifier" in h["retrieval_sources"] for h in result["hits"]))
check("1: the specification paragraph is among the hits",
      any(h["logical_key"] == "docs/requirements.md" for h in result["hits"]))
check("1: only blocks that contain the id are returned in exact mode",
      {h["logical_key"] for h in result["hits"]}
      == {"docs/requirements.md", "docs/cases.csv"})

# REQ-NC-0170 exists nowhere. Retrieval may still fall back to conceptual
# similarity, but nothing may be presented as an EXACT match for it — that is
# the confusion the token-boundary rule exists to prevent.
near_miss = runtime.documents.search(WS, "REQ-NC-0170", limit=10)
check("1: a NEAR identifier produces no exact match",
      not any("exact-identifier" in h["retrieval_sources"]
              for h in near_miss["hits"]))
check("1: a near identifier does not token-match the real one",
      document_rag.matches_term("REQ-NC-017: a manual review", "REQ-NC-0170")
      is False)

check("1: an error code is found exactly",
      all("NC-100" in h["excerpt"]
          for h in runtime.documents.search(WS, "NC-100", limit=10)["hits"]))
config_key = runtime.documents.search(WS, "namecheck.review.timeout.minutes",
                                      limit=10)
check("1: a dotted configuration key is found exactly",
      config_key["count"] > 0
      and all("namecheck.review.timeout.minutes" in h["excerpt"]
              for h in config_key["hits"]))

# ---------------------------------------------------------------------------
# 2. An exact identifier outranks a semantic-only result
# ---------------------------------------------------------------------------
mixed = runtime.documents.search(WS, "what is the manual review timeout for "
                                     "REQ-NC-017?", limit=10)
exact_positions = [i for i, h in enumerate(mixed["hits"])
                   if "exact-identifier" in h["retrieval_sources"]]
semantic_positions = [i for i, h in enumerate(mixed["hits"])
                      if "exact-identifier" not in h["retrieval_sources"]]
check("2: a natural-language query still finds the identifier",
      bool(exact_positions))
check("2: every exact hit outranks every semantic-only hit",
      not semantic_positions or max(exact_positions) < min(semantic_positions))
check("2: the mode reports that an identifier was involved",
      mixed["query_mode"] == "exact_identifier")

conceptual = runtime.documents.search(WS, "how long before a review times out",
                                      limit=10)
check("2: a purely conceptual query still returns hits", conceptual["count"] > 0)
check("2: a conceptual query is labelled conceptual",
      conceptual["query_mode"] == "conceptual")

# ---------------------------------------------------------------------------
# 3. Every hit is citable and bounded
# ---------------------------------------------------------------------------
hits = runtime.documents.search(WS, "REQ-NC-017", limit=10)["hits"]
required = ("asset_id", "revision_id", "segment_id", "evidence_id", "title",
            "logical_key", "block_type", "heading_path", "locator", "excerpt",
            "score", "retrieval_sources")
check("3: every hit carries the full contract", all(
    all(key in hit for key in required) for hit in hits))
check("3: every hit has an evidence id", all(hit["evidence_id"] for hit in hits))
check("3: the evidence id resolves to the exact stored block",
      runtime.assets.get_evidence(WS, hits[0]["evidence_id"])
      ["snapshot"]["status"] == "available")
check("3: the excerpt is bounded",
      all(len(hit["excerpt"]) <= document_rag.EXCERPT_CHARS for hit in hits))
check("3: the embedded structural header is NOT shown as document content",
      not any(hit["excerpt"].startswith("// document:") for hit in hits))
check("3: the locator is portable",
      all(hit["locator"].get("document", "").count(":") == 0 for hit in hits))
check("3: a limit above the hard cap is clamped",
      runtime.documents.search(WS, "the", limit=10_000)["count"]
      <= document_rag.MAX_HITS)

# ---------------------------------------------------------------------------
# 4. Filters
# ---------------------------------------------------------------------------
check("4: filtering by parser restricts the results",
      all(h["parser"] == "csv"
          for h in runtime.documents.search(WS, "TC-001", limit=10,
                                            parser="csv")["hits"]))
check("4: filtering by block type restricts the results",
      all(h["block_type"] == "table-row"
          for h in runtime.documents.search(WS, "NC-100", limit=10,
                                            block_type="table-row")["hits"]))
check("4: filtering by logical key restricts the results",
      all(h["logical_key"] == "docs/requirements.md"
          for h in runtime.documents.search(
              WS, "REQ-NC-017", limit=10,
              logical_key="docs/requirements.md")["hits"]))

# A removed document must not come back by default.
os.remove(DOCS / "design.html")
runtime.ingest.start(WS, wait=True, timeout=900)
after_removal = runtime.documents.search(WS, "NC-101", limit=20)
check("4: a removed document is excluded by default",
      not any(h["logical_key"] == "docs/design.html"
              for h in after_removal["hits"]))
check("4: the removed asset is still `removed`, not deleted",
      db.find_asset_by_logical_key(WS, "docs/design.html")["state"] == "removed")

# ---------------------------------------------------------------------------
# 5. Combined knowledge search
# ---------------------------------------------------------------------------
combined = runtime.documents.search_knowledge(WS, "NameCheck screening timeout")
check("5: code and documents are separate sections",
      "code" in combined and "documents" in combined
      and "hits" in combined["code"] and "hits" in combined["documents"])
check("5: the grounding counts both sides",
      combined["grounding"]["codeCount"] == len(combined["code"]["hits"])
      and combined["grounding"]["documentCount"]
      == len(combined["documents"]["hits"]))
check("5: document results are present",
      combined["grounding"]["documentCount"] > 0)
check("5: code results are present", combined["grounding"]["codeCount"] > 0)
check("5: the two sets are not merged",
      not any(h.get("evidence_id") for h in combined["code"]["hits"]))
check("5: the response states that adjacency is NOT a relationship",
      "NOT a claim" in combined["grounding"]["note"])
# The DATA must carry no relationship verb. (The grounding note deliberately
# names those verbs in order to disclaim them, so it is excluded here.)
payload = str({k: v for k, v in combined.items() if k != "grounding"}).lower()
for forbidden in ("implements", "refines", "verifies", "contradicts"):
    check(f"5: no result field claims '{forbidden}'", forbidden not in payload)
check("5: no hit carries a relationship field",
      not any(set(hit) & {"implements", "refines", "verifies", "contradicts",
                          "relation", "relationship"}
              for hit in combined["documents"]["hits"]))

# The code-oriented search must be unchanged by all of this.
from openmind import rag  # noqa: E402

code_only = rag.retrieve([WS], "NameCheckService", k=5)
check("5: the code RAG still returns code chunks",
      bool(code_only["code_chunks"]))
check("5: no document chunk leaked into the code collection",
      not any(c["id"].startswith("d_") for c in code_only["code_chunks"]))
check("5: the code RAG response shape is unchanged",
      {"code_chunks", "tokens", "query_mode", "exact_token", "grounding",
       "backends"} <= set(code_only))

# ---------------------------------------------------------------------------
# 6. Candidate association
# ---------------------------------------------------------------------------
related = runtime.documents.find_related_candidates(WS, REQ_ASSET, limit=30)
check("6: candidates are found", related["count"] > 0)
check("6: the envelope is labelled `candidate`",
      related["status"] == CANDIDATE_STATUS)
check("6: every candidate is individually labelled `candidate`",
      all(c["status"] == CANDIDATE_STATUS for c in related["candidates"]))
check("6: the note says these are not confirmed relations",
      "not verified relationships" in related["note"].lower())

by_type = {c["candidate_type"] for c in related["candidates"]}
check("6: a real code symbol mentioned by the document is found",
      CandidateType.MENTIONS_SYMBOL in by_type)
symbol_candidate = next(c for c in related["candidates"]
                        if c["candidate_type"] == CandidateType.MENTIONS_SYMBOL)
check("6: the symbol candidate names the symbol",
      symbol_candidate["mention"] == "NameCheckService")
check("6: an exact symbol match is HIGH confidence",
      symbol_candidate["confidence"] == CandidateConfidence.HIGH)
check("6: it points at the real code asset",
      symbol_candidate["target"]["logical_key"] == "NameCheckService.java")
check("6: it cites the document evidence it was observed in",
      bool(symbol_candidate["document_evidence"]["evidence_id"]))
check("6: it records how it was found",
      symbol_candidate["retrieval_method"] == "exact-symbol")
check("6: it explains itself in prose", bool(symbol_candidate["reason"]))

check("6: a requirement id shared with another document is found",
      any(c["retrieval_method"] == "exact-requirement-id"
          for c in related["candidates"]))
shared = next(c for c in related["candidates"]
              if c["retrieval_method"] == "exact-requirement-id")
check("6: the shared-id candidate points at the other document",
      shared["target"]["logical_key"] == "docs/cases.csv")
check("6: a shared explicit identifier is HIGH confidence",
      shared["confidence"] == CandidateConfidence.HIGH)

check("6: a database object from the parsed SQL schema is found",
      any(c["candidate_type"] == CandidateType.MENTIONS_DATABASE_OBJECT
          and c["mention"] == "screening_case" for c in related["candidates"]))
check("6: a database-object match is MEDIUM confidence",
      all(c["confidence"] == CandidateConfidence.MEDIUM
          for c in related["candidates"]
          if c["candidate_type"] == CandidateType.MENTIONS_DATABASE_OBJECT))

semantic = [c for c in related["candidates"]
            if c["retrieval_method"] == "semantic-retrieval"]
check("6: a semantic-only candidate is LOW confidence",
      all(c["confidence"] == CandidateConfidence.LOW for c in semantic))
check("6: a semantic-only candidate says so in its reason",
      all("similarity only" in c["reason"] for c in semantic))
check("6: a semantic-only candidate is typed as similar-content",
      all(c["candidate_type"] == CandidateType.SIMILAR_CONTENT
          for c in semantic))

check("6: deterministic candidates ALL outrank semantic-only ones",
      [c["confidence"] for c in related["candidates"]]
      == sorted((c["confidence"] for c in related["candidates"]),
                key=lambda v: {"high": 0, "medium": 1, "low": 2}[v]))
for forbidden in ("implements", "refines", "verifies", "contradicts"):
    check(f"6: no candidate type is '{forbidden}'",
          forbidden not in by_type)
check("6: no candidate is described as confirmed",
      not any("confirmed" in c["reason"].lower()
              for c in related["candidates"]))
check("6: the document itself is never its own candidate",
      not any(c["target"].get("id") == REQ_ASSET
              for c in related["candidates"]))
check("6: a mention with no real target produces no candidate",
      not any(c["target"].get("logical_key") == "does-not-exist"
              for c in related["candidates"]))
check("6: nothing was persisted as a relation",
      not [r for r in db._c().execute(
          "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
          if "relation" in r[0].lower() or "claim" in r[0].lower()])
check("6: the limit is respected",
      runtime.documents.find_related_candidates(WS, REQ_ASSET,
                                                limit=2)["count"] <= 2)

# ---------------------------------------------------------------------------
# 7. Honest empty results
# ---------------------------------------------------------------------------
empty = runtime.documents.search(WS, "ZZQQ-NONEXISTENT-99999", limit=10)
check("7: a query matching nothing exactly returns no fabricated exact hit",
      not any("exact-identifier" in h["retrieval_sources"]
              for h in empty["hits"]))
raised = None
try:
    runtime.documents.search(WS, "   ")
except Exception as exc:
    raised = exc
check("7: an empty query is a typed InvalidRequest",
      type(raised).__name__ == "InvalidRequest")
raised = None
try:
    runtime.documents.find_related_candidates(WS, "a_doesnotexist")
except Exception as exc:
    raised = exc
check("7: candidates for an unknown asset is an honest not-found",
      type(raised).__name__ == "AssetNotFound")

# ---------------------------------------------------------------------------
# 8. Identifier extraction
# ---------------------------------------------------------------------------
check("8: a requirement id is extracted from prose",
      "REQ-NC-017" in document_rag.extract_identifiers(
          "please check REQ-NC-017 today"))
check("8: an API path is extracted",
      "/name-check" in document_rag.extract_identifiers(
          "call POST /name-check now"))
check("8: a dotted config key is extracted",
      "namecheck.review.timeout.minutes" in document_rag.extract_identifiers(
          "set namecheck.review.timeout.minutes to 30"))
check("8: ordinary prose yields no identifiers",
      document_rag.extract_identifiers("how long is the review window") == [])
check("8: an API path matches a sub-path but not a longer sibling",
      document_rag.matches_term("GET /name-check/{caseId}", "/name-check")
      and not document_rag.matches_term("POST /name-checker", "/name-check"))

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
