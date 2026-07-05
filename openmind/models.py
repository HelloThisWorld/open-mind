"""Pydantic request bodies for the REST API. Responses are returned as plain
dicts built from the persistence/ extraction layers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateProjectReq(BaseModel):
    name: str
    path: Optional[str] = None
    exclude: List[str] = Field(default_factory=list)


class AddPathReq(BaseModel):
    path: str
    exclude: List[str] = Field(default_factory=list)


class SaveSelectionReq(BaseModel):
    path: str
    exclude: List[str] = Field(default_factory=list)


class SetTemplateReq(BaseModel):
    """Set or clear a project's template-profile override. name=None (or "")
    clears the override so the recorded auto-selection applies again."""
    name: Optional[str] = None


class SourceLinkReq(BaseModel):
    """Link a project to its source when no local copy resolves: either a local
    folder (becomes the machine-local source root) or a GitHub repo (fetched on
    demand). Stored machine-locally so a copied data/ folder stays trace-free."""
    kind: str                            # "local" | "github"
    path: Optional[str] = None           # kind=="local": absolute folder on this machine
    url: Optional[str] = None            # kind=="github": repo URL (or owner/repo)
    ref: Optional[str] = None            # kind=="github": optional branch/tag/commit


class IngestReq(BaseModel):
    project_id: str
    path: Optional[str] = None


class GendocsReq(BaseModel):
    project_id: str
    force: bool = False


class SearchReq(BaseModel):
    scope: str
    query: str
    k: int = 12
    case_sensitive: bool = True          # code identifiers are case-sensitive by default
    subword: bool = False                # also match camelCase/snake_case components
    exact: Optional[bool] = None         # force exact-token vs conceptual routing


class FileRef(BaseModel):
    file_path: str
    symbol: str = ""
    file_hash_at_save: str = ""


class AskReq(BaseModel):
    scope: str
    question: str
    k: int = 12
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class ClearAskReq(BaseModel):
    scope: str


class SaveAskCaseReq(BaseModel):
    exchange_id: str
    scope: Optional[str] = None


class SaveCaseReq(BaseModel):
    scope: Optional[str] = None
    project_id: Optional[str] = None
    problem_text: str
    resolution_summary: str
    involved_services: List[str] = Field(default_factory=list)
    involved_topics: List[str] = Field(default_factory=list)
    file_refs: List[Dict[str, Any]] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


class TerminateReq(BaseModel):
    clear_cases: bool = False


class RegenDocReq(BaseModel):
    scope: str
    page: Optional[str] = None


class EnrichEntry(BaseModel):
    term: str
    definition: str
    html: Optional[str] = None          # format-preserved Wikipedia quote (sanitized)
    url: Optional[str] = None
    title: Optional[str] = None


class EnrichGlossaryReq(BaseModel):
    """A batch of external standard definitions to attach to existing glossary
    terms. Produced by the wikipedia-glossary search skill; the server only merges
    the supplied text (it performs no web request itself)."""
    scope: str
    entries: List[EnrichEntry] = Field(default_factory=list)


class EnrichAutoReq(BaseModel):
    """Trigger the in-app enrichment ENGINE for a scope (queues a deterministic
    enrich job). Optionally updates the per-project enrichment settings first."""
    scope: str
    context: Optional[str] = None                       # disambiguation hint override
    pins: Dict[str, str] = Field(default_factory=dict)  # term -> exact article title
    block: List[str] = Field(default_factory=list)      # terms to never auto-enrich
    enabled: Optional[bool] = None                      # enable/disable auto-enrich
