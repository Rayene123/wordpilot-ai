"""Text replacement that edits only Word text nodes in safe run sequences."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.table import _Cell
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn
from docx.oxml.text.run import CT_R


@dataclass(frozen=True)
class ReplacementResult:
    replacements: int
    warnings: tuple[str, ...] = ()
    output_path: str | None = None
    operation_type: str = "replace"
    success: bool = True
    affected_elements: int = 0
    error: str | None = None


def _iter_paragraphs(parent: DocumentObject | _Cell):
    """Yield searchable paragraphs from the body and every nested cell."""
    for item in parent.iter_inner_content():
        if isinstance(item, Paragraph):
            yield item
        else:
            for row in item.rows:
                for cell in row.cells:
                    yield from _iter_paragraphs(cell)


def _safe_run(run: CT_R) -> bool:
    """Return whether a run contains only formatting properties and text nodes."""
    # Keep tabs, breaks, fields, drawings, and other run-level structures as
    # boundaries. Editing only w:t nodes leaves all such XML untouched.
    return all(child.tag in (qn("w:rPr"), qn("w:t")) for child in run)


def _segments(paragraph: Paragraph):
    """Split a paragraph into adjacent safe runs, stopping at non-text XML."""
    current = []
    for run in paragraph._p.r_lst:
        if not _safe_run(run):
            if current:
                yield current
                current = []
            continue
        current.append(run)
    if current:
        yield current


def _replace_at(runs: list[CT_R], target: str, replacement: str, start: int) -> None:
    """Replace one known match by editing text nodes while retaining run XML."""
    nodes = [node for run in runs for node in run.findall(qn("w:t"))]
    texts = [node.text or "" for node in nodes]
    end = start + len(target)
    spans = []
    cursor = 0
    for text in texts:
        spans.append((cursor, cursor + len(text)))
        cursor += len(text)
    touched = [i for i, (a, b) in enumerate(spans) if a < end and b > start]
    first = touched[0]
    for i in touched:
        a, b = spans[i]
        left = texts[i][:max(0, start - a)] if i == first else ""
        right = texts[i][max(0, end - a):] if i == touched[-1] else ""
        texts[i] = left + (replacement if i == first else "") + right
    for node, text in zip(nodes, texts):
        node.text = text
        if text.startswith(" ") or text.endswith(" "):
            node.set(qn("xml:space"), "preserve")


def _replace_all(runs: list[CT_R], target: str, replacement: str) -> int:
    """Replace original, non-overlapping matches from right to left."""
    nodes = [node for run in runs for node in run.findall(qn("w:t"))]
    original = "".join(node.text or "" for node in nodes)
    starts = []
    cursor = 0
    while True:
        position = original.find(target, cursor)
        if position < 0:
            break
        starts.append(position)
        cursor = position + len(target)
    for position in reversed(starts):
        _replace_at(runs, target, replacement, position)
    return len(starts)


def replace_text(
    document_path: str | Path,
    target_text: str,
    replacement_text: str,
    output_path: str | Path | None = None,
) -> ReplacementResult:
    """Replace text in paragraphs and table cells, saving to ``output_path``.

    Replacement text inherits the first matched run's formatting. Source is
    left untouched when output_path differs; when omitted, it is updated only
    if a match exists. Empty targets are rejected to avoid unbounded inserts.
    """
    if not target_text:
        raise ValueError("target_text must not be empty")
    source = Path(document_path)
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_file():
        raise IsADirectoryError(source)
    destination = Path(output_path) if output_path is not None else source
    try:
        document = Document(str(source))
    except Exception as exc:
        raise ValueError(f"Could not open DOCX document {source}: {exc}") from exc

    count = 0
    warnings: list[str] = []
    encountered_boundary = False
    for paragraph in _iter_paragraphs(document):
        if any(not _safe_run(run) for run in paragraph._p.r_lst):
            encountered_boundary = True
        for segment in _segments(paragraph):
            count += _replace_all(segment, target_text, replacement_text)
    if encountered_boundary:
        warnings.append(
            "Paragraphs containing drawings, fields, breaks, or other non-text run elements were treated as replacement boundaries."
        )
    if count:
        try:
            document.save(str(destination))
        except Exception as exc:
            raise OSError(f"Could not save DOCX document {destination}: {exc}") from exc
    else:
        warnings.append("Target text was not found in a safe text-only run sequence; document was not modified.")
    error = None if count else "Target text was not found in a safe text-only run sequence."
    return ReplacementResult(
        count,
        tuple(warnings),
        str(destination) if count else None,
        operation_type="replace",
        success=count > 0,
        affected_elements=count,
        error=error,
    )
