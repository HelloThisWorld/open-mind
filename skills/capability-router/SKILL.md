---
name: capability-router
description: Route a request to exactly one Open Mind capability (glossary / structure / search). A local model may decide when ready, but its choice is validated against the known capability set and falls back to a deterministic if-else floor — the model never invents or overrides a capability unchecked.
---

# Capability router — agent-style invocation with graceful degradation

## What it does
Given a natural-language or identifier query, decides which capability should handle
it and (optionally) invokes it:
- `glossary` — define a term/acronym,
- `structure` — callers/callees, dependencies, modules, flow,
- `search` — find code by concept or identifier.

## How the decision is made (the engineering point)
1. A **deterministic if-else classifier** always runs first (`_heuristic_route`) — the
   always-available floor (e.g. a bare identifier or "what is X" → glossary; "who calls
   X" / "dependency graph" → structure; otherwise → search).
2. **If a local model is ready**, it is asked to classify into one capability word. Its
   answer is **validated against the capability set** and accepted ONLY if valid.
3. An **unavailable, slow, or off-spec model silently degrades** to the deterministic
   choice. The model can never select a capability outside the known set or override
   the floor unchecked.

The response is a full trace: `{capability, decided_by, deterministic_fallback, reason}`
— so the routing is auditable and behaviour never *depends* on the model being right.

## Invocation
- **MCP tools:** `route(query)` (decision + trace), `dispatch(scope, query)` (route then
  invoke the capability, returning its result + trace).
- **REST:** `GET /route?query=`, `GET /dispatch?scope=&query=`.

## Implementation / verification
`openmind/router.py`. The deterministic floor is covered by routing a fixed query set
with `use_model=False`; readiness gating reuses `openmind.llm_client.is_ready()`.
