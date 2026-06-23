"""tree-sitter-java parsing helpers (with graceful no-parse behavior).

Used by topology extraction, schema/service extraction, and RAG chunking.
If tree-sitter is unavailable the callers fall back to regex (see topology.py).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterator, List, Optional

_parser = None
_lang = None
_lock = threading.Lock()


def available() -> bool:
    try:
        get_parser()
        return True
    except Exception:
        return False


def get_parser():
    global _parser, _lang
    if _parser is not None:
        return _parser
    with _lock:
        if _parser is not None:
            return _parser
        import tree_sitter_java
        from tree_sitter import Language, Parser
        _lang = Language(tree_sitter_java.language())
        try:
            _parser = Parser(_lang)
        except TypeError:  # older API
            _parser = Parser()
            _parser.set_language(_lang)
        return _parser


def parse(src: str):
    parser = get_parser()
    return parser.parse(bytes(src, "utf8"))


def text(node, src: bytes) -> str:
    try:
        return node.text.decode("utf-8", "replace")
    except Exception:
        return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def line(node) -> int:
    return node.start_point[0] + 1


def walk(node) -> Iterator[Any]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def find_nodes(root, types) -> Iterator[Any]:
    types = set(types) if not isinstance(types, set) else types
    for n in walk(root):
        if n.type in types:
            yield n


def get_package(root, src: bytes) -> str:
    for n in root.children:
        if n.type == "package_declaration":
            for c in n.children:
                if c.type in ("scoped_identifier", "identifier"):
                    return text(c, src)
    return ""


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------
ANNOTATION_TYPES = {"annotation", "marker_annotation"}


def annotation_name(node, src: bytes) -> str:
    name_node = node.child_by_field_name("name")
    raw = text(name_node, src) if name_node else ""
    return raw.split(".")[-1]


def annotations_of(node, src: bytes) -> List[Any]:
    """Annotations attached to a declaration (looks in modifiers + direct kids)."""
    out = []
    for c in node.children:
        if c.type == "modifiers":
            out.extend(a for a in c.children if a.type in ANNOTATION_TYPES)
        elif c.type in ANNOTATION_TYPES:
            out.append(c)
    return out


def annotation_string_args(node, src: bytes) -> List[str]:
    """All string-literal values anywhere inside an annotation's arguments."""
    vals = []
    args = node.child_by_field_name("arguments")
    if not args:
        return vals
    for n in walk(args):
        if n.type == "string_literal":
            vals.append(string_value(n, src))
    return vals


def annotation_named_value(node, src: bytes, key: str) -> Optional[str]:
    """Raw text of a named annotation argument, e.g. topics = "..."."""
    args = node.child_by_field_name("arguments")
    if not args:
        return None
    for n in args.children:
        if n.type == "element_value_pair":
            kids = n.children
            if kids and text(kids[0], src) == key:
                # value is after '='
                val = n.child_by_field_name("value")
                if val is None and len(kids) >= 3:
                    val = kids[2]
                return text(val, src) if val else None
    return None


def string_value(node, src: bytes) -> str:
    raw = text(node, src).strip()
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] in "\"'":
        return raw[1:-1]
    return raw


# ---------------------------------------------------------------------------
# Type declarations (class/interface/record/enum)
# ---------------------------------------------------------------------------
TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
    "annotation_type_declaration": "annotation",
}


def iter_types(root, src: bytes) -> Iterator[Dict[str, Any]]:
    for n in walk(root):
        if n.type in TYPE_DECLS:
            name_node = n.child_by_field_name("name")
            yield {
                "node": n,
                "kind": TYPE_DECLS[n.type],
                "name": text(name_node, src) if name_node else "",
                "start_line": line(n),
                "end_line": n.end_point[0] + 1,
            }


def class_body(node):
    return node.child_by_field_name("body")


def iter_methods(type_node, src: bytes) -> Iterator[Dict[str, Any]]:
    body = class_body(type_node)
    if body is None:
        return
    for c in body.children:
        if c.type in ("method_declaration", "constructor_declaration"):
            name_node = c.child_by_field_name("name")
            params = c.child_by_field_name("parameters")
            ret = c.child_by_field_name("type")
            sig_parts = []
            if ret is not None:
                sig_parts.append(text(ret, src))
            sig_parts.append(text(name_node, src) if name_node else "")
            sig_parts.append(text(params, src) if params else "()")
            yield {
                "node": c,
                "name": text(name_node, src) if name_node else "",
                "signature": " ".join(sig_parts),
                "start_line": line(c),
                "end_line": c.end_point[0] + 1,
            }


def iter_fields(type_node, src: bytes) -> Iterator[Dict[str, Any]]:
    body = class_body(type_node)
    if body is None:
        return
    for c in body.children:
        if c.type == "field_declaration":
            ftype = c.child_by_field_name("type")
            declarator = None
            for k in c.children:
                if k.type == "variable_declarator":
                    declarator = k
                    break
            name = ""
            if declarator is not None:
                nn = declarator.child_by_field_name("name")
                name = text(nn, src) if nn else ""
            yield {"name": name, "type": text(ftype, src) if ftype else "", "line": line(c)}


def record_components(type_node, src: bytes) -> List[Dict[str, str]]:
    params = type_node.child_by_field_name("parameters")
    out = []
    if params is None:
        return out
    for c in params.children:
        if c.type == "formal_parameter":
            t = c.child_by_field_name("type")
            n = c.child_by_field_name("name")
            out.append({"name": text(n, src) if n else "",
                        "type": text(t, src) if t else ""})
    return out


# ---------------------------------------------------------------------------
# Method invocations (e.g. KafkaTemplate.send("topic", ...))
# ---------------------------------------------------------------------------
def iter_method_invocations(root, src: bytes) -> Iterator[Dict[str, Any]]:
    for n in walk(root):
        if n.type == "method_invocation":
            name_node = n.child_by_field_name("name")
            obj_node = n.child_by_field_name("object")
            args_node = n.child_by_field_name("arguments")
            string_args = []
            arg_idents = []
            if args_node is not None:
                for a in args_node.children:
                    if a.type == "string_literal":
                        string_args.append(string_value(a, src))
                    elif a.type in ("identifier", "field_access", "scoped_identifier"):
                        arg_idents.append(text(a, src))
            yield {
                "name": text(name_node, src) if name_node else "",
                "object": text(obj_node, src) if obj_node else "",
                "string_args": string_args,
                "arg_idents": arg_idents,
                "line": line(n),
            }


def iter_static_string_constants(root, src: bytes) -> Dict[str, str]:
    """Resolve `static final String FOO = "bar"` -> {FOO: bar, Class.FOO: bar}."""
    out: Dict[str, str] = {}
    for t in iter_types(root, src):
        cls = t["name"]
        body = class_body(t["node"])
        if body is None:
            continue
        for c in body.children:
            if c.type != "field_declaration":
                continue
            txt = text(c, src)
            if "String" not in txt:
                continue
            for k in c.children:
                if k.type == "variable_declarator":
                    nn = k.child_by_field_name("name")
                    val = k.child_by_field_name("value")
                    if nn is not None and val is not None and val.type == "string_literal":
                        name = text(nn, src)
                        sval = string_value(val, src)
                        out[name] = sval
                        out[f"{cls}.{name}"] = sval
    return out
