"""Stage 1 — PROJECT DETECTION.

WHY THIS RUNS FIRST (design intent)
-----------------------------------
A capable coding agent does not assume a repository's architecture; it looks at
the manifests and dependencies and INFERS the stack, then adapts. Open Mind does
the same, deterministically. Detection scans identity/manifest files (pom.xml,
package.json, go.mod, Cargo.toml, requirements.txt, pyproject.toml, ...) plus the
language histogram of the source itself, and produces a small, grounded summary:
which languages, which build systems, library-vs-application, and *stack cues*
parsed from the declared dependencies (e.g. a web framework, a messaging client,
a SQL driver).

Detection DRIVES the rest of the pipeline: it labels the project, decides which
specialized diagrams are even attempted, and lets the base layer report what a
repo IS rather than silently producing an empty result. It never guesses beyond
what the manifests and source state — a cue is only emitted when a declared
dependency supports it, with the dependency named as its provenance.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import langspec

# A declared-dependency token -> coarse capability cue. Substring match against
# the lowercased dependency name; the dependency is recorded as provenance, so a
# cue is always traceable to a real manifest entry (never inferred from prose).
_DEP_CUES: Tuple[Tuple[str, str], ...] = (
    ("fastapi", "web"), ("flask", "web"), ("django", "web"), ("starlette", "web"),
    ("express", "web"), ("koa", "web"), ("nestjs", "web"), ("gin-gonic", "web"),
    ("spring-boot-starter-web", "web"), ("spring-webmvc", "web"), ("actix-web", "web"),
    ("rocket", "web"), ("axum", "web"), ("rails", "web"), ("sinatra", "web"),
    ("kafka", "messaging"), ("pulsar", "messaging"), ("rabbitmq", "messaging"),
    ("amqp", "messaging"), ("pika", "messaging"), ("nats", "messaging"),
    ("redis", "cache"),
    ("grpc", "rpc"), ("protobuf", "rpc"), ("thrift", "rpc"),
    ("sqlalchemy", "data"), ("psycopg", "data"), ("hibernate", "data"),
    ("spring-data", "data"), ("gorm", "data"), ("diesel", "data"),
    ("mongoose", "data"), ("prisma", "data"), ("sequelize", "data"),
    ("mysql", "data"), ("postgres", "data"), ("sqlite", "data"), ("mongodb", "data"),
    ("react", "frontend"), ("vue", "frontend"), ("svelte", "frontend"),
    ("@angular", "frontend"), ("next", "frontend"),
)


# ---------------------------------------------------------------------------
# Manifest parsers (dep-light: json + tomllib + regex; never lxml)
# ---------------------------------------------------------------------------
def _parse_requirements(text: str) -> List[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        out.append(re.split(r"[<>=!~ \[]", line, 1)[0].strip())
    return [d for d in out if d]


def _parse_pyproject(text: str) -> Tuple[List[str], bool]:
    deps: List[str] = []
    is_lib = False
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        return _parse_requirements(text), "[project]" in text
    proj = data.get("project", {})
    for d in proj.get("dependencies", []) or []:
        deps.append(re.split(r"[<>=!~ \[]", str(d), 1)[0].strip())
    poetry = (data.get("tool", {}) or {}).get("poetry", {})
    for k in (poetry.get("dependencies", {}) or {}):
        if k.lower() != "python":
            deps.append(k)
    # a [project] with packages but no console entry-points leans "library"
    has_scripts = bool(proj.get("scripts")) or bool((poetry.get("scripts")))
    is_lib = bool(proj or poetry) and not has_scripts
    return [d for d in deps if d], is_lib


def _parse_package_json(text: str) -> Tuple[List[str], bool, bool]:
    """Return (deps, has_entry, is_library)."""
    try:
        data = json.loads(text)
    except Exception:
        return [], False, False
    deps = list((data.get("dependencies") or {}).keys())
    deps += list((data.get("devDependencies") or {}).keys())
    scripts = data.get("scripts") or {}
    has_entry = bool(data.get("bin")) or "start" in scripts or "serve" in scripts
    # a package exposing "main"/"module"/"exports" but no bin/start reads library-ish
    is_lib = bool(data.get("main") or data.get("module") or data.get("exports")) and not has_entry
    return deps, has_entry, is_lib


def _parse_pom(text: str) -> Tuple[List[str], bool]:
    deps = re.findall(r"<artifactId>([^<]+)</artifactId>", text)
    packaging = re.search(r"<packaging>([^<]+)</packaging>", text)
    is_lib = bool(packaging) and packaging.group(1).strip() in ("jar", "pom") \
        and "spring-boot" not in text.lower()
    return [d.strip() for d in deps], is_lib


def _parse_gradle(text: str) -> List[str]:
    out = re.findall(r"(?:implementation|api|compile|runtimeOnly)\s*[\(']+([^'\")]+)", text)
    return [d.strip() for d in out]


def _parse_go_mod(text: str) -> List[str]:
    return [m.strip() for m in re.findall(r"^\s+([\w./-]+)\s+v[\d.]", text, re.M)]


def _parse_cargo(text: str) -> Tuple[List[str], bool]:
    deps: List[str] = []
    try:
        import tomllib
        data = tomllib.loads(text)
        deps = list((data.get("dependencies") or {}).keys())
        is_lib = "lib" in data and "bin" not in data
        return deps, is_lib
    except Exception:
        in_deps = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("["):
                in_deps = s.lower().startswith("[dependencies")
                continue
            if in_deps and "=" in s:
                deps.append(s.split("=", 1)[0].strip())
        return deps, "[lib]" in text and "[[bin]]" not in text


def _parse_gemfile(text: str) -> List[str]:
    return [m for m in re.findall(r"^\s*gem\s+['\"]([^'\"]+)['\"]", text, re.M)]


def _parse_csproj(text: str) -> List[str]:
    return re.findall(r'<PackageReference\s+Include="([^"]+)"', text)


# filename -> (build_system, kind_of_parse)
_MANIFEST_KINDS: Dict[str, str] = {
    "requirements.txt": "pip", "pyproject.toml": "pip", "setup.py": "pip",
    "package.json": "npm", "pom.xml": "maven", "build.gradle": "gradle",
    "build.gradle.kts": "gradle", "go.mod": "go-modules", "cargo.toml": "cargo",
    "gemfile": "bundler", "composer.json": "composer",
}


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect(files: Iterable[Tuple[str, str]], root: str = "") -> Dict[str, Any]:
    """Infer the project's stack from manifests + the source language histogram.

    ``files`` is an iterable of (path, text). Returns a deterministic detection
    artifact: languages (file/line histogram), build systems, manifests with
    their declared dependencies, library-vs-application, grounded stack cues
    (each tied to the dependency that supports it), and a human-readable summary.
    """
    files = list(files)
    lang_files: Dict[str, int] = {}
    lang_lines: Dict[str, int] = {}
    doc_files = config_files = other_files = 0
    has_entrypoint = False
    entry_langs: set = set()

    for path, text in files:
        lang = langspec.language_for(path)
        ext = langspec.ext_of(path)
        if lang is not None:
            lang_files[lang.name] = lang_files.get(lang.name, 0) + 1
            lang_lines[lang.name] = lang_lines.get(lang.name, 0) + (text.count("\n") + 1)
            for pat, kind in lang.entrypoints:
                if kind in ("main", "server") and re.search(pat, text, re.M):
                    has_entrypoint = True
                    entry_langs.add(lang.name)
                    break
        elif ext in langspec.DOC_EXTS:
            doc_files += 1
        elif ext in langspec.CONFIG_EXTS:
            config_files += 1
        else:
            other_files += 1

    # manifests + dependencies
    manifests: List[Dict[str, Any]] = []
    build_systems: List[str] = []
    all_deps: List[str] = []
    library_signal = False
    npm_entry = False
    for path, text in files:
        base = _basename(path).lower()
        kind = _MANIFEST_KINDS.get(base)
        if not kind:
            continue
        deps: List[str] = []
        if base in ("requirements.txt",):
            deps = _parse_requirements(text)
        elif base == "pyproject.toml":
            deps, lib = _parse_pyproject(text); library_signal |= lib
        elif base == "setup.py":
            deps = _parse_requirements(text); library_signal = True
        elif base == "package.json":
            deps, npm_e, lib = _parse_package_json(text)
            npm_entry |= npm_e; library_signal |= lib
        elif base == "pom.xml":
            deps, lib = _parse_pom(text); library_signal |= lib
        elif base in ("build.gradle", "build.gradle.kts"):
            deps = _parse_gradle(text)
        elif base == "go.mod":
            deps = _parse_go_mod(text)
        elif base == "cargo.toml":
            deps, lib = _parse_cargo(text); library_signal |= lib
        elif base == "gemfile":
            deps = _parse_gemfile(text)
        elif base == "composer.json":
            try:
                deps = list((json.loads(text).get("require") or {}).keys())
            except Exception:
                deps = []
        manifests.append({"file": path, "build_system": kind, "dependencies": sorted(set(deps))})
        if kind not in build_systems:
            build_systems.append(kind)
        all_deps += deps
    # .csproj is matched by suffix, not exact name
    for path, text in files:
        if _basename(path).lower().endswith(".csproj"):
            deps = _parse_csproj(text)
            manifests.append({"file": path, "build_system": "dotnet", "dependencies": sorted(set(deps))})
            if "dotnet" not in build_systems:
                build_systems.append("dotnet")
            all_deps += deps

    # grounded stack cues
    cues: List[Dict[str, str]] = []
    seen_cue: set = set()
    for dep in all_deps:
        dl = dep.lower()
        for token, category in _DEP_CUES:
            if token in dl and (category, dep) not in seen_cue:
                seen_cue.add((category, dep))
                cues.append({"category": category, "dependency": dep})

    # languages, sorted by file count
    total_code = sum(lang_files.values()) or 1
    languages = [
        {"language": name, "files": lang_files[name],
         "lines": lang_lines.get(name, 0),
         "share": round(lang_files[name] / total_code, 3)}
        for name in sorted(lang_files, key=lambda n: (-lang_files[n], n))
    ]
    primary = languages[0]["language"] if languages else None

    # application vs library (honest heuristic — stated, not asserted as fact)
    if has_entrypoint or npm_entry:
        kind = "application"
    elif library_signal:
        kind = "library"
    else:
        kind = "unknown"

    cue_categories = sorted({c["category"] for c in cues})
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "root": root,
        "primary_language": primary,
        "languages": languages,
        "build_systems": build_systems,
        "manifests": manifests,
        "kind": kind,
        "stack_cues": cues,
        "cue_categories": cue_categories,
        "counts": {"code_files": sum(lang_files.values()), "doc_files": doc_files,
                   "config_files": config_files, "other_files": other_files,
                   "manifest_count": len(manifests), "dependency_count": len(set(all_deps))},
        "summary": _summary(primary, languages, kind, build_systems, cue_categories,
                            sum(lang_files.values()), entry_langs),
    }


def _summary(primary: Optional[str], languages: List[Dict[str, Any]], kind: str,
             build_systems: List[str], cue_categories: List[str],
             code_files: int, entry_langs: set) -> str:
    if not primary:
        return "No recognized source languages detected; treated as a documents/config repository."
    others = [l["language"] for l in languages[1:4]]
    lang_str = primary + (f" (with {', '.join(others)})" if others else "")
    parts = [f"{kind.capitalize() if kind != 'unknown' else 'A'} {lang_str} project",
             f"{code_files} source file(s)"]
    if build_systems:
        parts.append("build: " + ", ".join(build_systems))
    if cue_categories:
        parts.append("stack cues: " + ", ".join(cue_categories))
    return ". ".join(parts) + "."
