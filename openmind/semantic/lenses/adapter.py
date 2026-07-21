"""Built-in Template → Lens projection.

The existing Template Profiles keep doing exactly what they do — detection,
facets, guide rendering, ``.openmind`` export — untouched. This adapter gives
the SEMANTIC planner a read-only lens VIEW of them: template ``match`` maps
onto lens ``match``, template roles onto lens roles, template facets onto
lens ``identifiers`` (kind ``facet``), and the guide's section metadata rides
along at the record level for display. The projection is recomputed from the
template files on every call, so editing a template updates its built-in
lens with no migration and no stored copy — until the moment a built-in lens
is ACTIVATED, at which point the service materializes a snapshot row.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ... import templates
from .models import LENS_SCHEMA_VERSION, validate_lens_definition

#: The id namespace for virtual (un-materialized) built-in lenses.
BUILTIN_ID_PREFIX = "builtin:"


def template_to_definition(template: "templates.Template") -> Dict[str, Any]:
    """Project one valid Template into the closed lens schema."""
    definition: Dict[str, Any] = {
        "schemaVersion": LENS_SCHEMA_VERSION,
        "name": template.name,
        "title": template.title or template.name,
        "description": template.description,
        "match": {
            "languages": list(template.match.get("languages") or []),
            "dependencies": list(template.match.get("dependencies") or []),
            "markerFiles": list(template.match.get("marker_files") or []),
            "pathGlobs": [],
            "documentTitlePatterns": [],
            "documentTypes": [],
        },
        "roles": [{
            "name": role["name"],
            "title": role.get("title") or role["name"],
            "pathGlobs": list(role.get("path_globs") or []),
            "namePatterns": list(role.get("name_patterns") or []),
            "annotations": list(role.get("annotations") or []),
        } for role in template.roles],
        "identifiers": [{
            "name": facet["name"],
            "kind": "facet",
            "pattern": facet.get("pattern") or "",
            "examples": [],
        } for facet in template.facets if facet.get("pattern")],
        "documentPatterns": [],
        "semanticTasks": [],
        "relationHints": [],
        "validation": {"minimumAssetCoverage": 0.0,
                       "maximumRoleOverlap": 1.0},
        "sampleEvidenceIds": [],
    }
    return definition


def builtin_lens_records() -> List[Dict[str, Any]]:
    """Virtual lens records for every VALID template, deterministic order.
    Invalid templates are not projected (they are already listed with their
    errors by the template surface itself)."""
    records: List[Dict[str, Any]] = []
    for summary in templates.list_templates():
        template = templates.get_template(summary["name"])
        if template is None:
            continue
        definition = template_to_definition(template)
        normalized, errors, warnings = validate_lens_definition(
            definition, source="builtin")
        records.append({
            "id": BUILTIN_ID_PREFIX + template.name,
            "workspace_id": "",
            "organization_key": "",
            "name": template.name,
            "version": template.schema_version or "1",
            "source": "builtin",
            "status": "validated" if not errors else "provisional",
            "schema_version": LENS_SCHEMA_VERSION,
            "definition": normalized,
            "validation": {"result": "valid" if not errors else "invalid",
                           "errors": errors, "warnings": warnings},
            "stored": False,
            "template": {"name": template.name, "source": template.source,
                         "guide_sections": [g.get("section")
                                            for g in template.guide]},
        })
    return records


def get_builtin_lens(name_or_id: str) -> Optional[Dict[str, Any]]:
    name = str(name_or_id or "")
    if name.startswith(BUILTIN_ID_PREFIX):
        name = name[len(BUILTIN_ID_PREFIX):]
    for record in builtin_lens_records():
        if record["name"] == name:
            return record
    return None


__all__ = ["builtin_lens_records", "get_builtin_lens",
           "template_to_definition", "BUILTIN_ID_PREFIX"]
