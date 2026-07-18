"""Application service layer — workspace, job, ingest, export and health.

The bounded-wait cases use the fake clock and a fake job reader from
``openmind.ports``. That is what those ports are for: driving the wait state
machine through queued -> running -> done (and into a timeout) deterministically
and in milliseconds, instead of spinning a real worker and sleeping.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

from typing import Any, Dict, List, Optional  # noqa: E402

from openmind.domain.errors import (InvalidRequest, JobNotFound,  # noqa: E402
                                    OperationTimeout, WorkspaceNotFound)
from openmind.domain.types import HealthCheck, HealthReport  # noqa: E402
from openmind.ports.runtime_ports import FakeClock  # noqa: E402
from openmind.runtime import OpenMindRuntime  # noqa: E402
from openmind.services.job_service import JobService  # noqa: E402

_results = []


def check(desc, cond, extra=""):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc
          + (f"  [{extra}]" if extra and not cond else ""))


def raises(exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return exc
    except Exception as exc:            # wrong type — report it, don't pass
        return ("WRONG", type(exc).__name__, str(exc))
    return None


FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "sample-repo")

runtime = OpenMindRuntime()
runtime.bootstrap()
ws = runtime.workspaces

# ---------------------------------------------------------------------------
# WorkspaceService — creation
# ---------------------------------------------------------------------------
created = ws.create("Service Demo")
check("create() returns a workspace record", bool(created.get("id")))
check("create() uses the existing project id shape",
      created["id"].startswith("p_"))
check("a new workspace starts in 'init'", created["state"] == "init")
check("create() does not register a path when none is given",
      created["paths"] == [])

err = raises(InvalidRequest, ws.create, "   ")
check("create() rejects a blank name", isinstance(err, InvalidRequest), repr(err))
check("the blank-name error names the field",
      isinstance(err, InvalidRequest) and err.details.get("field") == "name")

with_path = ws.create("With Path", path=FIXTURE)
check("create(path=...) registers the initial path",
      len(with_path["paths"]) == 1
      and with_path["paths"][0]["path"] == FIXTURE)

# ---------------------------------------------------------------------------
# WorkspaceService — reads and honest not-found
# ---------------------------------------------------------------------------
check("get() round-trips", ws.get(created["id"])["id"] == created["id"])
check("exists() is True for a real workspace", ws.exists(created["id"]))
check("exists() is False for an unknown id", not ws.exists("p_does_not_exist"))

err = raises(WorkspaceNotFound, ws.get, "p_does_not_exist")
check("get() raises WorkspaceNotFound, not a bare KeyError/None",
      isinstance(err, WorkspaceNotFound), repr(err))
check("WorkspaceNotFound maps to HTTP 404", getattr(err, "http_status", None) == 404)
check("WorkspaceNotFound maps to CLI exit 1", getattr(err, "exit_code", None) == 1)
check("WorkspaceNotFound is machine-readable",
      err.as_dict()["code"] == "workspace_not_found"
      and err.as_dict()["details"]["workspace_id"] == "p_does_not_exist")

ids = {w["id"] for w in ws.list()}
check("list() includes every created workspace",
      {created["id"], with_path["id"]} <= ids)

# ---------------------------------------------------------------------------
# WorkspaceService — path registration
# ---------------------------------------------------------------------------
updated = ws.add_path(created["id"], FIXTURE, ["target", "build"])
check("add_path() registers the path", len(updated["paths"]) == 1)
check("add_path() stores the exclude set",
      updated["paths"][0]["exclude"] == ["build", "target"])

re_added = ws.add_path(created["id"], FIXTURE, ["node_modules"])
check("add_path() UPDATES an existing path instead of duplicating it",
      len(re_added["paths"]) == 1
      and re_added["paths"][0]["exclude"] == ["node_modules"])

err = raises(WorkspaceNotFound, ws.add_path, "p_nope", FIXTURE)
check("add_path() 404s before touching the sidecar",
      isinstance(err, WorkspaceNotFound))
err = raises(InvalidRequest, ws.add_path, created["id"], "  ")
check("add_path() rejects a blank path", isinstance(err, InvalidRequest))

check("paths() reads back what add_path wrote",
      ws.paths(created["id"])[0]["path"] == FIXTURE)
check("source_root() resolves the machine-local root",
      ws.source_root(created["id"]) == FIXTURE.replace("\\", "/"))

removed = ws.remove_path(created["id"], FIXTURE)
check("remove_path() removes the path", removed["paths"] == [])
err = raises(WorkspaceNotFound, ws.remove_path, "p_nope", FIXTURE)
check("remove_path() 404s for an unknown workspace",
      isinstance(err, WorkspaceNotFound))

# ---------------------------------------------------------------------------
# WorkspaceService — machine-local storage (zero origin traces)
# ---------------------------------------------------------------------------
import sqlite3  # noqa: E402

from openmind import config  # noqa: E402

raw = sqlite3.connect(str(config.DB_PATH))
rows = raw.execute("SELECT paths_json FROM projects").fetchall()
raw.close()
check("the portable database stores NO absolute source path",
      all((r[0] or "[]") == "[]" for r in rows),
      f"rows={rows}")

# ---------------------------------------------------------------------------
# WorkspaceService — template selection
# ---------------------------------------------------------------------------
selection = ws.template_selection(created["id"])
check("template_selection() reports no override on a fresh workspace",
      selection["override"] is None and selection["effective"] is None)

err = raises(InvalidRequest, ws.set_template, created["id"], "no-such-template")
check("set_template() rejects an unknown template",
      isinstance(err, InvalidRequest), repr(err))
check("the unknown-template error lists what IS available",
      isinstance(err, InvalidRequest) and err.details.get("available"))
check("InvalidRequest maps to HTTP 400 / CLI exit 2",
      err.http_status == 400 and err.exit_code == 2)

from openmind import templates  # noqa: E402

available = [t["name"] for t in templates.list_templates() if t.get("valid", True)]
if available:
    picked = available[0]
    after = ws.set_template(created["id"], picked.upper())
    check("set_template() accepts a valid template and lower-cases it",
          after["override"] == picked.lower(), str(after))
    check("set_template() makes the override effective",
          after["effective"] == picked.lower()
          and after["effective_source"] == "override")
    cleared = ws.set_template(created["id"], None)
    check("set_template(None) clears the override",
          cleared["override"] is None)
else:
    check("template profiles are available to test selection", False,
          "no valid built-in templates found")

# ---------------------------------------------------------------------------
# WorkspaceService — status
# ---------------------------------------------------------------------------
status = ws.status(with_path["id"])
check("status() reports the workspace record", status["workspace"]["id"] == with_path["id"])
check("status() reports registered paths", len(status["paths"]) == 1)
check("status() reports state", status["state"] == "init")
check("status() reports template selection", "effective" in status["template"])
for key in ("indexed_files", "code_chunks", "solved_cases", "glossary_terms"):
    check(f"status() reports the {key} count", key in status["counts"])
check("status() counts are integers (or None for an unreadable store)",
      all(v is None or isinstance(v, int) for v in status["counts"].values()))


# ---------------------------------------------------------------------------
# JobService — bounded waiting, driven by fakes
# ---------------------------------------------------------------------------
class FakeReader:
    """A job reader that walks a scripted sequence of statuses."""

    def __init__(self, statuses: List[str]) -> None:
        self.statuses = list(statuses)
        self.reads = 0

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        if job_id != "job_1":
            return None
        self.reads += 1
        index = min(self.reads - 1, len(self.statuses) - 1)
        return {"job_id": job_id, "status": self.statuses[index],
                "type": "ingest", "progress": {"files_done": self.reads},
                "error": ""}

    def list_jobs(self, project_ids=None, status=None):
        return []

    def active_jobs(self):
        return []


clock = FakeClock()
svc = JobService(reader=FakeReader(["queued", "running", "running", "done"]),
                 engine=None, clock=clock)
outcome = svc.wait_for_terminal("job_1", timeout=60, poll_interval=0.5)
check("wait_for_terminal() returns when the job reaches 'done'",
      outcome.status == "done" and outcome.completed)
check("wait_for_terminal() polled until the terminal state", clock.sleeps == [0.5] * 3)
check("wait_for_terminal() reports how long it waited",
      abs(outcome.waited_seconds - 1.5) < 1e-9, str(outcome.waited_seconds))
check("the wait result is machine-readable",
      outcome.as_dict()["job_id"] == "job_1"
      and outcome.as_dict()["completed"] is True)

svc = JobService(reader=FakeReader(["running", "failed"]), engine=None,
                 clock=FakeClock())
outcome = svc.wait_for_terminal("job_1", timeout=60)
check("a failed job is terminal but NOT completed",
      outcome.status == "failed" and outcome.completed is False)

# The case that would otherwise hang: paused/interrupted never progress.
for stalled in ("paused", "interrupted"):
    svc = JobService(reader=FakeReader(["running", stalled]), engine=None,
                     clock=FakeClock())
    outcome = svc.wait_for_terminal("job_1", timeout=60)
    check(f"a '{stalled}' job returns instead of blocking until timeout",
          outcome.status == stalled and outcome.completed is False)

svc = JobService(reader=FakeReader(["running"]), engine=None, clock=FakeClock())
err = raises(OperationTimeout, svc.wait_for_terminal, "job_1", timeout=3,
             poll_interval=1)
check("a job that never finishes raises OperationTimeout",
      isinstance(err, OperationTimeout), repr(err))
check("OperationTimeout maps to CLI exit 5", getattr(err, "exit_code", None) == 5)
check("the timeout error says the job is still running",
      isinstance(err, OperationTimeout) and "still running" in str(err))
check("the timeout error carries the job id and last status",
      isinstance(err, OperationTimeout)
      and err.details.get("job_id") == "job_1"
      and err.details.get("status") == "running")

svc = JobService(reader=FakeReader(["done"]), engine=None, clock=FakeClock())
instant = svc.wait_for_terminal("job_1", timeout=60)
check("an already-finished job returns without sleeping at all",
      instant.completed and instant.waited_seconds == 0)

err = raises(JobNotFound, svc.get, "job_missing")
check("get() raises JobNotFound for an unknown job", isinstance(err, JobNotFound))
check("JobNotFound maps to HTTP 404", getattr(err, "http_status", None) == 404)

svc = JobService(reader=FakeReader(["running"]), engine=None, clock=FakeClock())
err = raises(InvalidRequest, svc.wait_for_terminal, "job_1", timeout=0)
check("wait_for_terminal() rejects a non-positive timeout",
      isinstance(err, InvalidRequest))


# KeyError from the engine must become a typed JobNotFound.
class ExplodingEngine:
    def pause(self, job_id): raise KeyError(job_id)
    def resume(self, job_id): raise KeyError(job_id)
    def cancel_job(self, job_id): raise KeyError(job_id)


svc = JobService(reader=FakeReader(["running"]), engine=ExplodingEngine(),
                 clock=FakeClock())
for name in ("pause", "resume", "cancel"):
    err = raises(JobNotFound, getattr(svc, name), "job_gone")
    check(f"{name}() translates the engine's KeyError into JobNotFound",
          isinstance(err, JobNotFound), repr(err))

# ---------------------------------------------------------------------------
# IngestService
# ---------------------------------------------------------------------------
ingest = runtime.ingest
err = raises(WorkspaceNotFound, ingest.start, "p_nope")
check("ingest.start() 404s for an unknown workspace",
      isinstance(err, WorkspaceNotFound))

no_paths = ws.create("No Paths")
err = raises(InvalidRequest, ingest.start, no_paths["id"])
check("ingest.start() refuses a workspace with no registered source path",
      isinstance(err, InvalidRequest), repr(err))
check("the no-path error explains what to do",
      isinstance(err, InvalidRequest) and "add one" in str(err))

queued = ingest.start(with_path["id"])
check("ingest.start() returns a persisted job id",
      queued["job_id"].startswith("job_"))
check("ingest.start() without wait does not block", queued["waited"] is False)
check("the enqueued job is an ingest for this workspace",
      queued["job"]["type"] == "ingest"
      and queued["job"]["project_id"] == with_path["id"])

again = ingest.start(with_path["id"])
check("the engine dedupes a second ingest for the same workspace",
      again["job_id"] == queued["job_id"])

state = ingest.status(with_path["id"])
check("ingest.status() reports the workspace state", "state" in state)
check("ingest.status() lists ingest jobs",
      any(j["job_id"] == queued["job_id"] for j in state["jobs"]))
check("ingest.status() surfaces the active job",
      (state["active_job"] or {}).get("job_id") == queued["job_id"])

# ---------------------------------------------------------------------------
# ExportService
# ---------------------------------------------------------------------------
export = runtime.export
check("export service reports the frozen artifact schema version",
      export.schema_version == "1.1.0")

err = raises(InvalidRequest, export.export, "./definitely-not-here", "out")
check("export() rejects a missing repository", isinstance(err, InvalidRequest))
err = raises(InvalidRequest, export.export, "", "out")
check("export() rejects an empty repo path", isinstance(err, InvalidRequest))
err = raises(InvalidRequest, export.export, FIXTURE, "out",
             template="spring-boot", no_template=True)
check("export() rejects --template together with --no-template",
      isinstance(err, InvalidRequest))

out_dir = tempfile.mkdtemp(prefix="om_export_")
summary = export.export(FIXTURE, out_dir)
check("export() writes the artifact directory",
      os.path.isfile(os.path.join(out_dir, "manifest.json")))
check("export() reports the schema version", summary["schemaVersion"] == "1.1.0")
check("export() indexed the fixture files", summary["filesIndexed"] > 0)

# ---------------------------------------------------------------------------
# HealthService
# ---------------------------------------------------------------------------
report = runtime.health.report()
check("health report is a HealthReport", isinstance(report, HealthReport))
check("health report carries the runtime version",
      report.version == runtime.version)
check("health report has checks", len(report.checks) >= 8)
check("every health check is a HealthCheck",
      all(isinstance(c, HealthCheck) for c in report.checks))

names = {c.name for c in report.checks}
for required in ("data_dir", "database", "migrations", "project_dirs",
                 "vectorstore", "embeddings", "mcp", "model_config",
                 "model_server", "runtime_version"):
    check(f"health reports the '{required}' check", required in names)

check("no health check is in an unknown state",
      all(c.status in ("ok", "warn", "error") for c in report.checks))
check("a missing optional local model does NOT fail doctor",
      report.ok is True, f"failures={[c.name for c in report.failures()]}")
check("the migrations check reports the schema version",
      any(c.name == "migrations" and c.data.get("version", 0) >= 2
          for c in report.checks))
check("the health summary is JSON-serializable",
      isinstance(runtime.health.summary()["checks"], list))

# An error-severity check must fail the report; a warning must not.
check("a report with only warnings is ok",
      HealthReport("x", [HealthCheck("a", "ok"), HealthCheck("b", "warn")]).ok)
check("a report with an error is not ok",
      not HealthReport("x", [HealthCheck("a", "ok"), HealthCheck("b", "error")]).ok)
check("report status is the worst severity present",
      HealthReport("x", [HealthCheck("a", "ok"),
                         HealthCheck("b", "warn")]).status == "warn")

bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
