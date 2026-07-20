"""Central configuration, paths, and defaults.

Everything here is local. The only network egress in the whole app is the LLM
call, which is pinned to a localhost OpenAI-compatible endpoint and routed
through ``netguard`` (Invariant 1).
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Kill third-party telemetry BEFORE any such library is imported anywhere.
# ChromaDB sends anonymized usage stats to PostHog by default; that is egress
# we forbid (Invariant 1). We also default HuggingFace/fastembed to a
# *deliberate* online mode for one-time weight download only (no project
# content), overridable to fully offline via OPENMIND_EMBED_OFFLINE=1.
# ---------------------------------------------------------------------------
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")
os.environ.setdefault("CHROMA_SERVER_NOFILE", "0")
os.environ.setdefault("POSTHOG_DISABLED", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
if os.environ.get("OPENMIND_EMBED_OFFLINE") == "1":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = Path(os.environ.get("OPENMIND_DATA_DIR", ROOT_DIR / "data"))
STATIC_DIR = APP_DIR / "static"

DB_PATH = DATA_DIR / "openmind.db"
CHROMA_DIR = DATA_DIR / "chroma"
OUTBOUND_LOG = DATA_DIR / "outbound.log"


def project_dir(project_id: str) -> Path:
    return DATA_DIR / project_id


def project_map_dir(project_id: str) -> Path:
    return project_dir(project_id) / "map"


def project_docs_dir(project_id: str) -> Path:
    return project_dir(project_id) / "docs"


def ensure_dirs() -> None:
    for p in (DATA_DIR, CHROMA_DIR, STATIC_DIR):
        p.mkdir(parents=True, exist_ok=True)


def ensure_project_dirs(project_id: str) -> None:
    for p in (project_dir(project_id), project_map_dir(project_id), project_docs_dir(project_id)):
        p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Local-only network policy (Invariant 1)
# ---------------------------------------------------------------------------
# Hosts the app is ALLOWED to talk to. Loopback ONLY — note 0.0.0.0 is the
# wildcard BIND address (all interfaces), not loopback, so it is deliberately
# excluded: treating it as "local" would let the model API be exposed to the LAN.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTS = set(LOOPBACK_HOSTS)

# Outbound enrichment egress (Invariant 1, relaxed + AUDITED). The glossary
# enrichment engine may reach these public reference hosts to fetch standard
# definitions. This is NOT general egress: only the enrichment path may use it
# (the LLM/model calls stay loopback-only), only single glossary TERMS are sent
# (never project source), and every call is still logged to OUTBOUND_LOG /netlog.
# Set OPENMIND_ENRICH_EGRESS=0 to re-lock to fully local.
ENRICH_EGRESS = os.environ.get("OPENMIND_ENRICH_EGRESS", "1") != "0"
ENRICH_ALLOWED_HOSTS = {"wikipedia.org"}
ENRICH_ALLOWED_SUFFIXES = {".wikipedia.org"}

# Source-link egress (Invariant 1, relaxed + AUDITED). When a project's source is
# not on this machine (a copied data/ folder, or the checkout is gone) the user may
# LINK the project to its GitHub repo; "jump to source" then fetches the file from
# raw.githubusercontent.com on demand. This is its OWN egress category, distinct from
# enrichment so the audit log can tell them apart: only the source-link path may use
# it, only a PUBLIC repo file path is requested (never project content goes out), the
# fetch is gated to the project's already-indexed sources, and every call is logged to
# OUTBOUND_LOG /netlog. Set OPENMIND_SOURCELINK_EGRESS=0 to re-lock to fully local
# (the link URL is still saved; only the dynamic fetch is blocked).
SOURCELINK_EGRESS = os.environ.get("OPENMIND_SOURCELINK_EGRESS", "1") != "0"
SOURCELINK_ALLOWED_HOSTS = {"raw.githubusercontent.com"}


def coerce_loopback(host: str) -> str:
    """Force any non-loopback host (e.g. 0.0.0.0 or a LAN IP) to 127.0.0.1 so
    the local llama-server is never bound to a public interface."""
    h = (host or "").strip().lower()
    return host if h in LOOPBACK_HOSTS else "127.0.0.1"


# ---------------------------------------------------------------------------
# Model-server defaults (LM-Studio-like; every field user-editable + persisted)
# ---------------------------------------------------------------------------
# Defaults are best-effort and carry NO machine-specific path in the source: a
# dev points them at their own setup via the OPENMIND_* env vars below, and the
# Model panel lets you edit and persist every field at runtime (a .gguf picker
# browses the local FS). The first candidate path that exists wins, else we fall
# back to a generic location / PATH lookup. A large ctx_size + -ngl 999 can OOM a
# small GPU; the launcher surfaces that on failure and hints to drop ctx_size.
def _first_existing(*candidates: str) -> str:
    for c in candidates:
        if c and Path(c).exists():
            return c
    return candidates[0] if candidates else ""


DEFAULT_MODELS_FOLDER = _first_existing(
    os.environ.get("OPENMIND_MODELS_FOLDER", ""),
    str(Path.home() / "models"),
    str(ROOT_DIR),
)

DEFAULT_MODEL_PATH = _first_existing(
    os.environ.get("OPENMIND_MODEL_PATH", ""),
    "",
)

DEFAULT_LLAMA_SERVER = _first_existing(
    os.environ.get("OPENMIND_LLAMA_SERVER", ""),
    "llama-server",  # fall back to PATH lookup
)

DEFAULT_MODEL_CONFIG: dict = {
    "llama_server_path": DEFAULT_LLAMA_SERVER,
    "model_path": DEFAULT_MODEL_PATH,
    "n_gpu_layers": 999,     # -ngl
    "ctx_size": 32768,       # -c   (drop to 16384 on OOM)
    "parallel": 1,           # --parallel
    "flash_attn": True,      # -fa
    "cache_type_k": "q8_0",  # -ctk
    "cache_type_v": "q8_0",  # -ctv
    "jinja": True,           # --jinja
    "host": "127.0.0.1",
    "port": 7080,
    "threads": 12,           # --threads
    "extra_args": "",        # appended verbatim
}

# ---------------------------------------------------------------------------
# Ingestion / walker policy
# ---------------------------------------------------------------------------
# Java sources get tree-sitter semantic chunking; every other indexable type
# gets whole-file RAG chunks. The walker is architecture-agnostic — it indexes
# common source/config types so code search and the deterministic glossary work
# for ANY repository (a monolith or a multi-module system alike).
INDEX_EXTENSIONS = {
    # JVM + config
    ".java", ".kt", ".kts", ".groovy", ".gradle", ".scala",
    ".properties", ".yml", ".yaml", ".xml", ".sql",
    # frontend / web
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less",
    # other common source
    ".go", ".py", ".rb", ".cs", ".php", ".rs",
}

# directories never descended into (in addition to user exclude-set + .gitignore)
DEFAULT_IGNORE_DIRS = {
    ".git", ".svn", ".hg", "target", "build", "out", "dist", "bin",
    "node_modules", ".gradle", ".mvn", ".idea", ".vscode", ".settings",
    "__pycache__", ".pytest_cache", ".venv", "venv", "obj",
}

BINARY_EXTENSIONS = {
    ".class", ".jar", ".war", ".ear", ".zip", ".tar", ".gz", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".pdf", ".so", ".dll", ".exe", ".bin", ".lock",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".gguf",
}

MAX_FILE_BYTES = 2_000_000  # skip files larger than this

OCR_MAX_UPLOAD_BYTES = 50_000_000  # /ocr rejects larger image uploads (413) instead of buffering them

# ---------------------------------------------------------------------------
# Document ingestion policy (OpenMind v2 Phase 3)
# ---------------------------------------------------------------------------
# Code discovery and DOCUMENT discovery are separate policies on purpose. A
# `.pdf` or `.docx` must never reach walker.read_text (it would decode binary
# noise and index it as source), so no document extension is added to
# INDEX_EXTENSIONS; the document walk has its own extension set, its own size
# limit, and its own parser plane.
CODE_INDEX_EXTENSIONS = INDEX_EXTENSIONS      # readable alias; same set, unchanged

TEXT_DOCUMENT_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt",
}
BINARY_DOCUMENT_EXTENSIONS = {".docx", ".pdf", ".xlsx"}
STRUCTURED_DOCUMENT_EXTENSIONS = {".csv", ".tsv"}

#: Everything a document parser can claim when a user ATTACHES a file explicitly.
#: Wider than the discovery set: `.html`, `.yaml`, `.json` and `.sql` documents
#: are perfectly parseable on demand.
DOCUMENT_EXTENSIONS = (TEXT_DOCUMENT_EXTENSIONS | BINARY_DOCUMENT_EXTENSIONS
                       | STRUCTURED_DOCUMENT_EXTENSIONS
                       | {".html", ".htm", ".yaml", ".yml", ".json", ".sql"})

#: What a WORKSPACE walk discovers as a document. The subtraction is
#: load-bearing: a file the code pipeline already owns (.html/.yaml/.json/.sql)
#: must not also be ingested as a document, or one file would become two Assets.
#: Those formats stay reachable through `openmind document add`.
DOCUMENT_DISCOVERY_EXTENSIONS = (
    (TEXT_DOCUMENT_EXTENSIONS | BINARY_DOCUMENT_EXTENSIONS
     | STRUCTURED_DOCUMENT_EXTENSIONS) - CODE_INDEX_EXTENSIONS)


def _int_env(name: str, default: int) -> int:
    """An OPENMIND_* integer override, ignoring anything unparseable or <= 0 —
    a typo must not silently disable a safety limit."""
    try:
        value = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Parser resource limits. Every one is enforced by the parser itself, and hitting
# one produces status='partial' + an exact warning naming the limit — never a
# silent truncation. Documents are untrusted input; these are the blast radius.
DOCUMENT_MAX_BYTES = _int_env("OPENMIND_DOCUMENT_MAX_BYTES", 25_000_000)
PDF_MAX_PAGES = _int_env("OPENMIND_PDF_MAX_PAGES", 2_000)
DOCX_MAX_PARAGRAPHS = _int_env("OPENMIND_DOCX_MAX_PARAGRAPHS", 50_000)
DOCX_MAX_TABLES = _int_env("OPENMIND_DOCX_MAX_TABLES", 2_000)
XLSX_MAX_SHEETS = _int_env("OPENMIND_XLSX_MAX_SHEETS", 100)
XLSX_MAX_ROWS_PER_SHEET = _int_env("OPENMIND_XLSX_MAX_ROWS_PER_SHEET", 20_000)
XLSX_MAX_CELLS = _int_env("OPENMIND_XLSX_MAX_CELLS", 500_000)
CSV_MAX_ROWS = _int_env("OPENMIND_CSV_MAX_ROWS", 50_000)
DOCUMENT_MAX_BLOCKS = _int_env("OPENMIND_DOCUMENT_MAX_BLOCKS", 20_000)
DOCUMENT_MAX_BLOCK_CHARS = _int_env("OPENMIND_DOCUMENT_MAX_BLOCK_CHARS", 20_000)

# ZIP package safety for the OOXML formats (DOCX/XLSX). A document archive is
# never extracted to a filesystem path; these bound what may be read INTO MEMORY
# from an already-validated archive.
ZIP_MAX_MEMBERS = _int_env("OPENMIND_ZIP_MAX_MEMBERS", 2_000)
ZIP_MAX_TOTAL_BYTES = _int_env("OPENMIND_ZIP_MAX_TOTAL_BYTES", 400_000_000)
ZIP_MAX_MEMBER_BYTES = _int_env("OPENMIND_ZIP_MAX_MEMBER_BYTES", 100_000_000)
ZIP_MAX_RATIO = _int_env("OPENMIND_ZIP_MAX_RATIO", 200)

# Template profiles (declarative framework/architecture lenses; see openmind.templates).
# Built-ins ship with the package; users drop their own .yaml/.json into the
# data-dir folder — same schema, user files override built-ins by name.
BUILTIN_TEMPLATES_DIR = APP_DIR / "templates"
USER_TEMPLATES_DIR = DATA_DIR / "templates"

# RAG
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"  # fastembed; hashing fallback if unavailable
EMBED_DIM = 384
CHUNK_MAX_LINES = 220       # split oversized methods
CHUNK_OVERLAP_LINES = 20

# ---------------------------------------------------------------------------
# Ingest resource governor (see openmind.resources)
# ---------------------------------------------------------------------------
# Keep the machine responsive during a long ingest without ever stalling it. The
# soft target is a comfortable headroom used ONLY to gate optional work; the hard
# floor is a small ABSOLUTE amount below which the OS is about to swap-thrash —
# only then does the guard pause briefly and, if it stays critical, abort cleanly
# instead of freezing the box. Both scale to the machine (psutil); with psutil
# absent every guard is a safe no-op.
INGEST_MIN_FREE_FRACTION = 0.15        # comfortable headroom: 15% of total RAM
INGEST_MIN_FREE_MB = 1024              # ...or 1 GB, whichever is larger
INGEST_CRITICAL_FREE_FRACTION = 0.0    # hard floor is absolute (no fraction): a big
                                       # box with a large resident model isn't 'critical'
INGEST_CRITICAL_FREE_MB = 300          # abort only when free RAM falls below ~300 MB
INGEST_MEM_WAIT_S = 20.0               # max pause for a transient critical spike to clear

# Auto-free the GPU for the embed phase. Ingest never uses the LLM, so if our own
# MANAGED model server is resident on the GPU (holding VRAM, idle), stop it for the
# duration of bulk embedding so DirectML can use the GPU (≈5× faster than CPU), then
# auto-restart it. Disable with OPENMIND_INGEST_FREE_GPU=0 (then it falls back to CPU
# while the model stays loaded). Never touches an ATTACHED external server.
INGEST_FREE_GPU = os.environ.get("OPENMIND_INGEST_FREE_GPU", "1") != "0"

# LLM generation
LLM_TIMEOUT = 600.0          # generous default for an explicit chat
SUMMARY_TIMEOUT = 60.0       # per-service summary call — short so a slow/loading
                             # model can't block the ingest worker (and the job
                             # queue) for minutes; falls back to deterministic.
LLM_TEMPERATURE = 0.2
