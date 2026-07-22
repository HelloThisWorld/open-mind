"""Bundle 2.0 Draft (draft.2): trace + conflict files exported on opt-in,
deterministic ordering, no absolute paths, referential integrity, coverage
arithmetic validation, stale snapshots excluded in current-only,
include-history carries them, `.openmind` 1.1.0 unchanged, verifier catches
corruption."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _traceability_helpers import check, finish, make_fixture  # noqa: E402

from openmind.bundle_verify import verify_bundle  # noqa: E402
from openmind.knowledge.bundle import (BUNDLE_SCHEMA_VERSION,  # noqa: E402
                                       export_bundle)
from openmind.runtime import get_runtime  # noqa: E402

runtime = get_runtime()
fx = make_fixture(runtime, "bundle-fix")
pid = fx.pid
trace = fx.trace
trace.set_workspace_policy(pid, policy_name="api-service", actor="fx",
                           note="api lifecycle")
objects = fx.lifecycle()
fx.lifecycle(with_test=False, key_suffix="-B")
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 2 seconds.")
fx.claim(objects["requirement"], "constraint",
         "The check timeout is 5 seconds.")
trace.refresh(pid)
trace.scan_conflicts(pid, actor="scanner")
trace.refresh(pid, force=True)      # a second (historical) snapshot

check("bundle schema advanced to 2.0.0-draft.2",
      BUNDLE_SCHEMA_VERSION == "2.0.0-draft.2")

TRACE_FILES = ("traceability-policies.jsonl", "traceability-runs.jsonl",
               "trace-paths.jsonl", "trace-path-steps.jsonl",
               "trace-gaps.jsonl", "coverage-snapshots.jsonl")
CONFLICT_FILES = ("conflicts.jsonl", "conflict-objects.jsonl",
                  "conflict-evidence.jsonl", "conflict-decisions.jsonl")


def read_jsonl(path: Path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- current-only export with both flags -------------------------------------
out_current = Path(tempfile.mkdtemp(prefix="om_tb1_")) / "bundle"
manifest = export_bundle(pid, str(out_current), current_only=True,
                         include_traceability=True, include_conflicts=True)
check("manifest mode records both flags",
      manifest["mode"]["includeTraceability"]
      and manifest["mode"]["includeConflicts"])
check("every trace file exported",
      all((out_current / name).exists() for name in TRACE_FILES))
check("every conflict file exported",
      all((out_current / name).exists() for name in CONFLICT_FILES))
check("manifest counts the trace records",
      manifest["counts"]["tracePaths"] >= 4
      and manifest["counts"]["coverageSnapshots"] == 1
      and manifest["counts"]["conflicts"] >= 1)
snapshots = read_jsonl(out_current / "coverage-snapshots.jsonl")
check("current-only export contains exactly the latest non-stale snapshot",
      len(snapshots) == 1 and not snapshots[0]["stale_at"])
paths = read_jsonl(out_current / "trace-paths.jsonl")
check("current-only export contains no stale path",
      all(not p["stale_at"] for p in paths))
check("trace paths deterministically ordered",
      paths == sorted(paths, key=lambda p: (p["root_entity_id"],
                                            p["path_kind"], p["id"])))
steps = read_jsonl(out_current / "trace-path-steps.jsonl")
check("steps ordered per path",
      steps == sorted(steps, key=lambda s: (s["trace_path_id"],
                                            s["ordinal"])))
conflicts = read_jsonl(out_current / "conflicts.jsonl")
check("current-only conflicts are open/under-review/accepted-risk only",
      all(c["status"] in ("open", "under-review", "accepted-risk")
          for c in conflicts))
text = "".join((out_current / name).read_text(encoding="utf-8")
               for name in TRACE_FILES + CONFLICT_FILES)
check("no machine-absolute path in the trace/conflict files",
      "D:\\\\" not in text and "C:\\\\Users" not in text
      and "/home/" not in text)

report = verify_bundle(str(out_current))
check("the extended verifier passes the clean bundle: "
      + "; ".join(report.errors[:2]), report.ok)

# coverage arithmetic validated: corrupt a percentage
snapshot_file = out_current / "coverage-snapshots.jsonl"
record = json.loads(snapshot_file.read_text(encoding="utf-8"
                                            ).splitlines()[0])
record["metrics"]["requirements"]["fully_traced"]["percentage"] = 99.99
snapshot_file.write_text(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":")) + "\n",
                         encoding="utf-8")
report2 = verify_bundle(str(out_current))
check("verifier catches inconsistent coverage arithmetic",
      not report2.ok
      and any("does not match" in e for e in report2.errors))

# referential integrity: conflict object pointing nowhere
out_broken = Path(tempfile.mkdtemp(prefix="om_tb2_")) / "bundle"
export_bundle(pid, str(out_broken), current_only=True,
              include_traceability=True, include_conflicts=True)
objects_file = out_broken / "conflict-objects.jsonl"
rows = read_jsonl(objects_file)
rows[0]["object_id"] = "clm_missing"
objects_file.write_text("\n".join(
    json.dumps(r, ensure_ascii=False, sort_keys=True,
               separators=(",", ":")) for r in rows) + "\n",
    encoding="utf-8")
report3 = verify_bundle(str(out_broken))
check("verifier catches a conflict object that resolves nowhere",
      not report3.ok
      and any("references missing" in e for e in report3.errors))

# -- include-history ---------------------------------------------------------
out_history = Path(tempfile.mkdtemp(prefix="om_tb3_")) / "bundle"
export_bundle(pid, str(out_history), current_only=False,
              include_traceability=True, include_conflicts=True)
history_snapshots = read_jsonl(out_history / "coverage-snapshots.jsonl")
check("include-history carries the historical snapshots",
      len(history_snapshots) >= 2
      and any(s["stale_at"] for s in history_snapshots))
report4 = verify_bundle(str(out_history))
check("history bundle verifies too: " + "; ".join(report4.errors[:2]),
      report4.ok)

# -- flags off: Phase 5 layout unchanged -------------------------------------
out_plain = Path(tempfile.mkdtemp(prefix="om_tb4_")) / "bundle"
plain_manifest = export_bundle(pid, str(out_plain), current_only=True)
check("without the flags no trace/conflict file is written",
      not any((out_plain / name).exists()
              for name in TRACE_FILES + CONFLICT_FILES))
check("plain bundle carries no trace counts",
      "tracePaths" not in plain_manifest["counts"])
report5 = verify_bundle(str(out_plain))
check("plain bundle verifies", report5.ok)

# -- .openmind 1.1.0 unchanged ------------------------------------------------
from openmind import artifacts  # noqa: E402
check(".openmind schemaVersion remains 1.1.0",
      artifacts.SCHEMA_VERSION == "1.1.0")
check("bundle draft version is NOT the artifact schema (separate "
      "contracts)", BUNDLE_SCHEMA_VERSION != artifacts.SCHEMA_VERSION)

finish()
