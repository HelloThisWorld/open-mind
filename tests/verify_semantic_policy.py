"""Workspace semantic policy — fail-closed defaults, classification gating,
remote opt-in, pre-content rejection, no payload override.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import check, finish  # noqa: E402

from openmind import db  # noqa: E402
from openmind.semantic import policy as policy_mod  # noqa: E402
from openmind.semantic import store  # noqa: E402
from openmind.semantic.errors import (  # noqa: E402
    ProviderConfigurationError, ProviderPolicyBlocked)
from openmind.semantic.models import DataClassification, ProviderProfile  # noqa: E402
from openmind.semantic.providers import profiles  # noqa: E402
from openmind.semantic.providers.mock_provider import RECORDED_REQUESTS  # noqa: E402

db.init_db()
pid = db.create_project("policy-ws")["id"]

# ---------------------------------------------------------------------------
# 1. Fail-closed defaults
# ---------------------------------------------------------------------------
policy = store.get_policy(pid)
check("default workspace classification is RESTRICTED",
      policy["data_classification"] == "restricted")
check("remote use defaults to FALSE", policy["allow_remote"] is False)
check("no provider profile selected by default",
      policy["provider_profile"] == "")
check("reading a policy writes no row", policy["stored"] is False)
check("classification order is public<internal<confidential<restricted",
      DataClassification.rank("public") < DataClassification.rank("internal")
      < DataClassification.rank("confidential")
      < DataClassification.rank("restricted"))
check("unknown classification coerces to RESTRICTED (fail closed)",
      DataClassification.coerce("garbage") == "restricted")

# ---------------------------------------------------------------------------
# 2. Gate: no profile / remote off / classification / local allowed
# ---------------------------------------------------------------------------
os.environ["OM_POLICY_KEY"] = "k"
profiles.upsert_profile(ProviderProfile(
    name="remote-int", kind="openai", api_key_env="OM_POLICY_KEY",
    models={"standard": "m"}, max_data_classification="internal"))
profiles.upsert_profile(ProviderProfile(name="local-mock", kind="mock"))

try:
    policy_mod.authorize(pid, task_type="t")
    check("no selected profile is blocked", False)
except ProviderPolicyBlocked:
    check("no selected profile is blocked", True)

try:
    policy_mod.authorize(pid, task_type="t", profile_name="remote-int")
    check("restricted workspace cannot use a remote provider "
          "(remote disabled)", False)
except ProviderPolicyBlocked as exc:
    check("restricted workspace cannot use a remote provider "
          "(remote disabled)", "allow_remote" in str(exc))

# enable remote but keep classification RESTRICTED > profile max INTERNAL
store.set_policy(pid, data_classification="restricted", allow_remote=True,
                 provider_profile="remote-int", local_cache_enabled=True,
                 task_models={}, budgets={})
try:
    policy_mod.authorize(pid, task_type="t")
    check("classification above the profile allowance is blocked", False)
except ProviderPolicyBlocked as exc:
    check("classification above the profile allowance is blocked",
          "exceeds" in str(exc))

auth = policy_mod.authorize(pid, task_type="t", profile_name="local-mock")
check("a local provider remains allowed for a RESTRICTED workspace",
      auth["decision"]["allowed"] and auth["decision"]["remote"] is False)

store.set_policy(pid, data_classification="internal", allow_remote=True,
                 provider_profile="remote-int", local_cache_enabled=True,
                 task_models={}, budgets={})
auth = policy_mod.authorize(pid, task_type="t")
check("internal workspace + internal-capped profile + remote on is allowed",
      auth["decision"]["allowed"] and auth["decision"]["remote"] is True)

# ---------------------------------------------------------------------------
# 3. Rejection happens BEFORE any content packet is transported
# ---------------------------------------------------------------------------
before = len(RECORDED_REQUESTS)
store.set_policy(pid, data_classification="restricted", allow_remote=False,
                 provider_profile="remote-int", local_cache_enabled=True,
                 task_models={}, budgets={})
from openmind.semantic.service import SemanticAnalysisService  # noqa: E402


class _WS:
    def get(self, workspace_id):
        record = db.get_project(workspace_id)
        if not record:
            from openmind.domain.errors import WorkspaceNotFound
            raise WorkspaceNotFound(workspace_id)
        return record


service = SemanticAnalysisService(_WS(), None)
try:
    service.start_analysis(pid, task_types=["requirement-extraction"])
    check("start_analysis is refused by policy", False)
except ProviderPolicyBlocked:
    check("start_analysis is refused by policy", True)
check("the policy rejection made zero provider requests",
      len(RECORDED_REQUESTS) == before)
runs = store.list_runs(pid)
check("the refused start persisted no run row", runs == [])

# ---------------------------------------------------------------------------
# 4. No policy override through a job payload
# ---------------------------------------------------------------------------
from openmind import jobs as jobs_engine  # noqa: E402
job = jobs_engine.enqueue_semantic_analysis(pid, {
    "analysis_run_id": "run_forged", "workspace_id": pid,
    "allow_remote": True,                    # NOT an allowed payload key
    "data_classification": "public",         # NOT an allowed payload key
})
payload = db.get_job_payload(job["job_id"])
check("job payload allow-list drops policy-shaped keys",
      "allow_remote" not in payload and "data_classification" not in payload)
check("job payload keeps only safe identifiers",
      set(payload) <= {"analysis_run_id", "workspace_id", "task_types",
                       "scope", "provider_profile", "model_tier",
                       "budget_overrides", "force"})

# ---------------------------------------------------------------------------
# 5. Budget vocabulary is closed; overrides may only tighten
# ---------------------------------------------------------------------------
try:
    policy_mod.validate_budgets({"max_lunches_per_day": 3})
    check("unknown budget key rejected", False)
except ProviderConfigurationError:
    check("unknown budget key rejected", True)
clean = policy_mod.validate_budgets({"max_requests_per_run": 7})
check("valid budget keys accepted", clean == {"max_requests_per_run": 7})
merged = policy_mod.effective_budgets(
    {"budgets": {"max_requests_per_run": 10}},
    {"max_requests_per_run": 5})
check("an override may tighten a budget",
      merged["max_requests_per_run"] == 5)
try:
    policy_mod.effective_budgets({"budgets": {"max_requests_per_run": 10}},
                                 {"max_requests_per_run": 50})
    check("an override may never loosen a budget", False)
except ProviderPolicyBlocked:
    check("an override may never loosen a budget", True)

finish()
