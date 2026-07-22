"""Shared setup for the Phase 6 traceability/conflict acceptance suites.

Import AFTER ``_isolate``. Provides the checked-result recorder, the neutral
NameCheck lifecycle fixture (spec §37) with controlled defect variants, and
small builders over the canonical graph service. No provider is ever
touched; graph objects are created through the same governed service calls
a human would use.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

_results: List[Tuple[str, bool]] = []


def check(desc: str, cond: Any) -> None:
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def finish() -> None:
    bad = [d for d, ok in _results if not ok]
    print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
    sys.exit(1 if bad else 0)


class GraphFixture:
    """Builders over one workspace's canonical graph, all through the
    governed KnowledgeService (evidence-required, actor-attributed)."""

    def __init__(self, runtime, workspace_id: str, evidence_id: str) -> None:
        self.runtime = runtime
        self.pid = workspace_id
        self.evidence_id = evidence_id
        self.knowledge = runtime.knowledge
        self.trace = runtime.traceability

    def entity(self, entity_type: str, key: str, name: str,
               evidence_id: Optional[str] = None) -> Dict[str, Any]:
        return self.knowledge.create_entity(
            self.pid, entity_type=entity_type, canonical_key=key,
            display_name=name,
            evidence=[{"evidence_id": evidence_id or self.evidence_id}],
            actor="fixture", note="fixture")["entity"]

    def claim(self, entity: Dict[str, Any], claim_type: str,
              statement: str,
              evidence_id: Optional[str] = None) -> Dict[str, Any]:
        return self.knowledge.create_claim(
            self.pid, entity_id=entity["id"], claim_type=claim_type,
            statement=statement,
            evidence=[{"evidence_id": evidence_id or self.evidence_id}],
            actor="fixture", note="fixture")["claim"]

    def relation(self, source: Dict[str, Any], target: Dict[str, Any],
                 relation_type: str,
                 state: str = "confirmed") -> Dict[str, Any]:
        return self.knowledge.create_relation(
            self.pid, source_entity_id=source["id"],
            target_entity_id=target["id"], relation_type=relation_type,
            relation_state=state,
            evidence=[{"evidence_id": self.evidence_id}],
            actor="fixture", note="fixture")["relation"]

    def inferred_relation(self, source: Dict[str, Any],
                          target: Dict[str, Any],
                          relation_type: str) -> Dict[str, Any]:
        """An 'inferred' relation written through the store's graph
        transaction (manual creation rightly refuses inferred; the
        deterministic projector is the production writer of these)."""
        from openmind.knowledge import store as kg
        from openmind.knowledge.vocabularies import RevisionAction
        with kg.graph_transaction(
                self.pid, action=RevisionAction.GRAPH_SYNC,
                actor="fixture-analyzer",
                summary="fixture inferred relation") as tx:
            relation = tx.insert_relation(
                source_entity_id=source["id"],
                target_entity_id=target["id"],
                relation_type=relation_type, relation_state="inferred",
                origin="deterministic")
        return relation

    def lifecycle(self, *, with_design: bool = False,
                  with_test: bool = True,
                  with_result: bool = True,
                  key_suffix: str = "") -> Dict[str, Dict[str, Any]]:
        """The compact NameCheck lifecycle: REQ-NC-017 -> (design) ->
        NameCheck API -> NameCheckService -> test case -> test result,
        wired with honest relation types."""
        s = key_suffix
        req = self.entity("requirement", f"requirement:REQ-NC-017{s}",
                          f"REQ-NC-017{s}")
        self.claim(req, "normative-statement",
                   "The name check service shall answer within 2 seconds.")
        out: Dict[str, Dict[str, Any]] = {"requirement": req}
        upstream = req
        if with_design:
            design = self.entity("design", f"design:namecheck-basic{s}",
                                 "NameCheck basic design")
            self.claim(design, "decision-rationale",
                       "The check runs synchronously against the registry.")
            self.relation(design, req, "refines")
            out["design"] = design
            upstream = design
        iface = self.entity("interface", f"interface:POST:/name-check{s}",
                            "NameCheck API")
        self.claim(iface, "interface-contract",
                   "POST /name-check returns the check result.")
        self.relation(iface, upstream, "refines")
        out["interface"] = iface
        code = self.entity("code-component",
                           f"code-component:namecheck-service{s}",
                           "NameCheckService")
        self.claim(code, "behavior", "NameCheckService executes the check.")
        self.relation(code, iface, "implements")
        out["code"] = code
        if with_test:
            test = self.entity("test-case", f"test-case:NC-T-01{s}",
                               "NameCheck test case")
            self.claim(test, "test-expectation",
                       "The check completes and reports a result.")
            self.relation(test, code, "verifies")
            out["test"] = test
            if with_result:
                result = self.entity("test-result",
                                     f"test-result:NC-T-01-run1{s}",
                                     "NameCheck run 1")
                self.claim(result, "test-expectation", "Run 1 passed.")
                self.relation(result, test, "evidenced-by")
                out["result"] = result
        return out


def make_fixture(runtime, name: str = "trace-fix") -> GraphFixture:
    """Workspace + one requirements document (the evidence source) +
    builders. The cheapest deterministic Phase 6 setup."""
    from _knowledge_helpers import find_evidence, make_minimal_workspace
    pid = make_minimal_workspace(runtime, name)
    evidence_id = find_evidence(pid, "REQ-NC-017")
    return GraphFixture(runtime, pid, evidence_id)


def insert_conflict_candidate(pid: str, *, evidence_id: str,
                              quote: str,
                              category: str = "requirement-design",
                              evidence_status: str = "verified",
                              left_candidate_id: Optional[str] = None,
                              right_candidate_id: Optional[str] = None
                              ) -> str:
    """One Phase 4 conflict candidate through the SAME store write the
    Phase 4 runner uses."""
    from openmind.knowledge.identity import quote_hash
    from openmind.semantic import store as semantic_store
    return semantic_store.insert_conflicts(pid, [{
        "category": category,
        "explanation": "fixture conflict candidate",
        "confidence": "medium",
        "evidence_status": evidence_status,
        "left_candidate_id": left_candidate_id,
        "right_candidate_id": right_candidate_id,
        "payload": {"subject_key": "requirement:REQ-NC-017"},
        "evidence": [{"evidence_id": evidence_id, "quote": quote,
                      "quote_hash": quote_hash(quote),
                      "role": "supports"}],
    }])[0]
