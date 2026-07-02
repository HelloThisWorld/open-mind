# Open Mind — capability skills

Open Mind's capabilities are organized as **self-contained, callable units**. Each
unit below is documented as a standard `SKILL.md` (name + description frontmatter and
a contract), is implemented through the local REST/API surface, and is partially
available through the MCP tool server (`python -m openmind.mcp_server`).

> **Deterministic, source-traceable, non-fabricating.** Same input → same output.
> Key facts carry source provenance (`file:line`, line range, or source artifact).
> The model never invents definitions, symbols, or edges; unknown inputs return an
> honest "not found," never a guess.

| Skill | Unit | Determinism |
|---|---|---|
| [glossary](glossary/SKILL.md) | verbatim term/acronym index + grounded usage profile | fully deterministic |
| [code-graphs](code-graphs/SKILL.md) | structure / dependency / call / flow graphs from real code | fully deterministic |
| [capability-router](capability-router/SKILL.md) | agent-style routing to a capability | model-assisted with a deterministic floor |

Each capability is small, independently testable (`tests/verify_*.py`), and composes:
the router chooses one, and the glossary's usage profile is built from the
code-graphs structure map.

All three skills are additionally **verified by an independent eval harness** —
the [Agent Skill Verification Template](https://github.com/HelloThisWorld/agent-skill-verification-template)
runs the real implementation through `openmind/skill_bridge.py` (10 runs per
test case; schema / citation / unsupported-claim / tool-call validators; release
gate). Latest results: 250/250 runs passed; see the README's
[Measured Skill Verification](../README.md#measured-skill-verification) section
and the snapshots in [`docs/verification/`](../docs/verification/).

The application also includes optional audited Wikipedia enrichment in
`openmind/wikienrich.py`; installable Claude Skill packaging for that enrichment
is not part of the tracked repository yet.
