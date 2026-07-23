"""Phase 7 adapter + compatibility checks (§40, §41, §5 compatibility).

Asserts: the 43 existing MCP tools are unchanged and 8 additive read-only Git
overlay tools bring the total to exactly 51; no write-capable overlay verb is
exposed via MCP; the CLI git/overlay/pr/impact groups parse; the Phase 7 REST
routes are registered; and the frozen version contracts are intact
(runtime 1.7.0-dev, migration head v0008, .openmind 1.1.0, Bundle
2.0.0-draft.2).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from _git_helpers import check, finish  # noqa: E402


# -- MCP tool inventory ------------------------------------------------------
from openmind import mcp_server as mcp  # noqa: E402

EXISTING = (list(mcp.TOOL_NAMES) + list(mcp.ASSET_TOOL_NAMES)
            + list(mcp.DOCUMENT_TOOL_NAMES) + list(mcp.SEMANTIC_TOOL_NAMES)
            + list(mcp.KNOWLEDGE_TOOL_NAMES) + list(mcp.TRACE_TOOL_NAMES))
OVERLAY = list(mcp.OVERLAY_TOOL_NAMES)
ALL = EXISTING + OVERLAY

check("43 existing MCP tools preserved", len(EXISTING) == 43,
      detail=str(len(EXISTING)))
check("exactly 8 additive overlay MCP tools", len(OVERLAY) == 8,
      detail=str(OVERLAY))
check("51 MCP tools total", len(ALL) == 51, detail=str(len(ALL)))
check("all MCP tool names unique", len(set(ALL)) == 51)

EXPECTED_OVERLAY = {
    "list_git_overlays", "get_git_overlay", "get_git_diff_summary",
    "search_git_overlay", "get_git_overlay_evidence", "get_change_impact_report",
    "list_impacted_requirements", "list_impacted_tests"}
check("overlay MCP tool names match the spec", set(OVERLAY) == EXPECTED_OVERLAY,
      detail=str(set(OVERLAY) ^ EXPECTED_OVERLAY))

# No write-capable overlay verb is exposed via MCP (spec §40).
WRITE_VERBS = ("create", "refresh", "delete", "reconcile", "capture",
               "abandon", "close", "promote")
leaked = [n for n in OVERLAY
          if any(v in n for v in WRITE_VERBS)]
check("no write-capable overlay MCP tool exposed", not leaked, detail=str(leaked))

# The overlay tools must be importable functions with docstrings that disclose
# the provisional / read-only nature.
for name in OVERLAY:
    fn = getattr(mcp, name)
    doc = (fn.__doc__ or "").lower()
    check(f"{name} discloses read-only/provisional intent",
          "read-only" in doc or "provisional" in doc or "never" in doc)

# -- CLI parses the Phase 7 groups -------------------------------------------
from openmind.cli import build_parser  # noqa: E402

parser = build_parser()
for argv in (
    ["git", "repositories", "--workspace", "p_x"],
    ["git", "baseline", "capture", "--workspace", "p_x"],
    ["git", "baseline", "list", "--workspace", "p_x"],
    ["overlay", "create", "--workspace", "p_x", "--kind", "branch",
     "--repository", "git:.", "--base", "main", "--head", "f"],
    ["overlay", "list", "--workspace", "p_x"],
    ["overlay", "impact", "--workspace", "p_x", "--overlay", "ov_x"],
    ["overlay", "reconcile", "--workspace", "p_x", "--overlay", "ov_x"],
    ["overlay", "delete", "--workspace", "p_x", "--overlay", "ov_x"],
    ["pr", "analyze", "--workspace", "p_x", "--repository", "git:.",
     "--base", "main", "--head", "f", "--pr-number", "1"],
    ["impact", "export", "--workspace", "p_x", "--overlay", "ov_x",
     "--output", "./out"],
    ["impact", "verify", "./out"],
):
    ok = True
    try:
        ns = parser.parse_args(argv)
        ok = getattr(ns, "func", None) is not None
    except SystemExit:
        ok = False
    check(f"CLI parses: {' '.join(argv[:3])}", ok)

# Existing groups still parse (compatibility).
for argv in (["trace", "coverage", "--workspace", "p_x"],
             ["conflict", "list", "--workspace", "p_x"],
             ["knowledge", "search", "--workspace", "p_x", "--query", "x"]):
    ok = True
    try:
        ns = parser.parse_args(argv)
        ok = getattr(ns, "func", None) is not None
    except SystemExit:
        ok = False
    check(f"existing CLI still parses: {' '.join(argv[:2])}", ok)

# -- REST routes registered --------------------------------------------------
from openmind.main import app  # noqa: E402

paths = {r.path for r in app.routes if hasattr(r, "path")}
for expected in (
    "/projects/{project_id}/git/repositories",
    "/projects/{project_id}/git/baselines",
    "/projects/{project_id}/overlays",
    "/projects/{project_id}/overlays/{overlay_id}",
    "/projects/{project_id}/overlays/{overlay_id}/impact",
    "/projects/{project_id}/overlays/{overlay_id}/reconcile",
):
    check(f"REST route present: {expected}", expected in paths)
# Existing routes still present.
for expected in ("/projects/{project_id}/conflicts",
                 "/projects/{project_id}/knowledge/stats"):
    check(f"existing REST route preserved: {expected}", expected in paths)

# -- frozen version contracts ------------------------------------------------
from openmind.version import RUNTIME_VERSION  # noqa: E402
check("runtime version is 1.7.0-dev", RUNTIME_VERSION == "1.7.0-dev",
      detail=RUNTIME_VERSION)

from openmind.migrations.runner import discover  # noqa: E402
head = discover()[-1].version
check("migration head is v0008", head == 8, detail=str(head))

from openmind import artifacts  # noqa: E402
av = getattr(artifacts, "SCHEMA_VERSION", None) or \
    getattr(artifacts, "ARTIFACT_SCHEMA_VERSION", None)
check(".openmind schema stays 1.1.0", av == "1.1.0", detail=str(av))

from openmind.knowledge import bundle  # noqa: E402
bv = getattr(bundle, "BUNDLE_SCHEMA_VERSION", None) or \
    getattr(bundle, "SCHEMA_VERSION", None)
check("Knowledge Bundle stays 2.0.0-draft.2", bv == "2.0.0-draft.2",
      detail=str(bv))

from openmind.overlays import CHANGE_IMPACT_SCHEMA_VERSION  # noqa: E402
check("Change Impact schema is 1.0.0-draft.1",
      CHANGE_IMPACT_SCHEMA_VERSION == "1.0.0-draft.1",
      detail=CHANGE_IMPACT_SCHEMA_VERSION)

raise SystemExit(finish("verify_overlay_adapters"))
