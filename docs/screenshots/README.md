# Screenshots

The main `README.md` references three images in this folder. Capture them from the running
app (`./run.ps1` → http://127.0.0.1:8077) on one of the indexed projects and save them here
with the exact filenames below.

| File | Page / view | What it should show |
|---|---|---|
| `glossary.png` | Glossary tab → click a term (e.g. **ISR** or **ACL** on Apache Kafka) | The detail panel: verbatim in-project definition, the `Source` line with `file:line` + a "jump to source" link, the content hash, and the **Usage profile** section (defined-at / used-in / modules / related terms). |
| `graphs.png` | Graphs tab → expand a hub, then click a node | The call/usage mind-map with a node-detail panel open (source location, callers/callees, cross-linked glossary terms). |
| `search.png` | Search box → an identifier query (e.g. `SocketServer`) | The token-precise results: real code snippets, each with its `file:line`, and the exact-token match indicator. |

Tips for clean shots:
- Use a maximized window and the dark theme.
- Prefer Apache Kafka or OpenClaw for visibly rich graphs/definitions.
- Crop to the relevant panel; PNG, ~1400px wide is plenty.

Optional extras (not referenced yet, but strong additions): the source viewer showing a
relative `file:line` path, and a "no authoritative definition found" honest-miss for an
unknown term.
