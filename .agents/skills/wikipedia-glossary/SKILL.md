---
name: wikipedia-glossary
description: >-
  Enrich an Open Mind project's glossary with standard term definitions from
  Wikipedia. For each already-extracted term, look up its authoritative Wikipedia
  definition, tidy it, and write it back to the glossary as a separate, attributed
  field; terms with no confident match keep their verbatim in-project definition.
  Use when the user asks to add standard / Wikipedia / authoritative definitions to
  an Open Mind glossary, or to "enrich the glossary".
---

# Wikipedia glossary enrichment

Open Mind extracts glossary terms **verbatim** from a project's own authoritative
sources and never fabricates them. This skill **adds** an authoritative *standard*
definition for those terms from Wikipedia — kept strictly separate from, and never
replacing, the in-project definition.

## How enrichment runs (pipeline guarantee + this portable skill)

Open Mind now **runs enrichment automatically** as a deterministic pipeline step:
after a project is learned (and on every server start via a recovery reconciler),
the in-app engine (`openmind/wikienrich.py`) looks up un-attempted terms and writes
results — so enrichment is **guaranteed** without depending on an agent remembering
to act. Wikipedia egress is allowed but **audited** (every call is logged to
`/netlog`); only the enrichment path may egress, and only single glossary terms are
sent — never project source.

**This skill** is the portable, manual/agentic interface to the *same* capability:
run it to (re)enrich on demand, change the disambiguation context, pin a domain
acronym, or enrich from a runtime that isn't the Open Mind app (Codex,
open-claw, etc.). It writes back via `POST /glossary/enrich`; the in-app engine and
this script share the same conservative matching. Use it when you want manual
control or an override — the pipeline covers the automatic case.

Two honesty guarantees are enforced on the Open Mind side:
1. **No invented terms.** A standard definition is only attached to a term the
   project's own sources already surfaced. An unknown term is reported under
   `missing`, never created.
2. **Verbatim definition untouched.** The in-project definition and its
   `file:line` provenance are preserved; the Wikipedia text is a distinct field,
   labelled and footnoted in the UI ("Open Mind used its search skill to extract
   the above from Wikipedia and tidy it").

## When to use

- The user asks to enrich / add standard / authoritative / Wikipedia definitions to
  a project's glossary, or runs this skill by name.
- Typically right after a project finishes learning (its terms are extracted) and
  the user wants richer definitions than the project's own sources provide.

## Compatibility

This is a standard Agent Skill (`SKILL.md` + bundled script) and works in any agent
runtime that reads the skill format (Codex, open-claw, etc.). The bundled
`scripts/enrich_glossary.py` uses **only the Python standard library**, so it runs
without extra installs. If a runtime cannot run Python with network access, use the
**manual path** below with the agent's own web-search/fetch tools.

## Prerequisites

- The Open Mind server is running locally (default `http://127.0.0.1:8077`).
- The target project has been learned (its glossary has terms). You need its
  **project id** (the scope).
- Network access to `*.wikipedia.org` for the duration of the run.

## Procedure (automated — preferred)

1. **Find the project id** if you don't have it:
   ```bash
   python .Codex/skills/wikipedia-glossary/scripts/enrich_glossary.py --list-projects
   ```
   This prints `id  state  name` lines (read from `GET /projects`).

2. **Dry run first** to review matches without writing anything. Pass a `--context`
   domain hint to disambiguate (e.g. the project's subject):
   ```bash
   python .Codex/skills/wikipedia-glossary/scripts/enrich_glossary.py \
     --scope <PROJECT_ID> --context "Apache Kafka distributed systems" --dry-run
   ```
   Each term prints `OK <article>` (confident match) or `-- no confident match`
   (keeps its local definition).

3. **Apply** by re-running without `--dry-run`. Restrict with `--only` or `--limit`
   while iterating:
   ```bash
   python .Codex/skills/wikipedia-glossary/scripts/enrich_glossary.py \
     --scope <PROJECT_ID> --context "Apache Kafka" --only ISR,SASL,ACL
   ```
   The script reads terms from `GET /glossary?scope=<id>`, looks each up on
   Wikipedia, then writes confident matches back via `POST /glossary/enrich`.

4. **Verify** in the UI: open the project's **Glossary** tab. Enriched terms show a
   small green **W** badge in the list; the term page shows the standard definition
   on top, the verbatim in-project definition below it, and the Wikipedia
   attribution footnote at the bottom.

### Useful flags

| Flag | Purpose |
| --- | --- |
| `--base-url` | Open Mind server URL (default `http://127.0.0.1:8077`) |
| `--scope` | project id whose glossary to enrich (required unless `--list-projects`) |
| `--context` | domain hint appended to each Wikipedia search (disambiguation) |
| `--only` | comma-separated subset of terms |
| `--skip-enriched` | skip terms that already have a standard definition (incremental top-up after a re-ingest) |
| `--map` | pin terms to exact article titles, e.g. `'CA=Certificate authority;EC=Elliptic-curve cryptography'` — bypasses auto-matching (you assert the article) |
| `--limit` | cap how many terms to process |
| `--lang` | Wikipedia language (default `en`) |
| `--max-chars` | cap the stored intro length (whole paragraphs up to this; default 2000) |
| `--dry-run` | look up and print, but do not write back |
| `--no-strict` | disable the conservative confidence gate (not recommended) |
| `--sleep` | seconds between lookups (default `0.3`, be polite) |

## Procedure (manual — when you can't run the script)

If the runtime lacks Python-with-network, do the same loop with your own tools:

1. `GET {base}/glossary?scope=<id>` → the list of terms.
2. For each term, web-search `"<term> <context>"`, open the best-matching Wikipedia
   article, and take its **full introduction section** (the lead paragraphs before
   the first heading — a detailed introduction, not just one sentence). **Be
   conservative**: if you are not confident the article is about this exact term
   (especially for acronyms), skip it — a wrong standard definition is worse than
   none. Keep the article's own introduction; don't invent or editorialise.
3. Write confident matches back in one batch:
   ```bash
   curl -s -X POST "{base}/glossary/enrich" -H "Content-Type: application/json" \
     -d '{"scope":"<id>","entries":[
       {"term":"ISR","definition":"...","url":"https://en.wikipedia.org/wiki/...","title":"In-sync replica"}
     ]}'
   ```

## Writeback contract — `POST /glossary/enrich`

```jsonc
// request
{ "scope": "<project id>",
  "entries": [ { "term": "ISR",
                 "definition": "tidied standard definition text",
                 "url": "https://en.wikipedia.org/wiki/...",   // optional
                 "title": "In-sync replica" } ] }              // optional
// response
{ "updated": ["ISR"],     // terms that existed and were enriched
  "missing": [],          // terms not in the glossary (NOT invented)
  "saved_projects": ["<pid>"],
  "retrieved": "2026-06-26" }
```

The server stamps the retrieval date itself. Sending an entry with an empty
`definition` clears any prior enrichment for that term.

## Re-ingest / re-learn workflow (important)

Enrichment is **NOT part of ingest** — Open Mind's ingest pipeline extracts terms
locally and never touches the network or this skill (that is what keeps the app
local-only). The skill is a **separate step you run explicitly** after ingest:

1. Ingest / re-learn the project → terms are (re)extracted locally.
2. Run this skill → terms get standard definitions.
3. Re-ingest later → existing enrichment is **preserved automatically** (Open Mind
   re-attaches it, keyed to the term, across incremental rebuilds). Only *new*
   terms lack enrichment — top them up with one incremental run:
   ```bash
   python .Codex/skills/wikipedia-glossary/scripts/enrich_glossary.py \
     --scope <id> --context "<domain>" --skip-enriched
   ```
   `--skip-enriched` processes only terms that don't already have a standard
   definition, so re-running is cheap.

## Notes & limitations

- **Conservative by design.** Acronyms are matched only by their TITLE INITIALS
  (`ACL` → *Access-control list*) and must look computing-related or match your
  `--context`; the first high-confidence hit wins. A term with no confident match
  keeps its verbatim local definition — the intended "fall back to existing logic"
  behaviour, not a failure. A wrong standard definition is worse than none.
- **Domain acronyms the matcher can't resolve** (e.g. `EC` = Elliptic Curve, which
  auto-matches the wrong "EC-Council") should be **pinned** with `--map` — you
  supply the correct article and the skill fetches its intro. Acronyms that are
  project-internal (e.g. Kafka's `ISR`, `DLT` = Dead Letter Topic) have no
  Wikipedia article and should stay local.
- **Detailed introduction.** The stored text is the article's full introduction
  section (paragraphs before the first heading), capped by `--max-chars` (2000).
- **Idempotent.** Re-running updates the same terms in place.
