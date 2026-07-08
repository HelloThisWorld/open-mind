"""Acceptance for the template-profile layer (selection infrastructure).

Covers the contract this build implements: declarative template files
(built-in + user dir), the schema gate (invalid files are listed with errors,
never crash discovery, never get selected), deterministic evidence-carrying
auto-selection at learn time, override precedence with honest failure, and the
ZERO-REGRESSION default — no template resolved means every consumer sees None
and behaves exactly as before templates existed. Learned output is not yet
shaped by templates; that is the extraction/projection phases.
"""
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OPENMIND_EMBED_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401 — forces an isolated data dir (never the live one)

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


# ---- built-ins discovered, deterministic order ----
listed = templates.list_templates()
names = [t["name"] for t in listed]
gen = next((t for t in listed if t["name"] == "generic"), None)
check("built-in 'generic' template is discovered and valid",
      gen is not None and gen["valid"] and gen["source"] == "builtin")
check("listing is deterministically ordered by name", names == sorted(names))

# ---- schema gate: broken files are visible-with-errors, never usable ----
write_user_template("bad-major.yaml",
                    'schemaVersion: "2.0.0"\nname: bad-major\ndescription: x\n')
write_user_template("bad-noname.yaml",
                    'schemaVersion: "1.0.0"\ndescription: no name here\n')
write_user_template("bad-matchkey.json", json.dumps({
    "schemaVersion": "1.0.0", "name": "bad-matchkey", "description": "x",
    "match": {"dependancies": ["typo"]}}))
write_user_template("bad-syntax.yaml", "just: [unclosed")
listed = {t["name"]: t for t in templates.list_templates()}
check("unsupported schema major is rejected with a clear error",
      not listed["bad-major"]["valid"]
      and any("major" in e for e in listed["bad-major"]["errors"]))
check("missing name/description fails validation (file still listed)",
      "bad-noname" in listed and not listed["bad-noname"]["valid"])
check("unknown match key (typo) is rejected, JSON templates load",
      not listed["bad-matchkey"]["valid"]
      and any("dependancies" in e for e in listed["bad-matchkey"]["errors"]))
check("malformed YAML is contained (listed invalid, discovery never crashes)",
      "bad-syntax" in listed and not listed["bad-syntax"]["valid"])
check("invalid templates never resolve", templates.get_template("bad-major") is None)

# ---- auto-selection: evidence-carrying, floor-gated, deterministic ----
LENS = ('schemaVersion: "1.0.0"\nname: %s\ntitle: Demo lens\n'
        'description: demo\nmatch:\n  dependencies: [spring-boot-starter]\n'
        '  languages: [Java]\n  marker_files: ["*.java"]\n')
write_user_template("spring-lens.yaml", LENS % "spring-lens")
detection = {"primary_language": "Java",
             "languages": [{"language": "Java", "files": 3}],
             "manifests": [{"file": "pom.xml",
                            "dependencies": ["spring-boot-starter-web", "kafka-clients"]}]}
sel = templates.auto_select(detection, ["src/main/java/App.java", "pom.xml"])
check("auto-select scores dependency+language+marker evidence (3+2+2=7)",
      sel is not None and sel["name"] == "spring-lens" and sel["score"] == 7,
      str(sel))
check("the selection carries WHICH evidence matched",
      sel["matched"]["dependencies"] == ["spring-boot-starter"]
      and sel["matched"]["languages"] == ["Java"]
      and sel["matched"]["marker_files"] == ["*.java"])
none_sel = templates.auto_select(
    {"primary_language": "Python", "languages": [{"language": "Python"}],
     "manifests": []}, ["README.md"])
check("below the score floor nothing is selected (honest None)", none_sel is None)
write_user_template("a-lens.yaml", LENS % "a-lens")
tie = templates.auto_select(detection, ["src/main/java/App.java"])
check("equal scores break deterministically on name", tie["name"] == "a-lens")
os.remove(config.USER_TEMPLATES_DIR / "a-lens.yaml")

# ---- resolve precedence + zero-regression default ----
check("no template selected -> resolve is None (behavior unchanged)",
      templates.resolve_for_project({}) is None
      and templates.resolve_for_project(None) is None)
proj = {"meta": {"template": "spring-lens", "template_auto": "generic"}}
check("user override wins over auto-selection",
      templates.resolve_for_project(proj).name == "spring-lens")
proj = {"meta": {"template_auto": "spring-lens"}}
check("auto-selection applies when no override is set",
      templates.resolve_for_project(proj).name == "spring-lens")
proj = {"meta": {"template": "does-not-exist", "template_auto": "spring-lens"}}
info = templates.selection_info(proj)
check("an override naming a missing template fails honestly (no silent fallback)",
      templates.resolve_for_project(proj) is None
      and info["effective"] is None and bool(info["override_error"]))

# ---- end to end: learn records the auto-selection; API sets/clears override ----
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
    check("GET /templates lists profiles and the user drop-in dir",
          any(t["name"] == "generic" for t in c.get("/templates").json()["templates"]))
    pid = c.post("/projects", json={"name": "tpl", "path": FIX, "exclude": []}).json()["id"]
    c.post("/ingest", json={"project_id": pid}); widle(c)
    ti = c.get(f"/projects/{pid}/template").json()
    check("learn records the auto-selected profile in project meta",
          ti["auto"] == "spring-lens" and ti["effective_source"] == "auto",
          str(ti))
    check("auto-selection score is persisted alongside the name",
          isinstance(ti["auto_score"], int) and ti["auto_score"] >= 3)
    r = c.post(f"/projects/{pid}/template", json={"name": "generic"})
    check("POST override sets the effective template",
          r.json()["effective"] == "generic"
          and r.json()["effective_source"] == "override")
    check("POST an unknown template name fails at write time (400)",
          c.post(f"/projects/{pid}/template", json={"name": "nope"}).status_code == 400)
    r = c.post(f"/projects/{pid}/template", json={"name": None})
    check("clearing the override falls back to the recorded auto-selection",
          r.json()["override"] is None and r.json()["effective"] == "spring-lens")

passed = sum(1 for _, ok in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
