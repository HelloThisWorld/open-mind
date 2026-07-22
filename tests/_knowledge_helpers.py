"""Shared setup for the Phase 5 knowledge-graph acceptance suites.

Import AFTER ``_isolate``. Provides: the checked-result recorder, neutral
invented fixture content (spec §38), workspace builders, a confirmed-
candidate factory driven by the Phase 4 MOCK provider (no real provider API
is ever called), and small read helpers.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_results: List[Tuple[str, bool]] = []


def check(desc: str, cond: Any) -> None:
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def finish() -> None:
    bad = [d for d, ok in _results if not ok]
    print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------------------
# Neutral fixture content (invented; spec §38)
# ---------------------------------------------------------------------------
REQUIREMENTS_MD = """# Neutral Component Requirements

## Scope
REQ-NC-017: The name check service shall answer within 2 seconds.

REQ-NC-018: Failed name checks must be retried three times.

## Notes
The archiver should compress completed batches nightly.
"""

DESIGN_MD = """# Neutral Component Design

## Interfaces
The NameCheck API exposes POST /name-check returning the check result.

REQ-NC-017 is implemented by the NameCheckService with a 2 second budget.
"""

OPENAPI_YAML = """openapi: 3.0.0
info:
  title: NameCheck API
  version: "1.0"
paths:
  /name-check:
    post:
      operationId: runNameCheck
      summary: Run a name check
      responses:
        "200":
          description: check result
components:
  schemas:
    NameCheckResult:
      type: object
      properties:
        status:
          type: string
"""

PASSENGER_SQL = """CREATE TABLE PASSENGER (
    PASSENGER_ID INTEGER PRIMARY KEY,
    FULL_NAME VARCHAR(200) NOT NULL,
    CHECK_STATUS VARCHAR(20)
);

CREATE TABLE CHECK_AUDIT (
    AUDIT_ID INTEGER PRIMARY KEY,
    PASSENGER_ID INTEGER NOT NULL,
    RESULT VARCHAR(20)
);
"""

JAVA_SERVICE = """package org.example.check;

public class NameCheckService {
    private final int timeoutMillis = 2000;

    public NameCheckService() {
    }

    public String execute(String request) {
        Recorder recorder = new Recorder();
        recorder.record(request);
        return normalize(request);
    }

    private String normalize(String raw) {
        return raw == null ? "" : raw.trim().toUpperCase();
    }
}
"""

JAVA_RECORDER = """package org.example.check;

public class Recorder {
    public void record(String entry) {
        System.out.println(entry);
    }
}
"""

#: A SECOND class with the SAME name in another directory — what makes the
#: name-based call edge from the service AMBIGUOUS (two possible owners for
#: the referenced class name ``Recorder``).
JAVA_RECORDER_ALT = """package org.example.audit;

public class Recorder {
    public void record(String entry) {
        System.err.println(entry);
    }
}
"""

JAVA_TEST = """package org.example.check;

public class NameCheckServiceTest {
    public void shouldNormalize() {
        NameCheckService service = new NameCheckService();
        service.execute(" alpha ");
    }
}
"""

CONFIG_PROPERTIES = """namecheck.timeout=2000
namecheck.retry.count=3
"""


def make_minimal_workspace(runtime, name: str = "kg-min") -> str:
    """A workspace with ONE requirements document — the cheapest source of
    real Evidence ids for manual-creation tests."""
    workspace = runtime.workspaces.create(name)
    pid = workspace["id"]
    src = Path(tempfile.mkdtemp(prefix="om_kgfix_"))
    path = src / "requirements.md"
    path.write_text(REQUIREMENTS_MD, encoding="utf-8")
    result = runtime.documents.add_document(pid, str(path), wait=True,
                                            timeout=180)
    assert result.get("status") in ("new_asset", "revision", "duplicate"), \
        f"fixture import failed: {result}"
    return pid


def make_source_workspace(runtime, name: str = "kg-src",
                          with_documents: bool = True) -> Tuple[str, Path]:
    """A workspace over a small neutral source tree (Java service + test +
    config), plus (optionally) OpenAPI, SQL and requirements documents.
    Returns (workspace_id, source_root)."""
    workspace = runtime.workspaces.create(name)
    pid = workspace["id"]
    src = Path(tempfile.mkdtemp(prefix="om_kgsrc_"))
    (src / "src").mkdir()
    (src / "src" / "NameCheckService.java").write_text(JAVA_SERVICE,
                                                      encoding="utf-8")
    (src / "src" / "Recorder.java").write_text(JAVA_RECORDER,
                                               encoding="utf-8")
    (src / "alt").mkdir()
    (src / "alt" / "Recorder.java").write_text(JAVA_RECORDER_ALT,
                                               encoding="utf-8")
    (src / "test").mkdir()
    (src / "test" / "NameCheckServiceTest.java").write_text(JAVA_TEST,
                                                            encoding="utf-8")
    (src / "namecheck.properties").write_text(CONFIG_PROPERTIES,
                                              encoding="utf-8")
    runtime.workspaces.add_path(pid, str(src), [])
    runtime.ingest.start(pid, wait=True, timeout=300)
    if with_documents:
        docs = Path(tempfile.mkdtemp(prefix="om_kgdoc_"))
        for filename, content in (("requirements.md", REQUIREMENTS_MD),
                                  ("design.md", DESIGN_MD),
                                  ("name-check-api.yaml", OPENAPI_YAML),
                                  ("passenger.sql", PASSENGER_SQL)):
            path = docs / filename
            path.write_text(content, encoding="utf-8")
            result = runtime.documents.add_document(pid, str(path),
                                                    wait=True, timeout=180)
            assert result.get("status") in ("new_asset", "revision",
                                            "duplicate"), \
                f"fixture import failed for {filename}: {result}"
    return pid, src


# ---------------------------------------------------------------------------
# Evidence + candidate factories (mock provider only)
# ---------------------------------------------------------------------------
def evidence_map(pid: str) -> Dict[str, str]:
    """evidence_id -> immutable text for every current-revision segment."""
    from openmind import db
    from openmind.semantic.context import resolve_evidence_text
    out: Dict[str, str] = {}
    for asset in db.list_assets(pid, state="active", limit=1000):
        revision_id = asset.get("current_revision_id")
        if not revision_id:
            continue
        for segment in db.list_segments(pid, revision_id, limit=500):
            ev = db.get_evidence_for_segment(pid, segment["id"])
            if not ev:
                continue
            text = resolve_evidence_text(pid, ev["id"])
            if text:
                out[ev["id"]] = text
    return out


def find_evidence(pid: str, containing: str) -> str:
    for evidence_id, text in evidence_map(pid).items():
        if containing in text:
            return evidence_id
    raise AssertionError(f"no evidence contains {containing!r}")


def mock_profile(name: str = "mock-kg",
                 responses: Optional[Dict[str, Any]] = None):
    from openmind.semantic.models import ProviderProfile
    from openmind.semantic.providers import profiles
    profile = ProviderProfile(name=name, kind="mock",
                              metadata={"responses": responses or {}})
    profiles.upsert_profile(profile)
    return profile


def requirement_response(evidence_id: str, *, stable_key: str = "REQ-NC-017",
                         statement: str = "The name check service shall "
                                          "answer within 2 seconds.",
                         quote: str = "shall answer within 2 seconds"
                         ) -> Dict[str, Any]:
    return {"candidates": [{
        "candidateType": "requirement", "stableKey": stable_key,
        "title": f"{stable_key} response time",
        "statement": statement, "attributes": {},
        "evidence": [{"evidenceId": evidence_id, "quote": quote}],
        "confidenceHint": "medium",
        "reason": "explicit normative statement with an identifier"}]}


def make_confirmed_candidate(runtime, pid: str, *,
                             containing: str = "REQ-NC-017",
                             response: Optional[Dict[str, Any]] = None,
                             confirm: bool = True) -> str:
    """Run one mock requirement extraction and (optionally) confirm the
    candidate. Returns the candidate id."""
    evidence_id = find_evidence(pid, containing)
    mock_profile(responses={
        "requirement-extraction": response
        or requirement_response(evidence_id)})
    runtime.semantic.set_policy(pid, provider_profile="mock-kg")
    runtime.semantic.start_analysis(
        pid, task_types=["requirement-extraction"],
        scope={"kind": "documents"}, wait=True, timeout=180)
    candidates = runtime.semantic.list_candidates(
        pid, candidate_type="requirement")["candidates"]
    assert candidates, "mock analysis produced no candidate"
    candidate_id = candidates[0]["id"]
    if confirm:
        runtime.semantic.review_candidate(pid, candidate_id,
                                          decision="confirm",
                                          reviewer="fixture-reviewer")
    return candidate_id


def insert_relation_candidate(pid: str, *, source_ref: Dict[str, Any],
                              target_ref: Dict[str, Any],
                              relation_type: str = "implements",
                              evidence_id: str = "", quote: str = "",
                              evidence_status: str = "verified",
                              source_candidate_id: Optional[str] = None,
                              target_candidate_id: Optional[str] = None
                              ) -> str:
    """Insert one relation candidate through the SAME store write the Phase 4
    runner uses (the mock relation pipeline needs multi-candidate setups this
    factory sidesteps)."""
    from openmind.knowledge.identity import quote_hash
    from openmind.semantic import store as semantic_store
    evidence = []
    if evidence_id:
        evidence = [{"evidence_id": evidence_id, "quote": quote,
                     "quote_hash": quote_hash(quote), "role": "supports"}]
    ids = semantic_store.insert_relations(pid, [{
        "relation_type": relation_type, "source_ref": source_ref,
        "target_ref": target_ref,
        "source_candidate_id": source_candidate_id,
        "target_candidate_id": target_candidate_id,
        "reason": "fixture", "confidence": "medium",
        "evidence_status": evidence_status, "evidence": evidence}])
    return ids[0]


def confirm_relation_candidate(runtime, pid: str, relation_id: str) -> None:
    runtime.semantic.review_relation_candidate(
        pid, relation_id, decision="confirm", reviewer="fixture-reviewer")


def entity_by_key(pid: str, entity_type: str,
                  canonical_key: str) -> Optional[Dict[str, Any]]:
    from openmind.knowledge import store
    return store.find_entity_by_key(pid, entity_type, canonical_key)


def entities_of_type(pid: str, entity_type: str) -> List[Dict[str, Any]]:
    from openmind.knowledge import store
    return store.list_entities(pid, entity_type=entity_type,
                               lifecycle_status=None, limit=1000)
