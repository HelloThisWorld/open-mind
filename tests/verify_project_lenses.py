"""Adaptive Project Lenses — Template projection, organization files,
safe-pattern gate, bounded induction samples, deterministic validation, and
the provisional→validated→approved→active lifecycle.
"""
import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401
from _semantic_helpers import (  # noqa: E402
    DESIGN_MD, REQUIREMENTS_MD, check, finish, make_workspace, mock_profile)

os.environ.update({"OPENMIND_EMBED_OFFLINE": "1",
                   "OPENMIND_EMBED_DEVICE": "cpu",
                   "OPENMIND_INGEST_FREE_GPU": "0",
                   "OPENMIND_ENRICH_EGRESS": "0",
                   "OPENMIND_SOURCELINK_EGRESS": "0"})

from openmind import config, templates  # noqa: E402
from openmind.runtime import get_runtime  # noqa: E402
from openmind.semantic.errors import LensInvalid  # noqa: E402
from openmind.semantic.lenses.models import validate_lens_definition  # noqa: E402
from openmind.semantic.lenses.sampling import (  # noqa: E402
    MAX_SAMPLE_CHARS, MAX_SAMPLED_ASSETS, MAX_SAMPLED_SEGMENTS,
    build_sample_plan)
from openmind.semantic.providers.mock_provider import (  # noqa: E402
    RECORDED_REQUESTS, reset_recorder)

runtime = get_runtime()
lenses = runtime.lenses
pid = make_workspace(runtime, "lens-ws",
                     documents={"requirements.md": REQUIREMENTS_MD,
                                "design.md": DESIGN_MD})
src = pathlib.Path(tempfile.mkdtemp(prefix="om_lens_code_"))
(src / "api").mkdir()
(src / "api" / "orders_handler.py").write_text(
    "def handle(req):\n    return 'REQ-NC-001'\n", encoding="utf-8")
runtime.workspaces.add_path(pid, str(src))
runtime.ingest.start(pid, wait=True, timeout=300)

# ---------------------------------------------------------------------------
# 1. Built-in Template projection; template behavior unchanged
# ---------------------------------------------------------------------------
template_names = {t["name"] for t in templates.list_templates()}
listing = lenses.list_lenses(pid, source="builtin")
builtin_names = {l["name"] for l in listing["lenses"]}
check("every valid Template projects to a built-in lens",
      {"generic", "spring-boot"} <= builtin_names
      and builtin_names <= template_names)
spring = lenses.get_lens(pid, "builtin:spring-boot")
check("the projection carries match + roles + facet identifiers",
      spring["definition"]["match"]["dependencies"]
      and spring["definition"]["roles"]
      and spring["source"] == "builtin" and spring["stored"] is False)
before = templates.get_template("spring-boot")
active = lenses.activate(pid, "builtin:spring-boot")
check("activating a built-in lens materializes a workspace snapshot",
      active["status"] == "active" and active["stored"] is True)
after = templates.get_template("spring-boot")
check("Template selection/facets/guide behavior is untouched by lens "
      "activation", before is not None and after is not None
      and before.roles == after.roles and before.guide == after.guide)

# ---------------------------------------------------------------------------
# 2. Organization lenses: valid loads, invalid stays visible
# ---------------------------------------------------------------------------
lens_dir = config.DATA_DIR / "lenses"
lens_dir.mkdir(parents=True, exist_ok=True)
(lens_dir / "org-neutral.json").write_text(json.dumps({
    "schemaVersion": "2.0.0", "name": "org-neutral", "title": "Org",
    "description": "an organization lens",
    "roles": [{"name": "api", "pathGlobs": ["api/*"],
               "namePatterns": [], "annotations": []}],
    "identifiers": [{"name": "req", "kind": "requirement",
                     "pattern": "REQ-[A-Z]+-[0-9]+",
                     "examples": ["REQ-NC-001"]}]}), encoding="utf-8")
(lens_dir / "org-broken.json").write_text("{ not json", encoding="utf-8")
org = lenses.list_lenses(pid, source="organization")["lenses"]
by_name = {l["name"]: l for l in org}
check("a valid organization lens file loads",
      by_name["org-neutral"]["validation"]["result"] == "valid")
check("an invalid organization lens stays VISIBLE with its errors",
      by_name["org-broken"]["validation"]["result"] == "invalid"
      and by_name["org-broken"]["validation"]["errors"])
imported = lenses.import_organization_lens(pid, "org-neutral")
check("import snapshots the file with a checksum",
      imported["status"] == "validated"
      and "#" in imported["organization_key"])
active2 = lenses.activate(pid, imported["id"])
check("a valid organization lens can be activated",
      active2["status"] == "active")
check("the previously active lens was superseded back",
      lenses.get_lens(pid, active["id"])["status"] in ("validated",
                                                       "approved"))

# ---------------------------------------------------------------------------
# 3. Safe-pattern gate on model-generated definitions
# ---------------------------------------------------------------------------
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "evil", "description": "d",
    "roles": [{"name": "r", "pathGlobs": [], "annotations": [],
               "namePatterns": ["(?<=secret)key"]}],
    "sampleEvidenceIds": ["e_1"]}, source="induced")
check("lookbehind regex rejected", any("disallowed regex" in e
                                       for e in errors))
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "evil2", "description": "d",
    "identifiers": [{"name": "x", "kind": "requirement",
                     "pattern": "(a+)\\1", "examples": []}],
    "sampleEvidenceIds": ["e_1"]}, source="induced")
check("backreference regex rejected", any("disallowed regex" in e
                                          for e in errors))
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "evil3", "description": "d",
    "match": {"pathGlobs": ["https://exfil.example.com/*"]},
    "sampleEvidenceIds": ["e_1"]}, source="induced")
check("URL inside a pattern rejected", bool(errors))
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "evil4", "description": "d",
    "identifiers": [{"name": "x", "kind": "requirement",
                     "pattern": "A" * 300, "examples": []}],
    "sampleEvidenceIds": ["e_1"]}, source="induced")
check("over-long pattern rejected", any("exceeds" in e for e in errors))
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "evil5", "description": "d",
    "shellHook": "rm -rf /", "sampleEvidenceIds": ["e_1"]},
    source="induced")
check("unknown (executable-shaped) top-level keys rejected",
      any("unknown top-level" in e for e in errors))
_, errors, _ = validate_lens_definition({
    "schemaVersion": "2.0.0", "name": "no-samples", "description": "d"},
    source="induced")
check("an induced lens without sampleEvidenceIds is rejected",
      any("sampleEvidenceIds" in e for e in errors))

# ---------------------------------------------------------------------------
# 4. Bounded, deterministic sampling
# ---------------------------------------------------------------------------
plan1 = build_sample_plan(pid)
plan2 = build_sample_plan(pid)
check("sampling is deterministic for the same workspace state",
      json.dumps(plan1, sort_keys=True) == json.dumps(plan2, sort_keys=True))
check("sampling respects its hard limits",
      plan1["sample_count"] <= MAX_SAMPLED_ASSETS
      and plan1["segment_count"] <= MAX_SAMPLED_SEGMENTS
      and plan1["total_chars"] <= MAX_SAMPLE_CHARS)
check("the plan reports what was omitted", "omitted" in plan1
      and "clusters_total" in plan1["omitted"])
check("the plan estimates tokens and labels them estimated",
      plan1["estimated_tokens"] > 0
      and "estimated" in plan1["token_estimate_basis"])
check("sample planning made no provider call",
      plan1["provider_calls_made"] == 0)

# ---------------------------------------------------------------------------
# 5. Induction: bounded samples in, provisional lens out, gates enforced
# ---------------------------------------------------------------------------
sample_ids = [e["evidence_id"] for s in plan1["samples"]
              for e in s["evidence"]]
induced_def = {
    "schemaVersion": "2.0.0", "name": "induced-neutral",
    "title": "Neutral", "description": "induced from samples",
    "match": {"languages": ["python"], "dependencies": [],
              "markerFiles": [], "pathGlobs": ["api/*"],
              "documentTitlePatterns": ["Neutral"], "documentTypes": []},
    "roles": [{"name": "api-handler", "title": "API",
               "pathGlobs": ["api/*"], "namePatterns": [],
               "annotations": []}],
    "identifiers": [{"name": "req-id", "kind": "requirement",
                     "pattern": "REQ-[A-Z]+-[0-9]+",
                     "examples": ["REQ-NC-001"]}],
    "documentPatterns": [{"name": "scope-heading",
                          "headingPatterns": ["^Scope"],
                          "tableHeaders": []}],
    "semanticTasks": [{"task": "requirement-extraction",
                       "includeRoles": [], "includeAssetTypes": [],
                       "includeBlockTypes": []}],
    "relationHints": [{"sourceType": "requirement",
                       "targetType": "interface",
                       "candidateRelation": "refines", "signals": ["id"]}],
    "validation": {"minimumAssetCoverage": 0.0, "maximumRoleOverlap": 1.0},
    "sampleEvidenceIds": sample_ids[:4],
}
mock_profile("mock-induce", responses={
    "project-lens-induction": induced_def})
reset_recorder()
result = lenses.start_induction(pid, provider_profile="mock-induce",
                                wait=True, timeout=180)
check("induction completed through the job engine",
      result.get("completed") and (result.get("run") or {})["status"]
      == "done")
sent = RECORDED_REQUESTS[-1]
check("induction sent ONLY the bounded sample (never the workspace)",
      len(sent["input_packet"]["untrustedContent"]) <= MAX_SAMPLED_SEGMENTS)
check("induction context carries the deterministic inventory",
      "inventory" in sent["input_packet"]["context"])
lens = result["lens"]
check("the induced lens is stored PROVISIONAL",
      lens["status"] == "provisional" and lens["source"] == "induced")
check("the induced lens references valid sample evidence",
      lens["definition"]["sampleEvidenceIds"]
      and set(lens["definition"]["sampleEvidenceIds"])
      <= set(sent["input_packet"]["allowedEvidenceIds"]))
check("induction provenance recorded (profile, model, prompt version)",
      lens["provider_profile"] == "mock-induce" and lens["model_name"]
      and lens["prompt_version"])

try:
    lenses.activate(pid, lens["id"])
    check("an unapproved induced lens cannot be activated", False)
except LensInvalid:
    check("an unapproved induced lens cannot be activated", True)

validated = lenses.validate(pid, lens["id"])
metrics = validated["validation"]["metrics"]
check("deterministic validation produces the full metric set",
      {"asset_coverage", "role_coverage", "role_overlap",
       "unmatched_role_count", "identifier_hits",
       "document_pattern_hits", "unsupported_task_count",
       "invalid_pattern_count"} <= set(metrics))
check("identifier patterns actually hit the corpus",
      metrics["identifier_hits"].get("req-id", 0) >= 1)
check("a not-invalid provisional lens becomes VALIDATED",
      validated["status"] == "validated")

approved = lenses.approve(pid, lens["id"])
check("explicit approval works and stamps approved_at",
      approved["status"] == "approved" and approved["approved_at"])
final = lenses.activate(pid, lens["id"])
check("an approved valid induced lens can be activated",
      final["status"] == "active")
check("get_active_lens returns it",
      lenses.get_active_lens(pid)["id"] == lens["id"])

# an invalid lens can never be approved
bad_ids = __import__("openmind.semantic.store",
                     fromlist=["store"]).insert_lens(pid, {
    "name": "bad-lens", "source": "induced", "status": "provisional",
    "schema_version": "2.0.0",
    "definition": {"schemaVersion": "2.0.0", "name": "bad-lens",
                   "description": "d",
                   "roles": [{"name": "r", "pathGlobs": ["zzz-nothing/*"],
                              "namePatterns": [], "annotations": []}],
                   "sampleEvidenceIds": ["e_not_real"]},
    "validation": {}})
try:
    lenses.approve(pid, bad_ids)
    check("an invalid lens cannot be approved", False)
except LensInvalid:
    check("an invalid lens cannot be approved", True)

# ---------------------------------------------------------------------------
# 6. The active lens narrows SEMANTIC PLANNING ONLY
# ---------------------------------------------------------------------------
semantic = runtime.semantic
mock_profile("mock-plan", responses={})
semantic.set_policy(pid, provider_profile="mock-plan")
in_lens = semantic.plan_analysis(pid, task_types=["requirement-extraction"])
outside = semantic.plan_analysis(pid, task_types=["document-classification"])
check("a task listed in the active lens plans targets",
      in_lens["target_count"] > 0
      and in_lens["lens_id"] == lens["id"])
check("a task NOT listed in the active lens is excluded with a reason",
      outside["target_count"] == 0 and outside["excluded_count"] > 0)
docs_before = runtime.documents.list_documents(pid)["total"]
ingest_again = runtime.ingest.start(pid, wait=True, timeout=300)
check("deterministic ingest is unchanged by the active lens",
      ingest_again.get("completed") is True
      and runtime.documents.list_documents(pid)["total"] == docs_before)

finish()
