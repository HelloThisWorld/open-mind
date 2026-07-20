"""JSON Schema documents.

DETECTION
---------
A JSON Schema is recognized by ``$schema`` naming a json-schema.org meta-schema,
or by a root object carrying schema-defining keys (``$defs`` / ``definitions``
alongside ``type``/``properties``). The extension alone is never enough — most
JSON Schema files are just ``.json``, and most ``.json`` files are not schemas.

REMOTE REFERENCES ARE NEVER FETCHED
-----------------------------------
This is the single most important behaviour here. ``$ref`` values pointing at a
URL or another file are recorded as unsupported content and left unresolved. A
schema parser that dereferences remote refs turns "read this document" into
"make network requests to whatever this document names", which is both an egress
violation and a server-side request forgery primitive.

Internal ``#/...`` references ARE resolved within the same document, because that
needs no I/O and is what makes a modular schema readable.

CONSTRAINTS ARE COPIED, NOT INTERPRETED
---------------------------------------
``minLength``, ``pattern``, ``enum``, ``format`` and friends are recorded as
declared. Nothing is validated, and no business meaning is inferred from a
constraint.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ..domain.types import ContentMode, DocumentBlockType, DocumentParseStatus
from .builder import DocumentBuilder, slug
from .models import DocumentParseContext, DocumentProbe, ParsedDocument
from .security import decode_text

_EXTENSIONS = frozenset({".json", ".yaml", ".yml"})

#: Keywords copied verbatim into a definition's rendered constraints.
_CONSTRAINT_KEYS = (
    "type", "format", "pattern", "enum", "const", "default",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "additionalProperties",
)

_SCHEMA_KEYS = ("$defs", "definitions", "properties", "allOf", "anyOf", "oneOf")


def _escape_pointer(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def CLAIMS(probe: DocumentProbe) -> bool:      # noqa: N802 - registry protocol
    if probe.magic.get("binary") or probe.magic.get("zip") \
            or probe.magic.get("pdf"):
        return False
    if probe.extension not in _EXTENSIONS:
        return False
    head = probe.head.decode("utf-8", "replace")
    if "json-schema.org" in head:
        return True
    # A document declaring $schema plus at least one schema-shaping keyword.
    if '"$schema"' in head or "$schema:" in head:
        return any(f'"{k}"' in head or f"{k}:" in head for k in _SCHEMA_KEYS)
    return False


class JsonSchemaParser:
    """Root schema, ``$defs``/``definitions``, properties and constraints."""

    name = "json-schema"
    version = "1.0"

    def supports(self, probe: DocumentProbe) -> bool:
        return CLAIMS(probe)

    def parse(self, content: bytes,
              context: DocumentParseContext) -> ParsedDocument:
        text, encoding, lossy = decode_text(content)
        doc = ParsedDocument(parser_name=self.name, parser_version=self.version,
                             media_type="application/schema+json",
                             title=context.filename or context.logical_key)
        schema, error = _load(text)
        if error is not None or not isinstance(schema, dict):
            doc.status = DocumentParseStatus.UNSUPPORTED
            doc.reason = "not_json_schema"
            doc.add_warning(
                "not-json-schema",
                error or "the document is not a JSON/YAML object, so it is not "
                         "a JSON Schema")
            return doc
        if lossy:
            doc.add_warning("decode-fallback",
                            "the file is not valid UTF-8; it was decoded with "
                            "replacement characters", encoding=encoding)

        title = str(schema.get("title") or "").strip()
        if title:
            doc.title = title
        dialect = str(schema.get("$schema") or "")
        if dialect:
            doc.metadata.extra["dialect"] = dialect
        schema_id = str(schema.get("$id") or schema.get("id") or "")
        if schema_id:
            doc.metadata.extra["id"] = schema_id

        builder = DocumentBuilder(doc, max_blocks=context.limits.max_blocks,
                                  max_block_chars=context.limits.max_block_chars)
        key = context.logical_key
        builder.add_root(doc.title, _locator(key, ""))

        root = builder.add(
            DocumentBlockType.SCHEMA_DEFINITION,
            _render(doc.title or "root", schema, schema),
            key="schema-root", locator=_locator(key, ""),
            content_mode=ContentMode.DERIVED,
            metadata={"schema": doc.title or "root", "root": True})
        root_key = root.block_key if root is not None else builder.root_key

        definitions = 0
        for container in ("$defs", "definitions"):
            entries = schema.get(container)
            if not isinstance(entries, dict):
                continue
            for name in sorted(entries):
                if builder.full:
                    break
                definitions += 1
                _emit_definition(builder, key, schema, name, entries[name],
                                 f"/{container}/{_escape_pointer(name)}",
                                 root_key)

        properties = 0
        props = schema.get("properties")
        if isinstance(props, dict):
            for name in sorted(props):
                if builder.full:
                    break
                properties += 1
                _emit_definition(builder, key, schema, name, props[name],
                                 f"/properties/{_escape_pointer(name)}",
                                 root_key, kind="property")

        _note_external_refs(schema, doc)
        doc.coverage["definitions"] = definitions
        doc.coverage["properties"] = properties
        return doc


def _load(text: str) -> Tuple[Any, Optional[str]]:
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"the JSON document could not be parsed: {exc}"
    try:
        import yaml
    except ImportError:
        return None, "PyYAML is required to read a YAML JSON-Schema document"
    try:
        return yaml.safe_load(text), None       # safe_load only, never load
    except Exception as exc:
        return None, f"the YAML document could not be parsed: {exc}"


def _locator(document: str, pointer: str) -> Dict[str, Any]:
    return {"kind": "json-pointer", "document": document, "pointer": pointer or ""}


def _resolve_internal(root: Dict[str, Any], ref: str) -> Optional[Any]:
    """Resolve a ``#/a/b`` pointer INSIDE this document. Returns None for a
    remote or unresolvable reference — and never performs any I/O."""
    if not ref.startswith("#"):
        return None
    node: Any = root
    for raw in ref[1:].split("/"):
        if not raw:
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and \
                int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def _render(name: str, schema: Any, root: Dict[str, Any]) -> str:
    """A deterministic rendering of one schema node's declared shape."""
    if not isinstance(schema, dict):
        return f"{name}: {json.dumps(schema, sort_keys=True, ensure_ascii=False)}"
    lines = [f"schema {name}"]
    if schema.get("description"):
        lines.append(f"description: {schema['description']}")
    ref = schema.get("$ref")
    if isinstance(ref, str):
        lines.append(f"$ref: {ref}")
        target = _resolve_internal(root, ref)
        if isinstance(target, dict) and target.get("type"):
            # Internal refs only. A remote $ref stays unresolved, by design.
            lines.append(f"resolved type: {target['type']}")
    for keyword in _CONSTRAINT_KEYS:
        if keyword in schema:
            value = schema[keyword]
            lines.append(f"{keyword}: "
                         f"{json.dumps(value, sort_keys=True, ensure_ascii=False)}"
                         if not isinstance(value, (str, int, float, bool))
                         else f"{keyword}: {value}")
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
    items = schema.get("items")
    if isinstance(items, dict):
        lines.append(f"items: {items.get('type') or items.get('$ref') or '?'}")
    return "\n".join(lines)


def _emit_definition(builder: DocumentBuilder, key: str, root: Dict[str, Any],
                     name: str, schema: Any, pointer: str, parent_key: str,
                     kind: str = "definition") -> None:
    builder.add(
        DocumentBlockType.SCHEMA_DEFINITION, _render(name, schema, root),
        key=f"{kind}-{slug(name) or 'unnamed'}",
        locator=_locator(key, pointer), content_mode=ContentMode.DERIVED,
        parent_key=parent_key, metadata={"name": name, "kind": kind})


def _note_external_refs(schema: Any, doc: ParsedDocument) -> None:
    external = 0
    stack: List[Any] = [schema]
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
            "remote and cross-file $ref targets are NEVER fetched; only "
            "internal '#/...' references are resolved")


__all__ = ["JsonSchemaParser", "CLAIMS"]
