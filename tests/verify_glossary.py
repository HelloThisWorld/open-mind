"""Acceptance test for the deterministic glossary tier.

Runs WITHOUT the heavy RAG/web deps — it exercises glossary.build_glossary +
get_glossary directly, proving the acceptance criteria:
  * build-time extraction with provenance (source_file + line_number + content_hash),
  * VERBATIM definitions (never reworded / summarised / model-generated),
  * priority sources (dedicated GLOSSARY file > structured patterns > docs > code),
  * markdown tables, definition lists, "TERM: def" / "TERM - def", acronym forms,
  * exact-token deterministic lookup (no similarity, no model guess),
  * "no authoritative definition found" for absent terms (no guessing),
  * incremental hash-keyed upkeep (an edit re-extracts; an unrelated edit reuses).

Run:  python tests/verify_glossary.py
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)
from openmind import glossary  # noqa: E402

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    ok = bool(cond)
    print(("PASS" if ok else "FAIL"), "-", name, ("" if ok else f"   >> {detail}"))
    _passed += ok
    _failed += (not ok)


def h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


# Two definition sources: a markdown doc ("Expansion (ACRO)") and a Java block
# comment ("ACRO (expansion)" + another "Expansion (ACRO)").
doc = ("# Replication\n"
       "Each partition has a set of In-Sync Replicas (ISR) that are caught up\n"
       "to the leader. The High Watermark (HW) is the last committed offset.\n")
java = ("/** A Log End Offset (LEO) is the offset of the next message.\n"
        " *  SASL (Simple Authentication and Security Layer) secures the broker. */\n"
        "class Log {}\n")
files = [("docs/design.md", doc, h(doc)), ("src/Log.java", java, h(java))]

art = glossary.build_glossary(files)
check("extracted ISR", "ISR" in art["terms"], list(art["terms"]))
check("extracted HW / LEO / SASL", {"HW", "LEO", "SASL"} <= set(art["terms"]), list(art["terms"]))

# 1) deterministic exact-token resolve + VERBATIM definition + provenance
isr = glossary.get_glossary(art, "ISR")
check("ISR -> verbatim 'In-Sync Replicas'",
      isr.get("found") and isr["definition"] == "In-Sync Replicas", isr)
check("ISR provenance is source_file + line_number + content_hash",
      isr.get("source_file") == "docs/design.md" and isr.get("line_number") == 2
      and bool(isr.get("content_hash")), isr)
check("ISR carries its source_kind (acronym)", isr.get("source_kind") == "acronym", isr)
check("reverse form SASL -> Simple Authentication and Security Layer",
      glossary.get_glossary(art, "SASL").get("definition") == "Simple Authentication and Security Layer",
      glossary.get_glossary(art, "SASL"))

# 2) case-insensitive exact-token still resolves deterministically
check("'isr' (lowercase) resolves", glossary.get_glossary(art, "isr").get("found"), "isr")

# 3) absent term -> NOT FOUND, never a guessed definition
miss = glossary.get_glossary(art, "ZZZ")
check("absent term reports not-found (no guess)",
      miss.get("found") is False
      and "no authoritative definition found" in miss.get("message", ""), miss)

# 4) routing: a definition question is classified as an explicit term query
check("'what does ISR mean?' routes explicit",
      glossary.looks_like_term_query("what does ISR mean?") == ("ISR", True),
      glossary.looks_like_term_query("what does ISR mean?"))

# 5) the term list (no `term`) returns term -> definition
listing = glossary.get_glossary(art)
check("term list returns term -> definition",
      listing.get("count") >= 4 and listing["terms"].get("ISR") == "In-Sync Replicas",
      listing)

# 6) structured patterns: markdown table + definition list + "TERM - def" in a
#    DEDICATED glossary file, and that file's definition WINS on a collision.
gloss = ("# Glossary\n\n"
         "| Term | Definition |\n"
         "| --- | --- |\n"
         "| ISR | In-Sync Replica set (authoritative) |\n"
         "| RTT | Round Trip Time |\n\n"
         "DAG\n"
         ": Directed Acyclic Graph\n\n"
         "MTU - Maximum Transmission Unit\n\n"
         "Broker\n"
         ": A node that stores and serves data.\n")
code = ("// ISR (in sync replicas) — a low-priority code-comment mention.\n"
        "class X {}\n")
files2 = [("GLOSSARY.md", gloss, h(gloss)), ("src/X.java", code, h(code)),
          ("docs/design.md", doc, h(doc))]
art2 = glossary.build_glossary(files2)

rtt = glossary.get_glossary(art2, "RTT")
check("markdown table row -> RTT verbatim 'Round Trip Time' (kind=table)",
      rtt.get("definition") == "Round Trip Time" and rtt.get("source_kind") == "table", rtt)
dag = glossary.get_glossary(art2, "DAG")
check("definition list -> DAG 'Directed Acyclic Graph' (kind=deflist)",
      dag.get("definition") == "Directed Acyclic Graph" and dag.get("source_kind") == "deflist", dag)
mtu = glossary.get_glossary(art2, "MTU")
check("'TERM - def' -> MTU 'Maximum Transmission Unit'",
      mtu.get("definition") == "Maximum Transmission Unit", mtu)
broker = glossary.get_glossary(art2, "Broker")
check("single Title-Case headword allowed in a glossary file (Broker)",
      broker.get("found") and broker["definition"] == "A node that stores and serves data.", broker)

# priority: the dedicated GLOSSARY file beats both the docs acronym and the code
# comment for ISR — and the definition is the verbatim glossary text.
isr2 = glossary.get_glossary(art2, "ISR")
check("dedicated glossary file wins on a term collision",
      isr2.get("source_file") == "GLOSSARY.md" and isr2.get("source_kind") == "table"
      and isr2["definition"] == "In-Sync Replica set (authoritative)", isr2)

# 7) incremental upkeep: unrelated edit REUSES, edited source RE-EXTRACTS
java2 = java + "// unrelated trailing comment\n"
art3 = glossary.build_glossary(
    [("docs/design.md", doc, h(doc)), ("src/Log.java", java2, h(java2))], prior=art)
check("unchanged doc source reused (not re-scanned)", art3["stats"]["sources_reused"] >= 1, art3["stats"])
check("changed source re-scanned", art3["stats"]["sources_scanned"] >= 1, art3["stats"])
check("ISR content_hash unchanged after an unrelated edit",
      glossary.get_glossary(art3, "ISR")["content_hash"] == isr["content_hash"],
      art3["terms"].get("ISR"))

doc2 = doc.replace("In-Sync Replicas (ISR)", "In Sync Replicas (ISR)")
art4 = glossary.build_glossary(
    [("docs/design.md", doc2, h(doc2)), ("src/Log.java", java, h(java))], prior=art)
check("editing the definition source refreshes content_hash + verbatim text",
      glossary.get_glossary(art4, "ISR")["content_hash"] == h(doc2)
      and glossary.get_glossary(art4, "ISR")["definition"] == "In Sync Replicas",
      glossary.get_glossary(art4, "ISR"))

# 8) a prior artifact written under an OLD schema is ignored (re-extracted), so a
#    shape change can never carry over a half-migrated entry.
legacy_prior = {"terms": {"OLD": {"term": "OLD", "expansion": "legacy shape"}},
                "source_hashes": {"docs/design.md": h(doc)}}
art5 = glossary.build_glossary(files, prior=legacy_prior)
check("legacy-schema prior is ignored (full re-extract)",
      "OLD" not in art5["terms"] and "ISR" in art5["terms"]
      and art5["stats"]["sources_reused"] == 0, art5["stats"])

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
