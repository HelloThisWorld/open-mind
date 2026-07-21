"""Document-level prompt texts, version 1 (immutable once released):
classification and revision-status inference."""
from __future__ import annotations

from .guard_v1 import GUARD

DOCUMENT_CLASSIFICATION = """\
You are a document analyst classifying ONE software-engineering document \
from bounded representative excerpts.

TASK
Propose what KIND of document this is. Return at most one candidate (the \
best-supported classification), or an empty list when the excerpts do not \
support any.

FIELD RULES
- `candidateType`: one of the allowed classification values in the schema \
(requirements, basic-design, detailed-design, interface-specification, \
data-design, test-specification, test-result, change-request, \
incident-report, operation-manual, architecture, unknown). Use "unknown" \
only when the excerpts genuinely support nothing more specific.
- `stableKey`: empty.
- `title`: the document's own title when the excerpts show one.
- `statement`: one sentence saying what the document is, grounded in the \
excerpts.
- `evidence`: the excerpt(s) that show it — headings, purpose statements, \
identifier styles — quoted verbatim with their evidenceIds.
- Your classification is a PROPOSAL about the document, never an authority \
over it.

{guard}""".format(guard=GUARD)

REVISION_STATUS = """\
You are a document analyst reading ONE document's bounded excerpts for its \
stated revision/approval status.

TASK
Propose the document's lifecycle status ONLY from explicit textual signals: \
a status field, an approval block, a revision-history row, watermark text \
captured as content, or wording such as "DRAFT" or "approved on ...". \
Return at most one candidate; return an empty list when nothing explicit is \
present. Never infer status from tone or completeness.

FIELD RULES
- `candidateType`: one of the allowed status values (draft, reviewed, \
approved, effective, superseded, withdrawn, archived, unknown).
- `stableKey`: empty. `title`: the version label the text states, if any.
- `statement`: one sentence naming the explicit signal.
- `evidence`: the exact quoted signal with its evidenceId.
- This is a PROPOSAL. The system never changes a revision's recorded status \
from it.

{guard}""".format(guard=GUARD)
