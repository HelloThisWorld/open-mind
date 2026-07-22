"""Deterministic conflict detectors: timeout mismatch, unit-normalized
equivalents, missing units never guessed, HTTP method mismatch, API path
normalization, configuration mismatch, database type mismatch,
Requirement–Test comparable mismatch, missing Test = Gap not Conflict,
arbitrary prose never compared, non-comparable facts silent."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.runtime import get_runtime  # noqa: E402
from openmind.traceability.facts import (compare_facts,  # noqa: E402
                                         facts_from_statement)
from openmind.traceability.models import ComparableFact  # noqa: E402

# -- pure normalization ------------------------------------------------------
f = facts_from_statement("The check timeout is 3 seconds.")
check("timeout with explicit unit -> duration in ms",
      f and f[0]["value_type"] == "duration" and f[0]["value"] == 3000
      and f[0]["unit"] == "ms")
f2 = facts_from_statement("timeout = 3000 ms")
check("equivalent duration units normalize to the same value",
      f2 and f2[0]["value"] == 3000 and f2[0]["unit"] == "ms")
f3 = facts_from_statement("The timeout is 3000.")
check("a number without a unit is NOT a duration (unit never guessed)",
      f3 and f3[0]["value_type"] == "integer" and f3[0]["unit"] == "")


def fact(**kw):
    base = dict(subject_key="s", property="timeout", operator="=",
                value=3000, unit="ms", value_type="duration")
    base.update(kw)
    return ComparableFact(**base)


check("united vs unitless comparison is not-comparable",
      compare_facts(fact(), fact(value_type="integer", unit=""))
      == "not-comparable")
check("different value types are not-comparable",
      compare_facts(fact(), fact(value_type="api-path", value="/x"))
      == "not-comparable")
check("equal normalized durations compare equal",
      compare_facts(fact(), fact()) == "equal")
check("different durations compare different",
      compare_facts(fact(), fact(value=5000)) == "different")
check("arbitrary prose produces no facts at all",
      facts_from_statement("The service should feel responsive and "
                           "delight its users in most situations.") == [])

# -- workspace scans ---------------------------------------------------------
runtime = get_runtime()
fx = make_fixture(runtime, "det-fix")
pid = fx.pid
trace = fx.trace
objects = fx.lifecycle()
req, iface = objects["requirement"], objects["interface"]
code, test = objects["code"], objects["test"]


def categories(scan):
    out = {}
    for conflict_id in scan["created"]:
        conflict = trace.get_conflict(pid, conflict_id)
        out.setdefault(conflict["category"], []).append(conflict)
    return out


# timeout mismatch (same subject, seconds vs seconds)
fx.claim(req, "constraint", "The check timeout is 2 seconds.")
fx.claim(req, "constraint", "The check timeout is 5 seconds.")
# unit-normalized equivalent: 2 seconds == 2000 ms -> NO conflict
fx.claim(req, "constraint", "The check timeout is 2000 ms.")
# unitless number: never compared against the united ones
fx.claim(req, "constraint", "The check timeout is 9999.")
scan1 = trace.scan_conflicts(pid, actor="scanner")
by_cat = categories(scan1)
doc_doc = by_cat.get("document-document", [])
check("explicit timeout mismatch detected",
      any("timeout" in c["metadata"].get("value_type", "")
          or "timeout" in c["title"] for c in doc_doc))
values_in_conflicts = {(c["metadata"].get("left_value"),
                        c["metadata"].get("right_value"))
                       for c in doc_doc}
check("2 seconds vs 2000 ms did NOT conflict (units normalize equal)",
      not any({"2000ms", "2000ms"} == {left, right}
              for left, right in values_in_conflicts))
check("unitless 9999 never conflicts with united values "
      "(missing unit not guessed)",
      not any("9999" in str(left) or "9999" in str(right)
              for left, right in values_in_conflicts))
check("2s vs 5s conflict present",
      any({str(left), str(right)} == {"2000ms", "5000ms"}
          for left, right in values_in_conflicts))

# requirement-design mismatch: related design with a different timeout
# (stated in the closed comparable form — "timeout is <n> <unit>"; freer
# phrasings are deliberately not extracted)
design = fx.entity("design", "design:namecheck-basic", "Basic design")
fx.claim(design, "decision-rationale",
         "In this design the check timeout is 4 seconds.")
fx.relation(design, req, "refines")
scan2 = trace.scan_conflicts(pid, actor="scanner")
by_cat2 = categories(scan2)
check("requirement-design mismatch detected",
      len(by_cat2.get("requirement-design", [])) >= 1)

# specification-code drift: documented config vs actual configuration
fx.claim(req, "constraint", "The service reads namecheck.timeout=3000.")
config = fx.entity("configuration",
                   "configuration:asset:a_fix:namecheck.timeout",
                   "namecheck.timeout")
fx.claim(config, "constraint",
         "The deployed value is namecheck.timeout=5000.")
scan3 = trace.scan_conflicts(pid, actor="scanner")
by_cat3 = categories(scan3)
check("configuration mismatch detected (specification-code)",
      any(c["subject_key"] == "namecheck.timeout"
          for c in by_cat3.get("specification-code", [])))

# HTTP method mismatch: documented POST vs canonical PUT interface
fx.claim(req, "interface-contract",
         "The check is invoked as POST /name-check.")
put_iface = fx.entity("interface", "interface:PUT:/name-check",
                      "NameCheck API v2")
fx.claim(put_iface, "interface-contract", "The v2 operation.")
scan4 = trace.scan_conflicts(pid, actor="scanner")
by_cat4 = categories(scan4)
method_conflicts = by_cat4.get("interface-schema", [])
check("HTTP method mismatch detected for the same normalized path",
      any(c["subject_key"] == "/name-check" for c in method_conflicts))

# API path normalization: /name-check/ == /name-check -> no extra conflict
fx.claim(req, "interface-contract",
         "Clients may also call POST /name-check/.")
scan5 = trace.scan_conflicts(pid, actor="scanner")
check("trailing-slash path normalizes (no false path conflict "
      "between the two POST claims)",
      not any("/name-check/" in str(c.get("subject_key"))
              for c in categories(scan5).get("interface-schema", [])))

# database type mismatch: two field-type declarations
schema_a = fx.entity("data-model", "data-model:namecheck-request",
                     "NameCheck request schema")
fx.claim(schema_a, "data-definition",
         "Field FULL_NAME has type varchar(200).")
fx.claim(schema_a, "data-definition", "Field FULL_NAME has type text.")
scan6 = trace.scan_conflicts(pid, actor="scanner")
by_cat6 = categories(scan6)
check("database/schema type mismatch detected",
      any(c["subject_key"] == "full_name"
          for c in by_cat6.get("interface-schema", [])))

# requirement-test mismatch via the verifies chain
fx.claim(req, "constraint", "The maximum latency is 3000 ms.")
fx.claim(test, "test-expectation",
         "The acceptance threshold is 5000 ms.")
scan7 = trace.scan_conflicts(pid, actor="scanner")
by_cat7 = categories(scan7)
check("Requirement-Test comparable mismatch detected",
      len(by_cat7.get("requirement-test", [])) >= 1)

# missing test is a GAP, never a conflict
no_test = fx.lifecycle(with_test=False, key_suffix="-NT")
scan8 = trace.scan_conflicts(pid, actor="scanner")
trace_result = trace.trace_requirement(pid, no_test["requirement"]["id"])
check("missing Test creates a Gap",
      any(g["gap_type"] == "missing-test"
          for g in trace_result["gaps"]))
check("missing Test creates NO conflict",
      not any("NT" in str(trace.get_conflict(pid, cid).get("subject_key"))
              for cid in scan8["created"]))

# prose claims on related entities never conflict
fx.claim(req, "implementation-note",
         "The team prefers clear error messages over clever ones.")
fx.claim(design, "implementation-note",
         "Error messages should sound helpful and friendly.")
scan9 = trace.scan_conflicts(pid, actor="scanner")
check("arbitrary prose is not compared (no new conflicts from notes)",
      not scan9["created"])

finish()
