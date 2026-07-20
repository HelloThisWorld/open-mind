"""OpenAPI 2.0 / 3.x descriptions, in JSON or YAML.

DETECTION IS STRUCTURAL
-----------------------
An OpenAPI document is recognized by its own top-level keys (``openapi`` /
``swagger`` plus ``paths`` or ``components``), never by being called
``openapi.yaml``. A file named ``openapi.yaml`` that is really a Helm values file
is not claimed, and a spec named ``api-v3.yml`` is. Detection reads a bounded
head sample, so probing costs nothing on a large file.

SAFETY
------
YAML is loaded with ``yaml.safe_load`` — the constructor for arbitrary Python
objects is never reachable. Remote ``$ref`` targets are **never fetched**;
external references are recorded as unsupported content so their absence is
visible rather than silent.

WHAT IS EXTRACTED, AND WHAT IS NOT
----------------------------------
Blocks for API metadata, every path operation, its parameters, request body and
responses, and every component schema — each with a JSON Pointer locator, which
is the format's own canonical addressing scheme and therefore the most portable
citation available.

No business meaning is inferred. An operation's block text is its declared
summary, description, parameters and response codes; nothing about what the
endpoint is *for* is invented.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ..domain.types import ContentMode, DocumentBlockType
from .builder import DocumentBuilder, slug
from .models import (DocumentParseContext, DocumentProbe, ParsedDocument,
                     dependency_unavailable)
from .security import decode_text

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch",
                 "trace")
_EXTENSIONS = frozenset({".json", ".yaml", ".yml"})


def _escape_pointer(token: str) -> str:
    """RFC 6901 escaping: ``~`` -> ``~0``, ``/`` -> ``~1``. Without it a path
    like ``/name-check`` would split the pointer into two segments."""
    return str(token).replace("~", "~0").replace("/", "~1")


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf"):
        return False
    if probe.extension not in _EXTENSIONS:
        return False
    head = probe.head.decode("utf-8", "replace")
    # Structural keys at the start of the document. Checking the raw head keeps
    # the probe cheap; the parser re-validates on the full parsed object and
    # declines honestly if the head was misleading.
    has_version = ('"openapi"' in head or "openapi:" in head
                   or '"swagger"' in head or "swagger:" in head)
    has_body = ('"paths"' in head or "paths:" in head
                or '"components"' in head or "components:" in head)
    return has_version and has_body


class OpenApiParser:
    """Blocks for API metadata, operations, parameters, responses and schemas."""

    name = "openapi"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             title=context.filename or context.logical_key)
        spec, media, error = _load(text, context.logical_key)
        if error is not None:
            return _decline(doc, error)
        if not isinstance(spec, dict):
            return _decline(doc, "the document is not a JSON/YAML object")
        version = spec.get("openapi") or spec.get("swagger")
        if not version:
            return _decline(doc,
                            "the document has no 'openapi' or 'swagger' version "
                            "key, so it is not an OpenAPI description")
        doc.media_type = media
        if lossy:
            doc.add_warning("decode-fallback",
                            "the file is not valid UTF-8; it was decoded with "
                            "replacement characters", encoding=encoding)

        info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
        title = str(info.get("title") or "").strip()
        if title:
            doc.title = title
        # info.version is an EXPLICIT, documented field — the only source of a
        # version label here. Nothing is inferred from prose or the filename.
        api_version = str(info.get("version") or "").strip()
        if api_version:
            doc.metadata.version_label = api_version
        doc.metadata.extra["openapi_version"] = str(version)

        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        key = context.logical_key
        builder.add_root(doc.title, _locator(key, ""))
        _emit_info(builder, key, spec, info, str(version))
        _emit_paths(builder, key, spec, doc)
        _emit_components(builder, key, spec, doc)
        _note_external_refs(spec, doc)
        return doc


def _decline(doc: ParsedDocument, reason: str) -> ParsedDocument:
    """Not an OpenAPI document after all — report ``unsupported``, never an
    empty successful parse."""
    from ..domain.types import DocumentParseStatus
    doc.status = DocumentParseStatus.UNSUPPORTED
    doc.reason = "not_openapi"
    doc.add_warning("not-openapi", reason)
    return doc


def _load(text: str, logical_key: str) -> Tuple[Any, str, Optional[str]]:
    """(document, media_type, error). JSON first, then safe YAML."""
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(text), "application/json", None
        except json.JSONDecodeError as exc:
            return None, "", f"the JSON document could not be parsed: {exc}"
    try:
        import yaml
    except ImportError:
        return None, "", "PyYAML is required to read a YAML OpenAPI description"
    try:
        # safe_load ONLY: yaml.load would allow arbitrary Python construction
        # from an untrusted document.
        return yaml.safe_load(text), "application/yaml", None
    except Exception as exc:
        return None, "", f"the YAML document could not be parsed: {exc}"


def _locator(document: str, pointer: str) -> Dict[str, Any]:
    return {"kind": "json-pointer", "document": document,
            "pointer": pointer or ""}


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _emit_info(builder: DocumentBuilder, key: str, spec: Dict[str, Any],
               info: Dict[str, Any], version: str) -> None:
    lines = [f"OpenAPI {version}"]
    for field in ("title", "version", "description", "termsOfService"):
        value = info.get(field)
        if value:
            lines.append(f"{field}: {_text_of(value)}")
    servers = spec.get("servers")
    if isinstance(servers, list):
        for server in servers:
            if isinstance(server, dict) and server.get("url"):
                lines.append(f"server: {server['url']}")
    elif spec.get("host"):                              # swagger 2.0
        lines.append(f"host: {spec['host']}{spec.get('basePath', '')}")
    builder.add(DocumentBlockType.SECTION, "\n".join(lines), key="api-info",
                locator=_locator(key, "/info"),
                content_mode=ContentMode.DERIVED, indexable=True,
                metadata={"openapi": version})


def _emit_paths(builder: DocumentBuilder, key: str, spec: Dict[str, Any],
                doc: ParsedDocument) -> None:
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return
    operations = 0
    for path, item in sorted(paths.items()):
        if builder.full or not isinstance(item, dict):
            continue
        path_pointer = f"/paths/{_escape_pointer(path)}"
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            operations += 1
            _emit_operation(builder, key, path, method, operation,
                            f"{path_pointer}/{method}", item)
    doc.coverage["operations"] = operations


def _emit_operation(builder: DocumentBuilder, key: str, path: str, method: str,
                    operation: Dict[str, Any], pointer: str,
                    path_item: Dict[str, Any]) -> None:
    upper = method.upper()
    lines = [f"{upper} {path}"]
    for field in ("operationId", "summary", "description"):
        value = operation.get(field)
        if value:
            lines.append(f"{field}: {_text_of(value)}")
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        lines.append("tags: " + ", ".join(str(t) for t in tags))

    parameters = _collect_parameters(operation, path_item)
    for param in parameters:
        lines.append(
            f"parameter: {param.get('name', '?')} in {param.get('in', '?')}"
            + (" (required)" if param.get("required") else "")
            + (f" — {param['description']}" if param.get("description") else ""))

    body = operation.get("requestBody")
    if isinstance(body, dict):
        media_types = list((body.get("content") or {}).keys())
        lines.append("requestBody: " + (", ".join(media_types) or "declared")
                     + (" (required)" if body.get("required") else ""))

    responses = operation.get("responses")
    if isinstance(responses, dict):
        for code in sorted(responses, key=str):
            entry = responses[code]
            description = (entry.get("description")
                           if isinstance(entry, dict) else "")
            lines.append(f"response {code}: {description or ''}".rstrip())

    operation_key = f"op-{method}-{slug(path) or 'root'}"
    block = builder.add(
        DocumentBlockType.API_OPERATION, "\n".join(lines), key=operation_key,
        locator=_locator(key, pointer), content_mode=ContentMode.DERIVED,
        metadata={"method": upper, "path": path,
                  "operationId": str(operation.get("operationId") or ""),
                  "parameters": len(parameters)})
    parent = block.block_key if block is not None else builder.current_parent

    for index, param in enumerate(parameters):
        name = str(param.get("name") or f"p{index + 1}")
        detail = [f"{name} in {param.get('in', '?')}"]
        if param.get("required"):
            detail.append("required")
        if param.get("description"):
            detail.append(str(param["description"]))
        schema = param.get("schema") or {k: v for k, v in param.items()
                                         if k in ("type", "format", "enum")}
        if schema:
            detail.append(_text_of(schema))
        builder.add(
            DocumentBlockType.SCHEMA_DEFINITION, " — ".join(detail),
            key=f"{operation_key}-param-{slug(name) or index}",
            locator=_locator(key, f"{pointer}/parameters/{index}"),
            content_mode=ContentMode.DERIVED, parent_key=parent,
            metadata={"parameter": name, "in": str(param.get("in") or "")})


def _collect_parameters(operation: Dict[str, Any],
                        path_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Operation parameters PLUS the path item's shared ones, which OpenAPI says
    apply to every operation under that path. Skipping them would under-report
    what the endpoint accepts."""
    out: List[Dict[str, Any]] = []
    for source in (path_item.get("parameters"), operation.get("parameters")):
        if isinstance(source, list):
            out.extend(p for p in source if isinstance(p, dict))
    return out


def _emit_components(builder: DocumentBuilder, key: str, spec: Dict[str, Any],
                     doc: ParsedDocument) -> None:
    """Component schemas (OpenAPI 3) or definitions (Swagger 2)."""
    containers = (
        ("/components/schemas", (spec.get("components") or {}).get("schemas")
         if isinstance(spec.get("components"), dict) else None),
        ("/definitions", spec.get("definitions")),
    )
    count = 0
    for pointer_base, schemas in containers:
        if not isinstance(schemas, dict):
            continue
        for name in sorted(schemas):
            if builder.full:
                break
            schema = schemas[name]
            count += 1
            builder.add(
                DocumentBlockType.SCHEMA_DEFINITION,
                _describe_schema(name, schema),
                key=f"schema-{slug(name) or count}",
                locator=_locator(key, f"{pointer_base}/{_escape_pointer(name)}"),
                content_mode=ContentMode.DERIVED,
                metadata={"schema": name})
    doc.coverage["schemas"] = count


def _describe_schema(name: str, schema: Any) -> str:
    """A compact, deterministic rendering of a schema's declared shape.

    DERIVED by construction: it is a summary of the declaration, not the
    document's literal text, and it is labelled as such wherever it is stored.
    """
    if not isinstance(schema, dict):
        return f"{name}: {_text_of(schema)}"
    lines = [f"schema {name}"]
    if schema.get("type"):
        lines.append(f"type: {schema['type']}")
    if schema.get("description"):
        lines.append(f"description: {_text_of(schema['description'])}")
    required = schema.get("required")
    if isinstance(required, list) and required:
        lines.append("required: " + ", ".join(str(r) for r in required))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop in sorted(properties):
            spec = properties[prop]
            kind = (spec.get("type") or spec.get("$ref") or "?") \
                if isinstance(spec, dict) else "?"
            lines.append(f"property {prop}: {kind}")
    return "\n".join(lines)


def _note_external_refs(spec: Any, doc: ParsedDocument) -> None:
    """Count ``$ref`` targets that leave this document. They are NEVER fetched;
    counting them is how their absence stays visible."""
    external = 0
    stack: List[Any] = [spec]
    seen = 0
    while stack and seen < 200_000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                external += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if external:
        doc.note_unsupported(
            "external-reference", external,
            "external $ref targets are never fetched; only internal references "
            "are resolved")


__all__ = ["OpenApiParser", "CLAIMS"]
