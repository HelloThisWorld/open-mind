"""Extraction prompt family, version 1 (immutable once released).

One template, rendered per concept task with that task's noun and guidance.
The rendered text is deterministic per task type, so its hash is stable and
belongs in the cache key. Guidance is deliberately conservative: extract only
what the text STATES; identifiers verbatim; empty result over invention.
"""
from __future__ import annotations

from .guard_v1 import GUARD

_TEMPLATE = """\
You are an engineering-knowledge extraction analyst working over one bounded \
excerpt of a software project's documentation or source.

TASK
Extract every {concept_title} that the provided content EXPLICITLY states or \
directly evidences. Do not infer {concept_title}s the text does not support, \
do not merge distinct ones, and do not restate the same one twice.

{concept_guidance}

FIELD RULES
- `candidateType` must be "{concept_type}".
- `stableKey`: the explicit identifier from the text (e.g. REQ-XX-001) when \
one exists, verbatim; otherwise an empty string. Never invent identifiers.
- `title`: a short name (a heading, an identifier's subject); may be empty.
- `statement`: the normative content, faithful to the text, self-contained.
- `attributes`: short string facts explicitly present (e.g. priority, actor, \
version); empty object when none.
- `evidence`: the supporting excerpts. Every entry quotes text that appears \
VERBATIM in one `untrustedContent` entry and names that entry's evidenceId.
- `confidenceHint`: your reading confidence (high/medium/low). It is a hint \
only; the system computes final confidence from the evidence.
- `reason`: one or two short sentences naming what in the quoted text makes \
this a {concept_title}.

If the content contains no {concept_title}, return {{"candidates": []}}.

{guard}"""

_CONCEPTS = {
    "requirement": (
        "Requirement",
        "A requirement is a normative statement of what the system SHALL/"
        "MUST/SHOULD do or provide, or an explicitly labelled requirement "
        "entry. Quality targets with explicit values (response time, "
        "throughput, availability) are requirements too."),
    "business-rule": (
        "Business Rule",
        "A business rule is a policy or condition governing business "
        "behavior (eligibility, limits, rounding, approval conditions, "
        "calendar rules) stated as always holding — distinct from a system "
        "feature description."),
    "decision": (
        "Design Decision",
        "A design decision records a CHOICE that was made (technology, "
        "pattern, topology, protocol), ideally with its rationale or "
        "considered alternatives. The rationale belongs in `attributes` "
        "under 'rationale' when the text states one."),
    "constraint": (
        "Constraint",
        "A constraint is an externally imposed limitation the design must "
        "respect: regulatory, budgetary, platform, compatibility, resource "
        "or organizational. It restricts solutions rather than describing "
        "behavior."),
    "interface": (
        "Interface",
        "An interface is a defined interaction surface: an API operation "
        "(record method and path in `attributes`), a message/event contract, "
        "a file exchange, a screen/report handoff, or an integration point "
        "between named systems."),
    "acceptance-criterion": (
        "Acceptance Criterion",
        "An acceptance criterion is a testable condition under which a "
        "requirement or story counts as satisfied — given/when/then clauses, "
        "pass conditions, measurable thresholds tied to acceptance."),
    "failure-mode": (
        "Failure Mode",
        "A failure mode is a described way the system can fail or misbehave, "
        "with its trigger, effect or handling (error conditions, outage "
        "scenarios, degradation paths, recovery expectations)."),
    "data-model": (
        "Data Model element",
        "A data-model element is a described entity, table, record layout, "
        "field set or relationship, including key fields and their meanings. "
        "Record entity/field names verbatim in `attributes`."),
    "workflow": (
        "Workflow",
        "A workflow is an ordered multi-step process the text describes "
        "(actor steps, state transitions, approval chains, batch sequences). "
        "Summarize the steps faithfully in `statement`."),
    "test-case": (
        "Test Case",
        "A test case is a described verification: its target, setup or "
        "precondition, action, and expected result — or an explicitly "
        "labelled test entry with an identifier."),
}


def system_text(concept_type: str) -> str:
    title, guidance = _CONCEPTS[concept_type]
    return _TEMPLATE.format(concept_title=title, concept_guidance=guidance,
                            concept_type=concept_type, guard=GUARD)
