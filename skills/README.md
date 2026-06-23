# Open Mind — capability skills

Open Mind's capabilities are organized as **self-contained, callable units**. Each
unit below is documented as a standard `SKILL.md` (name + description frontmatter and
a contract), is invocable two ways — as an **MCP tool** (`python -m openmind.mcp_server`)
and as a **REST endpoint** (`./run.ps1`) — and obeys the same discipline:

> **Deterministic, source-traceable, non-fabricating.** Same input → same output.
> Every fact links to a real `file:line`. The model never invents definitions,
> symbols, or edges; unknown inputs return an honest "not found," never a guess.

| Skill | Unit | Determinism |
|---|---|---|
| [glossary](glossary/SKILL.md) | verbatim term/acronym index + grounded usage profile | fully deterministic |
| [code-graphs](code-graphs/SKILL.md) | structure / dependency / call / flow graphs from real code | fully deterministic |
| [capability-router](capability-router/SKILL.md) | agent-style routing to a capability | model-assisted with a deterministic floor |
| [external-definitions](../.claude/skills/wikipedia-glossary/SKILL.md) | authoritative external definitions, pinned with source + timestamp | deterministic write-back; egress audited |

Each capability is small, independently testable (`tests/verify_*.py`), and composes:
the router chooses one; the glossary's usage profile is built from the code-graphs'
structure map; external definitions attach to glossary terms as a separate, attributed
field — never replacing the in-project verbatim definition.
