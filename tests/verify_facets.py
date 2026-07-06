"""Acceptance for template facets — the output-shaping half of template
profiles (roles + facet captures) and its projections.

Covers: the activated roles/facets schema contracts (bad patterns fail at
load, visibly), deterministic role classification and facet capture with
file:line evidence, honest empties (a profile that matches nothing produces
nothing), the learn pipeline persisting/removing the facts map, the
structure/graph API surfacing, and the .openmind export (schema 1.1.0:
layers, role counts, facet-named flows) with byte-identical determinism.
"""
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openmind import artifacts, config, facets, templates  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures", "testrepos").replace("\\", "/")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if detail else ""))


def write_user_template(fname, text):
    config.USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    (config.USER_TEMPLATES_DIR / fname).write_text(text, encoding="utf-8")


# ---- the built-in spring-boot profile carries normalized roles + facets ----
sb = templates.get_template("spring-boot")
check("built-in spring-boot profile is valid with roles and facets",
      sb is not None and len(sb.roles) >= 4 and len(sb.facets) >= 2)
check("normalized roles keep template order (controller is the first layer)",
      sb.roles[0]["name"] == "controller" and sb.roles[0]["layer"] == 1)

# ---- activated schema contracts: broken sections fail at load, visibly ----
write_user_template("bad-facet-rx.yaml",
                    'schemaVersion: "1.1.0"\nname: bad-facet-rx\ndescription: x\n'
                    'facets:\n  - name: broken\n    pattern: "(unclosed"\n')
write_user_template("bad-captures.yaml",
                    'schemaVersion: "1.1.0"\nname: bad-captures\ndescription: x\n'
                    'facets:\n  - name: uneven\n    pattern: "(a)(b)"\n'
                    '    captures: [only-one]\n')
write_user_template("bad-role.yaml",
                    'schemaVersion: "1.1.0"\nname: bad-role\ndescription: x\n'
                    'roles:\n  - name: hollow\n    title: no matchers\n')
listed = {t["name"]: t for t in templates.list_templates()}
check("a facet regex that does not compile fails at load with the facet named",
      not listed["bad-facet-rx"]["valid"]
      and any("does not compile" in e for e in listed["bad-facet-rx"]["errors"]))
check("captures must map 1:1 onto the pattern's capture groups",
      not listed["bad-captures"]["valid"]
      and any("1:1" in e for e in listed["bad-captures"]["errors"]))
check("a role without any matcher is rejected",
      not listed["bad-role"]["valid"]
      and any("matcher" in e for e in listed["bad-role"]["errors"]))

# ---- build_facts: classification, capture, evidence, honest empties ----
FILES = [
    ("src/OrderController.java",
     "package x;\n@RestController\npublic class OrderController {\n"
     '  @PostMapping("/orders")\n  public void place() {}\n'
     '  @GetMapping("/orders/{id}")\n  public void get() {}\n}\n', "h1"),
    ("src/BillingService.java",
     "package x;\npublic class BillingService {\n  public void bill() {}\n}\n", "h2"),
    ("docs/notes.md", '@PostMapping("/not-code")\n', "h3"),
]
fdoc = facets.build_facts(FILES, sb)
check("annotation classification carries the deciding line as evidence",
      fdoc["roles"]["src/OrderController.java"]["role"] == "controller"
      and fdoc["roles"]["src/OrderController.java"]["line"] == 2
      and "annotation" in fdoc["roles"]["src/OrderController.java"]["via"])
check("file-name classification works without annotations",
      fdoc["roles"]["src/BillingService.java"]["role"] == "service"
      and fdoc["roles"]["src/BillingService.java"]["via"].startswith("name:"))
routes = [f for f in fdoc["facts"] if f["facet"] == "http_route"]
check("facet captures name the groups verbatim (method + path)",
      len(routes) == 2
      and routes[0]["values"] == {"method": "Post", "path": "/orders"}
      and routes[1]["values"] == {"method": "Get", "path": "/orders/{id}"})
check("every fact carries file:line:snippet evidence",
      all(f["file"] and f["line"] >= 1 and f["snippet"] for f in fdoc["facts"]))
check("facet file filters hold (a .md mention is not a route fact)",
      not any(f["file"].endswith(".md") for f in fdoc["facts"]))
check("build is deterministic (double build -> identical document)",
      json.dumps(fdoc, sort_keys=True) == json.dumps(facets.build_facts(FILES, sb),
                                                     sort_keys=True))
gen = templates.get_template("generic")
check("a profile with no roles/facets produces an honest empty document",
      facets.build_facts(FILES, gen)["stats"] == {"files_classified": 0,
                                                  "fact_count": 0})

# ---- learn pipeline + API surfacing (override -> facts; cleared -> removed) ----
from fastapi.testclient import TestClient  # noqa: E402
from openmind.main import app  # noqa: E402


def widle(c, n=300):
    for _ in range(n):
        if not [x for x in c.get("/jobs").json()["jobs"]
                if x["status"] in ("queued", "running")]:
            return
        time.sleep(0.3)


with TestClient(app) as c:
    c.post("/model-config", json={"port": 1})
    pid = c.post("/projects", json={"name": "fx", "path": FIX, "exclude": []}).json()["id"]
    c.post(f"/projects/{pid}/template", json={"name": "spring-boot"})
    c.post("/ingest", json={"project_id": pid}); widle(c)

    ov = c.get(f"/structure?scope={pid}").json()
    tpl_block = ov.get("template") or {}
    check("learn persists template facts; /structure summarizes the layers",
          tpl_block.get("name") == "spring-boot"
          and any(l["name"] == "controller" and l["count"] >= 1
                  for l in tpl_block.get("layers", [])), str(tpl_block)[:200])

    roots = c.get(f"/graph?scope={pid}").json().get("roots", [])
    ctrl = next((n for n in roots if (n.get("id") or "").endswith("OrderController.java")), None)
    check("graph roots carry the template role of their file",
          ctrl is not None and ctrl.get("role") == "controller",
          str({k: ctrl.get(k) for k in ("id", "role")} if ctrl else roots[:2]))

    node = c.get(f"/graph/node?scope={pid}&id={ctrl['id']}").json() if ctrl else {}
    check("node detail exposes the role evidence and the file's facet facts",
          (node.get("role") or {}).get("role") == "controller"
          and any(f["facet"] == "http_route" and f["values"]["path"] == "/orders"
                  for f in node.get("facets", [])))

    c.post(f"/projects/{pid}/template", json={"name": None})
    c.post("/ingest", json={"project_id": pid}); widle(c)
    check("clearing the template and re-learning removes the facts (honest reset)",
          c.get(f"/structure?scope={pid}").json().get("template") is None)

# ---- .openmind export: schema 1.1.0 template projection + determinism ----
out_root = tempfile.mkdtemp(prefix="openmind-facets-")
out_a, out_b, out_plain = (os.path.join(out_root, d) for d in ("a", "b", "plain"))
s = artifacts.generate_artifacts(FIX, out_a, template="spring-boot",
                                 generated_at="2026-01-01T00:00:00Z")
artifacts.generate_artifacts(FIX, out_b, template="spring-boot",
                             generated_at="2026-01-01T00:00:00Z")
artifacts.generate_artifacts(FIX, out_plain, no_template=True,
                             generated_at="2026-01-01T00:00:00Z")


def load(d, name):
    with open(os.path.join(d, name), encoding="utf-8") as fh:
        return json.load(fh)


arch = load(out_a, "architecture.json")
flows = load(out_a, "flows.json")["flows"]
meta = load(out_a, "metadata.json")
check("metadata records the applied template profile",
      (meta.get("template") or {}).get("name") == "spring-boot")
layer_roles = {l["role"]: l for l in arch.get("layers", [])}
check("architecture gains evidence-backed role layers",
      {"controller", "service", "repository"} <= set(layer_roles)
      and all(l["evidence"][0]["file"] and l["evidence"][0]["line"] >= 1
              for l in arch["layers"]))
check("components carry per-module role counts",
      any(c.get("roles") for c in arch["components"]))
named = [f for f in flows if f["name"].startswith("HTTP route:")]
check("flows are named from verbatim facet captures",
      any("/orders" in f["name"] for f in named)
      and all(f.get("facets") for f in named), str([f["name"] for f in flows]))
check("templated export is byte-identical across runs",
      all(open(os.path.join(out_a, n), "rb").read()
          == open(os.path.join(out_b, n), "rb").read()
          for n in os.listdir(out_a)))
check("--no-template reproduces the plain shape (no layers, template null)",
      "layers" not in load(out_plain, "architecture.json")
      and load(out_plain, "metadata.json")["template"] is None)

passed = sum(1 for _, ok in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
