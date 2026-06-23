"""Language registry — the extensible backbone of the detect-then-adapt design.

WHY THIS EXISTS (design intent)
-------------------------------
Open Mind must comprehend *any* repository, not one fixed stack. The honest way
to do that with a local (weaker) model is to keep the language knowledge in a
small, declarative, DETERMINISTIC table instead of hardcoding one ecosystem into
the core pipeline. Both Stage 1 (:mod:`openmind.detect`) and Stage 2
(:mod:`openmind.structure`) read this registry; nothing else encodes
per-language assumptions. Adding support for a new language is adding ONE entry
here — never editing the traversal, structure, diagram, or query code.

WHAT THIS IS (and is NOT)
-------------------------
These are line-oriented, boundary-checked HEURISTICS — deliberately not full
parsers. A regex registry cannot match a real grammar, and we do not pretend it
does: it recovers the *structural skeleton* (where definitions, imports, and
entry points are) reliably and cheaply, with zero heavy dependencies, on a
codebase in any of the languages below. Richer per-language parsing (e.g.
tree-sitter) can layer on top when available, but the base map never depends on
it — that is what guarantees Stage 2 produces meaningful output for every repo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Language:
    """A declarative description of one language's surface syntax.

    Every pattern is a regular expression applied per line (``re.MULTILINE``)
    by the consumers. ``defs``/``entrypoints`` patterns capture a single group
    (the symbol name / nothing for entry points); ``imports`` patterns capture
    the imported module/path in group 1.
    """
    name: str
    exts: Tuple[str, ...]
    line_comments: Tuple[str, ...] = ()
    block_comment: Optional[Tuple[str, str]] = None
    defs: Tuple[Tuple[str, str], ...] = ()          # (pattern, kind)
    imports: Tuple[str, ...] = ()                    # pattern -> group(1) = target
    entrypoints: Tuple[Tuple[str, str], ...] = ()    # (pattern, kind)
    # how an import target maps onto a project module path; see structure.resolve_import
    import_style: str = "none"


# ---------------------------------------------------------------------------
# The registry. Order is irrelevant; lookup is by extension.
# ---------------------------------------------------------------------------
LANGUAGES: Tuple[Language, ...] = (
    Language(
        name="Python", exts=(".py",), line_comments=("#",),
        defs=(
            (r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", "class"),
        ),
        imports=(
            r"^[ \t]*import[ \t]+([A-Za-z_][\w.]*)",
            r"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import\b",
        ),
        entrypoints=(
            (r"^if[ \t]+__name__[ \t]*==[ \t]*['\"]__main__['\"]", "main"),
            (r"^[ \t]*def[ \t]+main[ \t]*\(", "main"),
            (r"@\w+\.(?:get|post|put|delete|patch|route|websocket)\b", "route"),
        ),
        import_style="dotted",
    ),
    Language(
        name="JavaScript", exts=(".js", ".jsx", ".mjs", ".cjs"),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)", "function"),
            (r"^[ \t]*(?:export[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)", "class"),
            (r"^[ \t]*(?:export[ \t]+)?const[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]*)?\(", "function"),
        ),
        imports=(
            r"^[ \t]*import\b[^'\"]*from[ \t]*['\"]([^'\"]+)['\"]",
            r"\brequire\([ \t]*['\"]([^'\"]+)['\"][ \t]*\)",
        ),
        entrypoints=(
            (r"\b\w+\.(?:get|post|put|delete|patch|use)\([ \t]*['\"]", "route"),
            (r"\.listen\([ \t]*\d", "server"),
        ),
        import_style="relative",
    ),
    Language(
        name="TypeScript", exts=(".ts", ".tsx"),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)", "function"),
            (r"^[ \t]*(?:export[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)", "class"),
            (r"^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_$][\w$]*)", "interface"),
            (r"^[ \t]*(?:export[ \t]+)?type[ \t]+([A-Za-z_$][\w$]*)", "type"),
            (r"^[ \t]*(?:export[ \t]+)?enum[ \t]+([A-Za-z_$][\w$]*)", "enum"),
            (r"^[ \t]*(?:export[ \t]+)?const[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]*)?\(", "function"),
        ),
        imports=(
            r"^[ \t]*import\b[^'\"]*from[ \t]*['\"]([^'\"]+)['\"]",
            r"\brequire\([ \t]*['\"]([^'\"]+)['\"][ \t]*\)",
        ),
        entrypoints=(
            (r"@(?:Get|Post|Put|Delete|Patch)\b", "route"),
            (r"\b\w+\.(?:get|post|put|delete|patch|use)\([ \t]*['\"]", "route"),
            (r"\.listen\([ \t]*\d", "server"),
        ),
        import_style="relative",
    ),
    Language(
        name="Java", exts=(".java",),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:public|private|protected)?[ \t]*(?:abstract|final|static|sealed)?[ \t]*class[ \t]+([A-Za-z_]\w*)", "class"),
            (r"^[ \t]*(?:public|private|protected)?[ \t]*interface[ \t]+([A-Za-z_]\w*)", "interface"),
            (r"^[ \t]*(?:public|private|protected)?[ \t]*enum[ \t]+([A-Za-z_]\w*)", "enum"),
            (r"^[ \t]*(?:public|private|protected)?[ \t]*record[ \t]+([A-Za-z_]\w*)", "record"),
        ),
        imports=(r"^[ \t]*import[ \t]+(?:static[ \t]+)?([\w.]+)",),
        entrypoints=(
            (r"public[ \t]+static[ \t]+void[ \t]+main[ \t]*\(", "main"),
            (r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\b", "route"),
        ),
        import_style="dotted",
    ),
    Language(
        name="Kotlin", exts=(".kt", ".kts"),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:public|private|internal|protected)?[ \t]*(?:suspend[ \t]+)?fun[ \t]+([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*(?:public|private|internal|protected)?[ \t]*(?:data[ \t]+|sealed[ \t]+|abstract[ \t]+)?class[ \t]+([A-Za-z_]\w*)", "class"),
            (r"^[ \t]*(?:public|private|internal|protected)?[ \t]*(?:object|interface)[ \t]+([A-Za-z_]\w*)", "type"),
        ),
        imports=(r"^[ \t]*import[ \t]+([\w.]+)",),
        entrypoints=((r"^[ \t]*fun[ \t]+main[ \t]*\(", "main"),),
        import_style="dotted",
    ),
    Language(
        name="Scala", exts=(".scala", ".sc"),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:def)[ \t]+([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*(?:final[ \t]+|abstract[ \t]+|sealed[ \t]+|case[ \t]+)*(?:class|trait|object)[ \t]+([A-Za-z_]\w*)", "class"),
        ),
        imports=(r"^[ \t]*import[ \t]+([\w.]+)",),
        entrypoints=(
            (r"^[ \t]*def[ \t]+main[ \t]*\(", "main"),
            (r"\bextends[ \t]+App\b", "main"),
        ),
        import_style="dotted",
    ),
    Language(
        name="Go", exts=(".go",),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*func[ \t]+(?:\([^)]*\)[ \t]*)?([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*type[ \t]+([A-Za-z_]\w*)[ \t]+(?:struct|interface)\b", "type"),
        ),
        imports=(r'^[ \t]*[A-Za-z_.]*[ \t]*"([^"]+)"[ \t]*$',),   # inside import ( ... ) blocks
        entrypoints=((r"^[ \t]*func[ \t]+main[ \t]*\(", "main"),),
        import_style="path",
    ),
    Language(
        name="Rust", exts=(".rs",),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?fn[ \t]+([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*(?:pub[ \t]+)?(?:struct|enum|trait)[ \t]+([A-Za-z_]\w*)", "type"),
        ),
        imports=(r"^[ \t]*use[ \t]+([\w:]+)",),
        entrypoints=((r"^[ \t]*fn[ \t]+main[ \t]*\(", "main"),),
        import_style="colons",
    ),
    Language(
        name="C#", exts=(".cs",),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:public|private|protected|internal)?[ \t]*(?:abstract|sealed|static|partial)?[ \t]*class[ \t]+([A-Za-z_]\w*)", "class"),
            (r"^[ \t]*(?:public|private|protected|internal)?[ \t]*interface[ \t]+([A-Za-z_]\w*)", "interface"),
            (r"^[ \t]*(?:public|private|protected|internal)?[ \t]*(?:struct|enum)[ \t]+([A-Za-z_]\w*)", "type"),
        ),
        imports=(r"^[ \t]*using[ \t]+(?:static[ \t]+)?([\w.]+)",),
        entrypoints=(
            (r"static[ \t]+(?:async[ \t]+)?(?:void|Task|int)[ \t]+Main[ \t]*\(", "main"),
            (r"\[Http(?:Get|Post|Put|Delete|Patch)\]", "route"),
        ),
        import_style="dotted",
    ),
    Language(
        name="Ruby", exts=(".rb",), line_comments=("#",),
        defs=(
            (r"^[ \t]*def[ \t]+([A-Za-z_]\w*[!?=]?)", "function"),
            (r"^[ \t]*(?:class|module)[ \t]+([A-Za-z_]\w*)", "class"),
        ),
        imports=(
            r"^[ \t]*require(?:_relative)?[ \t]+['\"]([^'\"]+)['\"]",
        ),
        entrypoints=((r"^if[ \t]+__FILE__[ \t]*==[ \t]*\$0", "main"),),
        import_style="relative",
    ),
    Language(
        name="PHP", exts=(".php",),
        line_comments=("//", "#"), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:public|private|protected|static|final|abstract)*[ \t]*function[ \t]+([A-Za-z_]\w*)", "function"),
            (r"^[ \t]*(?:abstract[ \t]+|final[ \t]+)?(?:class|interface|trait)[ \t]+([A-Za-z_]\w*)", "class"),
        ),
        imports=(
            r"^[ \t]*use[ \t]+([\w\\]+)",
            r"\b(?:require|include)(?:_once)?[ \t]*\(?['\"]([^'\"]+)['\"]",
        ),
        import_style="namespaced",
    ),
    Language(
        name="C", exts=(".c", ".h"), line_comments=("//",), block_comment=("/*", "*/"),
        defs=((r"^[A-Za-z_][\w \t\*]*[ \t\*]([A-Za-z_]\w*)[ \t]*\([^;]*\)[ \t]*\{", "function"),),
        imports=(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]',),
        entrypoints=((r"\bint[ \t]+main[ \t]*\(", "main"),),
        import_style="include",
    ),
    Language(
        name="C++", exts=(".cpp", ".cc", ".cxx", ".hpp", ".hh"),
        line_comments=("//",), block_comment=("/*", "*/"),
        defs=(
            (r"^[ \t]*(?:class|struct)[ \t]+([A-Za-z_]\w*)", "class"),
            (r"^[A-Za-z_][\w \t\*:<>]*[ \t\*:]([A-Za-z_]\w*)[ \t]*\([^;]*\)[ \t]*\{", "function"),
        ),
        imports=(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]',),
        entrypoints=((r"\bint[ \t]+main[ \t]*\(", "main"),),
        import_style="include",
    ),
)

# extension -> Language (lowercase ext)
EXT_INDEX: Dict[str, Language] = {ext: lang for lang in LANGUAGES for ext in lang.exts}

# Extensions we treat as documentation/config (covered by traversal + glossary,
# but not by the code-structure extractors).
DOC_EXTS = {".md", ".markdown", ".rst", ".adoc", ".txt"}
CONFIG_EXTS = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".properties", ".xml", ".json"}


def language_for(path: str) -> Optional[Language]:
    """Return the Language for a path by extension, or None (doc/config/unknown)."""
    i = path.rfind(".")
    if i == -1:
        return None
    return EXT_INDEX.get(path[i:].lower())


def ext_of(path: str) -> str:
    i = path.rfind(".")
    return path[i:].lower() if i != -1 else ""
