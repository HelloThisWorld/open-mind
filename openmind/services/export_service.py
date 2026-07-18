"""Artifact export use case.

A deliberately thin wrapper over :func:`openmind.artifacts.generate_artifacts`.
The ``.openmind`` directory is a FROZEN integration contract (schema 1.1.0) that
external consumers depend on, so this service adds no fields, reorders nothing,
and changes no defaults. Its whole job is to make export reachable from the CLI
and from tests the same way it is reachable from
``python -m openmind.artifacts``.

Export is standalone by design: no web app, no model server, no vector store, no
database. This service therefore does NOT require a bootstrapped runtime, and
the CLI's ``export`` command runs without initializing one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .. import artifacts
from ..domain.errors import InvalidRequest


class ExportService:
    """Generate the deterministic ``.openmind`` artifact directory."""

    #: The artifact schema version this build writes. Frozen for this phase.
    schema_version = artifacts.SCHEMA_VERSION

    def export(self, repo: str, output: str, name: Optional[str] = None,
               template: Optional[str] = None, no_template: bool = False,
               generated_at: Optional[str] = None) -> Dict[str, Any]:
        """Analyze *repo* and write artifacts into *output*.

        *generated_at* overrides the manifest timestamp, which is what makes a
        reproducible build byte-identical across runs.
        """
        if not (repo or "").strip():
            raise InvalidRequest("repo path must not be empty",
                                 details={"field": "repo"})
        if not (output or "").strip():
            raise InvalidRequest("output path must not be empty",
                                 details={"field": "output"})
        if template and no_template:
            raise InvalidRequest(
                "--template and --no-template are mutually exclusive",
                details={"template": template})

        repo_path = Path(repo)
        if not repo_path.is_dir():
            raise InvalidRequest(f"repository not found: {repo}",
                                 details={"repo": str(repo)})

        try:
            summary = artifacts.generate_artifacts(
                repo, output, name=name, generated_at=generated_at,
                template=template, no_template=no_template)
        except (FileNotFoundError, ValueError) as exc:
            # The exporter's own argument/IO failures are caller errors, not
            # crashes. Anything else propagates untouched.
            raise InvalidRequest(str(exc), details={"repo": str(repo),
                                                    "output": str(output)}) from exc

        summary["schemaVersion"] = self.schema_version
        return summary


__all__ = ["ExportService"]
