"""The prompt builder — where the injection boundary is ENFORCED, not hoped.

Layout of every semantic request, for every task and every provider:

* **system/developer message**: task instructions + the shared guard text
  from the versioned prompt modules. Contains NO project content, ever.
* **user message**: one JSON packet::

      {
        "task": "...",
        "allowedEvidenceIds": ["e_..."],
        "context": { ...structural metadata only... },
        "untrustedContent": [
          {"evidenceId": "e_...", "locator": {...}, "text": "..."}
        ]
      }

  ``untrustedContent[].text`` is the ONLY field that ever carries project
  text. ``context`` is restricted to structural metadata (heading paths,
  symbols, glossary TERM NAMES, deterministic pair descriptors) — an
  assertion checked by :func:`assert_packet_shape`, which the tests run over
  hostile fixtures.

No tools are attached, no chain-of-thought is requested, and the only
"reason" the schema admits is a bounded, evidence-tied sentence. Prompt text
is resolved from the IMMUTABLE versioned modules; :func:`prompt_hash` is the
SHA-256 of the exact rendered system text and goes into every cache key and
provenance record.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Sequence

from .errors import ProviderConfigurationError
from .tasks import TaskDefinition

#: Bound on one untrusted entry's text; longer content is truncated with an
#: explicit marker (the verifier only needs quotes to be findable in what was
#: actually SENT, and what was sent is exactly this).
MAX_UNTRUSTED_ENTRY_CHARS = 8_000
MAX_UNTRUSTED_ENTRIES = 24

#: Keys the ``context`` object may carry — structural metadata only.
ALLOWED_CONTEXT_KEYS = frozenset({
    "headingPath", "symbol", "segmentType", "logicalKey", "assetType",
    "glossaryTerms", "neighborsIncluded", "identifiers", "pairs",
    "inventory", "existingMentions",
})


def _render_system_text(task: TaskDefinition) -> str:
    """The versioned system text for a task. Dispatch is by prompt family +
    version; an unknown combination is a configuration error, never a silent
    default prompt."""
    version = task.prompt_version
    if version == "1":
        if task.schema_name == "engineering-candidates":
            from .prompt_texts.extraction_v1 import system_text
            concept = next(iter(task.allowed_candidate_types))
            return system_text(concept)
        if task.task_type == "document-classification":
            from .prompt_texts.document_tasks_v1 import DOCUMENT_CLASSIFICATION
            return DOCUMENT_CLASSIFICATION
        if task.task_type == "revision-status-inference":
            from .prompt_texts.document_tasks_v1 import REVISION_STATUS
            return REVISION_STATUS
        if task.task_type == "relation-candidate-analysis":
            from .prompt_texts.pair_analysis_v1 import RELATION_ANALYSIS
            return RELATION_ANALYSIS
        if task.task_type == "conflict-candidate-analysis":
            from .prompt_texts.pair_analysis_v1 import CONFLICT_ANALYSIS
            return CONFLICT_ANALYSIS
        if task.task_type == "project-lens-induction":
            from .prompt_texts.lens_induction_v1 import LENS_INDUCTION
            return LENS_INDUCTION
    raise ProviderConfigurationError(
        f"no prompt registered for task {task.task_type!r} version "
        f"{version!r}", details={"task": task.task_type,
                                 "prompt_version": version})


def system_instructions(task: TaskDefinition) -> str:
    return _render_system_text(task)


def prompt_hash(task: TaskDefinition) -> str:
    """SHA-256 of the exact rendered system text — the released-prompt
    identity used by cache keys and provenance."""
    return hashlib.sha256(
        _render_system_text(task).encode("utf-8")).hexdigest()


def build_input_packet(task: TaskDefinition,
                       untrusted: Sequence[Dict[str, Any]],
                       context: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the user-message packet.

    *untrusted*: ``{evidenceId, locator, text}`` entries — every piece of
    project text the model may see. *context*: structural metadata only,
    validated against :data:`ALLOWED_CONTEXT_KEYS`.
    """
    entries: List[Dict[str, Any]] = []
    for item in list(untrusted)[:MAX_UNTRUSTED_ENTRIES]:
        text = str(item.get("text") or "")
        if len(text) > MAX_UNTRUSTED_ENTRY_CHARS:
            text = text[:MAX_UNTRUSTED_ENTRY_CHARS] + "\n[truncated]"
        entries.append({
            "evidenceId": str(item.get("evidenceId") or ""),
            "locator": dict(item.get("locator") or {}),
            "text": text,
        })
    packet = {
        "task": task.task_type,
        "allowedEvidenceIds": [e["evidenceId"] for e in entries
                               if e["evidenceId"]],
        "context": {k: v for k, v in (context or {}).items()
                    if k in ALLOWED_CONTEXT_KEYS},
        "untrustedContent": entries,
    }
    assert_packet_shape(packet)
    return packet


def assert_packet_shape(packet: Dict[str, Any]) -> None:
    """The structural injection-boundary invariant, enforced at build time
    (and re-runnable by tests over any packet):

    1. only the four packet keys exist;
    2. ``context`` carries no free text field outside the allowlist;
    3. every ``untrustedContent`` entry is {evidenceId, locator, text};
    4. ``allowedEvidenceIds`` exactly matches the entries' ids.
    """
    unknown = sorted(set(packet) - {"task", "allowedEvidenceIds", "context",
                                    "untrustedContent"})
    if unknown:
        raise ProviderConfigurationError(
            f"input packet has unexpected top-level keys: {unknown}")
    bad_context = sorted(set(packet.get("context") or {})
                         - ALLOWED_CONTEXT_KEYS)
    if bad_context:
        raise ProviderConfigurationError(
            f"packet context carries non-structural keys: {bad_context}")
    ids = []
    for i, entry in enumerate(packet.get("untrustedContent") or []):
        extra = sorted(set(entry) - {"evidenceId", "locator", "text"})
        if extra:
            raise ProviderConfigurationError(
                f"untrustedContent[{i}] has unexpected keys: {extra}")
        if entry.get("evidenceId"):
            ids.append(entry["evidenceId"])
    if sorted(set(packet.get("allowedEvidenceIds") or [])) != sorted(set(ids)):
        raise ProviderConfigurationError(
            "allowedEvidenceIds does not match untrustedContent entries")


__all__ = ["system_instructions", "prompt_hash", "build_input_packet",
           "assert_packet_shape", "ALLOWED_CONTEXT_KEYS",
           "MAX_UNTRUSTED_ENTRY_CHARS", "MAX_UNTRUSTED_ENTRIES"]
