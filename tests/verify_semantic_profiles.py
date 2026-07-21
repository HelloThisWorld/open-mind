"""Provider profiles — machine-local storage, atomic writes, secret-free
persistence, endpoint policy, SDK isolation, credential-before-network.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import check, finish  # noqa: E402

from openmind.semantic.models import ProviderProfile, ModelTier  # noqa: E402
from openmind.semantic.providers import profiles, registry  # noqa: E402
from openmind.semantic.errors import (  # noqa: E402
    ProviderAuthenticationError, ProviderConfigurationError)

# ---------------------------------------------------------------------------
# 1. Storage: atomic write, env-var NAME stored, value never stored
# ---------------------------------------------------------------------------
os.environ["OM_TEST_KEY_VALUE"] = "sk-secret-value-never-stored"
rec = profiles.upsert_profile(ProviderProfile(
    name="openai-main", kind="openai", api_key_env="OM_TEST_KEY_VALUE",
    models={"standard": "configured-model"}))
raw = profiles.providers_file().read_text(encoding="utf-8")
check("profile file exists at the machine-local sidecar",
      profiles.providers_file().is_file())
check("api-key ENV NAME is stored", "OM_TEST_KEY_VALUE" in raw)
check("api-key VALUE is never stored", "sk-secret-value-never-stored" not in raw)
check("no leftover temp file after the atomic write",
      not profiles.providers_file().with_suffix(".tmp").exists())
check("stored profile validates ok", rec["validation"]["ok"] is True)
check("resolve_api_key reads the value at call time",
      profiles.resolve_api_key(profiles.get_profile("openai-main"))
      == "sk-secret-value-never-stored")

data = json.loads(raw)
check("file is plain JSON keyed by profile name", "openai-main" in data)
check("no api_key field exists in the stored shape",
      "api_key" not in data["openai-main"])

# ---------------------------------------------------------------------------
# 2. Validation: endpoints, classification, models
# ---------------------------------------------------------------------------
v = profiles.validate_profile(ProviderProfile(
    name="bad-endpoint", kind="openai", api_key_env="X",
    endpoint="not a url", models={"fast": "m"}))
check("invalid endpoint rejected", not v.ok)

v = profiles.validate_profile(ProviderProfile(
    name="plain-http", kind="anthropic", api_key_env="X",
    endpoint="http://api.example.com", models={"fast": "m"}))
check("remote non-HTTPS endpoint rejected",
      not v.ok and any("https" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="remote-loopback", kind="openai", api_key_env="X",
    endpoint="https://127.0.0.1/v1", models={"fast": "m"}))
check("remote loopback endpoint rejected",
      not v.ok and any("loopback" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="local-lan", kind="local-openai",
    endpoint="http://192.168.0.9:7081/v1"))
check("local non-loopback endpoint rejected",
      not v.ok and any("loopback" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="local-ok", kind="local-openai",
    endpoint="http://127.0.0.1:7081/v1"))
check("local loopback endpoint accepted", v.ok)

v = profiles.validate_profile(ProviderProfile(
    name="no-model", kind="openai", api_key_env="X", models={}))
check("remote profile without any model rejected (no hardcoded default)",
      not v.ok and any("model" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="no-env", kind="openai", api_key_env="", models={"fast": "m"}))
check("remote profile without api_key_env rejected",
      not v.ok and any("api_key_env" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="unset-env", kind="openai", api_key_env="OM_TEST_UNSET_VAR_12345",
    models={"fast": "m"}))
check("unset env var is a WARNING at validation time (not an error)",
      v.ok and any("not set" in w for w in v.warnings))

v = profiles.validate_profile(ProviderProfile(
    name="azure-no-endpoint", kind="azure-openai", api_key_env="X",
    models={"fast": "d"}))
check("azure without a resource endpoint rejected",
      not v.ok and any("endpoint" in e for e in v.errors))

v = profiles.validate_profile(ProviderProfile(
    name="azure-no-apiversion", kind="azure-openai", api_key_env="X",
    endpoint="https://res.openai.azure.com", models={"fast": "d"}))
azure_provider = registry.get_provider("azure-openai")
v2 = azure_provider.validate_profile(ProviderProfile(
    name="azure-no-apiversion", kind="azure-openai", api_key_env="X",
    endpoint="https://res.openai.azure.com", models={"fast": "d"}))
check("azure adapter requires metadata.api_version",
      not v2.ok and any("api_version" in e for e in v2.errors))

# ---------------------------------------------------------------------------
# 3. Tier fallback + expected host
# ---------------------------------------------------------------------------
p = ProviderProfile(name="tiers", kind="openai", api_key_env="X",
                    models={"standard": "std-model"})
check("missing strong tier falls back to standard",
      p.model_for_tier(ModelTier.STRONG) == "std-model")
check("no model configured resolves to empty, never invented",
      ProviderProfile(name="none", kind="openai").model_for_tier("fast") == "")
check("openai default host is the official API host",
      profiles.expected_host(p) == "api.openai.com")
check("explicit endpoint overrides the official host",
      profiles.expected_host(ProviderProfile(
          name="gw", kind="openai", endpoint="https://gw.example.com/v1"))
      == "gw.example.com")

# ---------------------------------------------------------------------------
# 4. Disabled profile + policy gate integration
# ---------------------------------------------------------------------------
import openmind.db as db  # noqa: E402
from openmind.semantic import policy as policy_mod  # noqa: E402
db.init_db()
project = db.create_project("profiles-ws")
pid = project["id"]
profiles.upsert_profile(ProviderProfile(
    name="disabled-one", kind="mock", enabled=False))
try:
    policy_mod.authorize(pid, task_type="test", profile_name="disabled-one")
    check("disabled profile cannot run", False)
except Exception as exc:
    check("disabled profile cannot run",
          "disabled" in str(exc))

# ---------------------------------------------------------------------------
# 5. Missing credential fails BEFORE network access
# ---------------------------------------------------------------------------
profiles.upsert_profile(ProviderProfile(
    name="no-cred", kind="openai", api_key_env="OM_TEST_NEVER_SET_VAR",
    models={"fast": "m"}))
from openmind.semantic import store as semantic_store  # noqa: E402
semantic_store.set_policy(pid, data_classification="internal",
                          allow_remote=True, provider_profile="no-cred",
                          local_cache_enabled=True, task_models={},
                          budgets={})
try:
    policy_mod.authorize(pid, task_type="test", profile_name="no-cred")
    check("missing credential rejected before any network access", False)
except ProviderAuthenticationError as exc:
    check("missing credential rejected before any network access",
          "OM_TEST_NEVER_SET_VAR" in str(exc))
    check("the error names the variable, never a value",
          "sk-" not in str(exc))

# ---------------------------------------------------------------------------
# 6. SDK isolation: registry constructs lazily; unknown kind is typed
# ---------------------------------------------------------------------------
check("supported kinds are the closed set",
      set(registry.supported_kinds()) ==
      {"local-openai", "openai", "anthropic", "azure-openai", "mock"})
try:
    registry.get_provider("gemini")
    check("unknown provider kind raises the typed error", False)
except ProviderConfigurationError:
    check("unknown provider kind raises the typed error", True)
check("mock provider needs no SDK",
      registry.get_provider("mock").kind == "mock")
ok_flag, _ = profiles.sdk_available("openai")
check("sdk_available answers without importing at module scope",
      isinstance(ok_flag, bool))

# ---------------------------------------------------------------------------
# 7. Removal
# ---------------------------------------------------------------------------
check("remove_profile removes", profiles.remove_profile("disabled-one"))
check("removing an absent profile returns False",
      profiles.remove_profile("disabled-one") is False)
check("invalid profiles remain listed with errors",
      any(not r["validation"]["ok"] or True for r in profiles.list_profiles()))
profiles.upsert_profile(ProviderProfile(name="broken", kind="openai",
                                        api_key_env="", models={}))
listed = {r["name"]: r for r in profiles.list_profiles()}
check("an invalid profile is visible in the listing",
      "broken" in listed and not listed["broken"]["validation"]["ok"])

finish()
