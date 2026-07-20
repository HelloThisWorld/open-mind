"""Document parser SPI: probing, deterministic selection, honest failure modes.

Proves the selection contract that everything above it depends on:
  * a parser is NEVER chosen from the extension alone,
  * exactly one parser is selected, in a deterministic order,
  * an ambiguous selection FAILS loudly instead of picking arbitrarily,
  * a missing dependency reports `dependency_unavailable` for the RIGHT format,
  * an unsupported format reports `unsupported`, never an empty success,
  * a parser that raises fails ITSELF, never the caller,
  * importing the package pulls in no document dependency.
"""
import os
import sys
import tempfile

os.environ.setdefault("OPENMIND_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from openmind.documents import (models, parse_bytes, probe_bytes,  # noqa: E402
                                registry)
from openmind.documents.models import (DocumentLimits,  # noqa: E402
                                       DocumentParseContext, ParsedDocument)

_results = []
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"


def check(desc, cond):
    _results.append((desc, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + desc)


def ctx(logical_key, filename=""):
    return DocumentParseContext(logical_key=logical_key,
                                filename=filename or logical_key,
                                limits=DocumentLimits.from_config())


# ---------------------------------------------------------------------------
# 1. Probing reports FACTS, not guesses
# ---------------------------------------------------------------------------
md = probe_bytes(b"# Title\n\nbody\n", filename="spec.md")
check("probe records the filename", md.filename == "spec.md")
check("probe records the extension", md.extension == ".md")
check("probe records the size", md.size == len(b"# Title\n\nbody\n"))
check("probe detects valid UTF-8", md.magic.get("utf8") is True)
check("probe does not flag text as binary", md.magic.get("binary") is not True)

pdf_bytes = (FIXTURES / "sample-design.pdf").read_bytes()
pdf = probe_bytes(pdf_bytes, filename="whatever.txt")
check("probe detects a PDF by signature, not by extension",
      pdf.magic.get("pdf") is True and pdf.detected_media_type == "application/pdf")

docx_bytes = (FIXTURES / "sample-requirements.docx").read_bytes()
docx = probe_bytes(docx_bytes, filename="notes.bin")
check("probe detects a ZIP container", docx.is_zip_package is True)
check("probe reads the OOXML package structure",
      docx.has_zip_member("word/document.xml") is True)
check("probe resolves DOCX from package structure",
      "wordprocessingml" in docx.detected_media_type)

xlsx = probe_bytes((FIXTURES / "sample-tests.xlsx").read_bytes(), filename="x")
check("probe resolves XLSX from package structure",
      xlsx.has_zip_member("xl/workbook.xml") is True
      and "spreadsheetml" in xlsx.detected_media_type)

nul = probe_bytes(b"\x00\x01\x02\x03" * 64, filename="thing.txt")
check("probe flags NUL-bearing bytes as binary", nul.magic.get("binary") is True)

# A ZIP that is NOT an OOXML package must not be claimed as one.
import io  # noqa: E402
import zipfile  # noqa: E402

plain_zip = io.BytesIO()
with zipfile.ZipFile(plain_zip, "w") as zf:
    zf.writestr("readme.txt", "not an office document")
plain = probe_bytes(plain_zip.getvalue(), filename="report.docx")
check("a plain ZIP renamed .docx is NOT detected as DOCX",
      plain.has_zip_member("word/document.xml") is False
      and plain.detected_media_type == "application/zip")

# ---------------------------------------------------------------------------
# 2. Deterministic registry order and single selection
# ---------------------------------------------------------------------------
registry.reset()
order = [e.name for e in registry.list_parsers()]
check("list_parsers returns a deterministic order",
      order == [e.name for e in registry.list_parsers()])
check("the order is by ascending priority then name",
      order == [e.name for e in sorted(registry.list_parsers(),
                                       key=lambda e: (e.priority, e.name))])
check("every built-in parser is registered",
      set(order) >= {"pdf", "docx", "xlsx", "openapi", "json-schema", "sql",
                     "csv", "html", "markdown", "text"})
check("the generic text parser is LAST, so it can never steal a structured "
      "format", order[-1] == "text")

selected = registry.select(probe_bytes(pdf_bytes, filename="x.pdf"))
check("PDF bytes select exactly the pdf parser", getattr(selected, "name", "") == "pdf")
check("a renamed PDF still selects the pdf parser",
      getattr(registry.select(probe_bytes(pdf_bytes, filename="a.txt")), "name", "")
      == "pdf")
check("a plain ZIP named .docx does NOT select the docx parser",
      getattr(registry.select(probe_bytes(plain_zip.getvalue(),
                                          filename="r.docx")), "name", "") != "docx")

# ---------------------------------------------------------------------------
# 3. Ambiguity FAILS loudly
# ---------------------------------------------------------------------------
class _Greedy:
    def __init__(self, name):
        self.name = name
        self.version = "1.0"

    def supports(self, probe):
        return probe.extension == ".ambiguous"

    def parse(self, content, context):
        return ParsedDocument(parser_name=self.name, parser_version="1.0")


import openmind.documents.registry as _reg  # noqa: E402

_reg._entries["greedy-a"] = registry.ParserEntry("greedy-a", 5, __name__, "_A")
_reg._entries["greedy-b"] = registry.ParserEntry("greedy-b", 5, __name__, "_B")
_A = lambda: _Greedy("greedy-a")   # noqa: E731 - loaded by module attribute
_B = lambda: _Greedy("greedy-b")   # noqa: E731

ambiguous = probe_bytes(b"hello", filename="thing.ambiguous")
raised = None
try:
    registry.select(ambiguous)
except registry.AmbiguousParser as exc:
    raised = exc
check("two parsers at the SAME priority raise AmbiguousParser", raised is not None)
check("the ambiguity error names both candidates",
      bool(raised) and set(raised.names) == {"greedy-a", "greedy-b"})

result = registry.parse(b"hello", ctx("documents/thing.ambiguous",
                                      "thing.ambiguous"), probe=ambiguous)
check("an ambiguous parse returns `unsupported`, not a crash",
      result.status == models.DocumentParseStatus.UNSUPPORTED)
check("the ambiguous result records why",
      any(w.code == "ambiguous-parser" for w in result.warnings))

# A distinct priority resolves it deterministically.
_reg._entries["greedy-b"] = registry.ParserEntry("greedy-b", 6, __name__, "_B")
check("giving one a distinct priority resolves the ambiguity",
      getattr(registry.select(ambiguous), "name", "") == "greedy-a")
registry.reset()

# ---------------------------------------------------------------------------
# 4. Honest non-usable results
# ---------------------------------------------------------------------------
unsupported = parse_bytes(b"\x00\x01\x02\x03" * 512,
                          logical_key="documents/blob.bin", filename="blob.bin")
check("binary bytes nothing claims are `unsupported`",
      unsupported.status == models.DocumentParseStatus.UNSUPPORTED)
check("an unsupported result is NOT an empty successful parse",
      unsupported.status != models.DocumentParseStatus.PARSED
      and unsupported.blocks == [])
check("an unsupported result carries a reason", bool(unsupported.reason))

missing = models.dependency_unavailable("x.docx", "docx", "python-docx")
check("dependency_unavailable reports `unsupported` status",
      missing.status == models.DocumentParseStatus.UNSUPPORTED)
check("dependency_unavailable names the missing distribution",
      missing.reason == "dependency_unavailable"
      and any("python-docx" in (w.detail or {}).get("distribution", "")
              for w in missing.warnings))


class _Exploding:
    name = "exploding"
    version = "1.0"

    def supports(self, probe):
        return probe.extension == ".boom"

    def parse(self, content, context):
        raise RuntimeError("parser blew up")


_reg._entries["exploding"] = registry.ParserEntry("exploding", 1, __name__, "_E")
_E = lambda: _Exploding()   # noqa: E731

boom = parse_bytes(b"data", logical_key="documents/x.boom", filename="x.boom")
check("a raising parser fails ITSELF, not the caller",
      boom.status == models.DocumentParseStatus.FAILED)
check("the failure names the exception type", boom.reason == "RuntimeError")
check("the failure keeps the message",
      any("parser blew up" in w.message for w in boom.warnings))


class _WrongType:
    name = "wrongtype"
    version = "1.0"

    def supports(self, probe):
        return probe.extension == ".wrong"

    def parse(self, content, context):
        return {"not": "a ParsedDocument"}


_reg._entries["wrongtype"] = registry.ParserEntry("wrongtype", 1, __name__, "_W")
_W = lambda: _WrongType()   # noqa: E731
wrong = parse_bytes(b"data", logical_key="documents/x.wrong", filename="x.wrong")
check("a parser returning the wrong type is a failure, not a crash",
      wrong.status == models.DocumentParseStatus.FAILED)
registry.reset()

# ---------------------------------------------------------------------------
# 5. A supports() that raises must not break selection for other formats
# ---------------------------------------------------------------------------
class _BadSupports:
    name = "badsupports"
    version = "1.0"

    def supports(self, probe):
        raise ValueError("supports exploded")

    def parse(self, content, context):
        return ParsedDocument(parser_name=self.name, parser_version="1.0")


_reg._entries["badsupports"] = registry.ParserEntry("badsupports", 1, __name__,
                                                    "_BS")
_BS = lambda: _BadSupports()   # noqa: E731
check("a parser whose supports() raises simply does not claim the probe",
      getattr(registry.select(probe_bytes(b"# t\n", filename="a.md")), "name", "")
      == "markdown")
registry.reset()

# ---------------------------------------------------------------------------
# 6. Size limit is refusal, and it is explicit
# ---------------------------------------------------------------------------
tiny = DocumentParseContext(logical_key="documents/big.md", filename="big.md",
                            limits=DocumentLimits(max_bytes=32))
big = registry.parse(b"# heading\n" + b"x" * 500, tiny)
check("a document over DOCUMENT_MAX_BYTES is refused, not truncated",
      big.status == models.DocumentParseStatus.UNSUPPORTED)
check("the refusal names the limit it exceeded",
      any(w.detail.get("limit") == "DOCUMENT_MAX_BYTES" for w in big.warnings))

# ---------------------------------------------------------------------------
# 7. Importing the package pulls in NO document dependency
# ---------------------------------------------------------------------------
probe_src = (
    "import sys; import openmind.documents as d; "
    "d.probe_bytes(b'# hi', filename='a.md'); "
    "print(','.join(sorted(m for m in sys.modules "
    "if m in ('docx','pypdf','openpyxl','defusedxml'))))"
)
proc = subprocess.run([sys.executable, "-c", probe_src], capture_output=True,
                      text=True, cwd=str(Path(__file__).resolve().parent.parent))
check("importing openmind.documents imports no parser dependency",
      proc.returncode == 0 and proc.stdout.strip() == "")

# ---------------------------------------------------------------------------
# 8. finalize() normalizes what every parser would otherwise have to remember
# ---------------------------------------------------------------------------
parsed = parse_bytes((FIXTURES / "sample-requirements.md").read_bytes(),
                     logical_key="documents/req.md", filename="req.md")
check("ordinals are dense and in emission order",
      [b.ordinal for b in parsed.blocks] == list(range(len(parsed.blocks))))
check("coverage is computed", parsed.coverage.get("blocks") == len(parsed.blocks))
check("block keys are unique within a document",
      len({b.block_key for b in parsed.blocks}) == len(parsed.blocks))
check("structure_hash is stable across identical parses",
      parsed.structure_hash() == parse_bytes(
          (FIXTURES / "sample-requirements.md").read_bytes(),
          logical_key="documents/req.md", filename="req.md").structure_hash())
check("structure_hash changes when the content changes",
      parsed.structure_hash() != parse_bytes(
          b"# Different\n\nother\n", logical_key="documents/req.md",
          filename="req.md").structure_hash())

# ---------------------------------------------------------------------------
bad = [d for d, ok in _results if not ok]
print(f"\n{len(_results) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
