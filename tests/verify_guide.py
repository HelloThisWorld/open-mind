"""Acceptance for the template-driven learning guide (docs revival) and the
new built-in profiles.

Covers: the activated guide schema contract, the deterministic renderer
(citations, honest empty sections, byte-identical regeneration), human-notes
preservation across regeneration, the honest reset (no template -> pages
cleared), the /docs + /gendocs surface, and the new rails / django /
express-nestjs built-ins including the express min_score guard.
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import config, templates  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures", "testrepos").replace("\\", "/")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if detail else ""))


def write_user_template(fname, text):
    config.USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    (config.USER_TEMPLATES_DIR / fname).write_text(text, encoding="utf-8")


# ---- new built-ins load valid, with guides ----
for name in ("rails", "django", "express-nestjs", "spring-boot"):
    t = templates.get_template(name)
    check(f"built-in '{name}' profile is valid and carries a guide",
          t is not None and len(t.guide) >= 4)

# ---- guide schema contract fails at load, visibly ----
write_user_template("bad-guide-query.yaml",
                    'schemaVersion: "1.2.0"\nname: bad-guide-query\ndescription: x\n'
                    'guide:\n  - section: s1\n    title: T\n    query: hallucinate\n')
write_user_template("bad-guide-facet.yaml",
                    'schemaVersion: "1.2.0"\nname: bad-guide-facet\ndescription: x\n'
                    'guide:\n  - section: s1\n    title: T\n    query: facets\n')
write_user_template("bad-guide-index.yaml",
                    'schemaVersion: "1.2.0"\nname: bad-guide-index\ndescription: x\n'
                    'guide:\n  - section: index\n    title: T\n    query: overview\n')
listed = {t["name"]: t for t in templates.list_templates()}
check("an unknown guide query is rejected naming the vocabulary",
      not listed["bad-guide-query"]["valid"]
      and any("must be one of" in e for e in listed["bad-guide-query"]["errors"]))
check("query 'facets' without a facet name is rejected",
      not listed["bad-guide-facet"]["valid"]
      and any("requires a facet" in e for e in listed["bad-guide-facet"]["errors"]))
check("'index' is a reserved section name",
      not listed["bad-guide-index"]["valid"]
      and any("reserved" in e for e in listed["bad-guide-index"]["errors"]))

# ---- express-nestjs auto-select guard: manifest evidence required ----
ts_only = {"primary_language": "TypeScript",
           "languages": [{"language": "TypeScript"}], "manifests": []}
check("a routes/ folder + TypeScript alone does NOT select express-nestjs",
      templates.auto_select(ts_only, ["src/routes/orders.ts"]) is None)
with_dep = {"primary_language": "TypeScript",
            "languages": [{"language": "TypeScript"}],
            "manifests": [{"file": "package.json", "dependencies": ["express"]}]}
sel = templates.auto_select(with_dep, ["src/routes/orders.ts"])
check("an express dependency plus the language signal selects express-nestjs",
      sel is not None and sel["name"] == "express-nestjs", str(sel))

# ---- end to end: learn -> guide pages; notes survive; honest reset ----
from fastapi.testclient import TestClient  # noqa: E402
from openmind.main import app  # noqa: E402
from openmind import db  # noqa: E402


def widle(c, n=300):
    for _ in range(n):
        if not [x for x in c.get("/jobs").json()["jobs"]
                if x["status"] in ("queued", "running")]:
            return
        time.sleep(0.3)


with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})
    pid = c.post("/projects", json={"name": "gd", "path": FIX, "exclude": []}).json()["id"]
    c.post(f"/projects/{pid}/template", json={"name": "spring-boot"})
    c.post("/ingest", json={"project_id": pid}); widle(c)

    docs = c.get(f"/docs?scope={pid}").json()["docs"]
    pages = [d["page"] for d in docs]
    check("learn generates the guide pages (index first, guide order kept)",
          pages[:3] == ["index", "overview", "layers"], str(pages))
    check("doc listing carries titles and the template",
          all(d["title"] and d["template"] == "spring-boot" for d in docs))

    http = c.get(f"/docs/http-surface?scope={pid}").json()
    check("facet sections render verbatim captures as a cited table",
          "| Post | /orders |" in http["content"]
          and "OrderController.java:" in http["content"])
    kafka = c.get(f"/docs/kafka-topics?scope={pid}").json()
    check("a facet with no matches renders the honest empty line",
          "Nothing found for this section" in kafka["content"])
    flows = c.get(f"/docs/request-flows?scope={pid}").json()
    check("flows are named from facet captures with cited steps",
          "HTTP route: Post /orders" in flows["content"]
          and "Entry point (route) at" in flows["content"])
    gloss = c.get(f"/docs/domain-terms?scope={pid}").json()
    check("glossary section quotes verbatim definitions with provenance",
          "**SKU** — Stock Keeping Unit" in gloss["content"])
    check("an invalid page slug is a 404, not a crash",
          c.get(f"/docs/..no..pe?scope={pid}").status_code == 404)

    # notes survive regeneration; content is otherwise byte-identical
    docs_dir = config.project_docs_dir(pid)
    page_file = docs_dir / "layers.md"
    txt = page_file.read_text(encoding="utf-8")
    txt = txt.replace("<!-- OPENMIND:NOTES:START -->",
                      "<!-- OPENMIND:NOTES:START -->\nKEEP-ME: reviewed by a human.")
    page_file.write_text(txt, encoding="utf-8")
    c.post("/gendocs", json={"project_id": pid}); widle(c)
    regen1 = (docs_dir / "layers.md").read_text(encoding="utf-8")
    check("human notes are preserved across regeneration",
          "KEEP-ME: reviewed by a human." in regen1)
    c.post("/gendocs", json={"project_id": pid}); widle(c)
    regen2 = (docs_dir / "layers.md").read_text(encoding="utf-8")
    check("regeneration is byte-identical (deterministic, notes included)",
          regen1 == regen2)

    # honest reset: clearing the template clears the docs on the next learn
    c.post(f"/projects/{pid}/template", json={"name": None})
    c.post("/ingest", json={"project_id": pid}); widle(c)
    check("no template -> docs are cleared (honest empty shelf)",
          c.get(f"/docs?scope={pid}").json()["docs"] == []
          and c.get(f"/docs/index?scope={pid}").status_code == 404)

passed = sum(1 for _, ok in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
