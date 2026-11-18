"""Controlled paragraph insertion and removal for DOCX documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from .package_preservation import save_preserving_package_parts


@dataclass(frozen=True)
class OperationResult:
    """Common outcome returned by insertion and removal operations."""

    operation_type: str
    success: bool
    affected_elements: int
    warnings: tuple[str, ...] = ()
    error: str | None = None
    output_path: str | None = None


def _paragraphs(parent: DocumentObject | _Cell) -> Iterator[Paragraph]:
    """Yield paragraphs in a document or cell, recursively including tables."""
    for item in parent.iter_inner_content():
        if isinstance(item, Paragraph):
            yield item
        else:
            for row in item.rows:
                for cell in row.cells:
                    yield from _paragraphs(cell)


def _tables(parent: DocumentObject | _Cell) -> Iterator[Table]:
    """Yield a document's tables in order, including tables nested in cells."""
    for item in parent.iter_inner_content():
        if isinstance(item, Table):
            yield item
            for row in item.rows:
                for cell in row.cells:
                    yield from _tables(cell)


def _load(path: str | Path) -> tuple[DocumentObject | None, Path, str | None]:
    """Open a DOCX and return its document, source path, or a readable error."""
    source = Path(path)
    if not source.exists():
        return None, source, f"Document does not exist: {source}"
    if not source.is_file():
        return None, source, f"Document path is not a file: {source}"
    try:
        return Document(str(source)), source, None
    except Exception as exc:
        return None, source, f"Could not open DOCX document {source}: {exc}"


def _save(document: DocumentObject, source: Path, destination: Path, operation: str, count: int) -> OperationResult:
    """Save an edited document and describe success or the save failure."""
    try:
        save_preserving_package_parts(document, source, destination)
    except Exception as exc:
        return OperationResult(operation, False, 0, error=f"Could not save DOCX document {destination}: {exc}")
    return OperationResult(operation, True, count, output_path=str(destination))


def insert_paragraph(
    document_path: str | Path,
    target_text: str,
    text: str,
    position: str = "after",
    style: str | None = None,
    output_path: str | Path | None = None,
    location: tuple[int, ...] | None = None,
) -> OperationResult:
    """Insert a paragraph before or after one uniquely matching paragraph.

    Matching searches body and table-cell paragraphs, including nested tables.
    The original paragraph is never rebuilt or changed. A duplicate target is
    rejected rather than choosing a location silently. Supply ``location`` to
    choose a specific match using the same tuple format as remove_paragraph.
    """
    operation = "insert_paragraph"
    if position not in ("before", "after"):
        return OperationResult(operation, False, 0, error="position must be 'before' or 'after'.")
    if not target_text:
        return OperationResult(operation, False, 0, error="target_text must not be empty.")
    document, source, error = _load(document_path)
    if error:
        return OperationResult(operation, False, 0, error=error)
    if location is not None:
        target = _location_paragraph(document, location)
        if target is None or target.text != target_text:
            return OperationResult(operation, False, 0, error="No paragraph with the requested text exists at that location.")
    else:
        matches = [p for p in _paragraphs(document) if p.text == target_text]
        if not matches:
            return OperationResult(operation, False, 0, error="Target paragraph was not found.")
        if len(matches) > 1:
            return OperationResult(operation, False, 0, error="Target paragraph is ambiguous; it occurs more than once.")
        target = matches[0]
    element = OxmlElement("w:p")
    inserted = Paragraph(element, target._parent)
    try:
        inserted.style = style if style is not None else target.style
    except KeyError:
        return OperationResult(operation, False, 0, error=f"Unknown paragraph style: {style}")
    inserted.add_run(text)
    if position == "before":
        target._p.addprevious(element)
    else:
        target._p.addnext(element)
    destination = Path(output_path) if output_path is not None else source
    return _save(document, source, destination, operation, 1)


def _location_paragraph(document: DocumentObject, location: tuple[int, ...]) -> Paragraph | None:
    """Resolve a body or table-cell index tuple to its paragraph, if present."""
    if len(location) == 1:
        paragraphs = document.paragraphs
        index = location[0]
        return paragraphs[index] if 0 <= index < len(paragraphs) else None
    if len(location) == 4:
        table_index, row_index, cell_index, paragraph_index = location
        tables = list(_tables(document))
        if not 0 <= table_index < len(tables):
            return None
        table = tables[table_index]
        if not 0 <= row_index < len(table.rows):
            return None
        row = table.rows[row_index]
        if not 0 <= cell_index < len(row.cells):
            return None
        paragraphs = row.cells[cell_index].paragraphs
        return paragraphs[paragraph_index] if 0 <= paragraph_index < len(paragraphs) else None
    return None


def _safe_to_remove(paragraph: Paragraph) -> str | None:
    """Explain why deletion is unsafe, or return None when the paragraph is safe."""
    style_name = paragraph.style.name.lower() if paragraph.style is not None else ""
    if style_name.startswith("heading") or style_name in {"title", "subtitle"}:
        return "Refusing to remove a heading, title, or subtitle paragraph."
    for child in paragraph._p:
        if child.tag == qn("w:pPr"):
            continue
        if child.tag != qn("w:r"):
            return "Refusing to remove a paragraph containing non-text XML content."
        if any(part.tag not in (qn("w:rPr"), qn("w:t")) for part in child):
            return "Refusing to remove a paragraph containing images, breaks, fields, or other non-text run content."
    parent_paragraphs = getattr(paragraph._parent, "paragraphs", ())
    if len(parent_paragraphs) <= 1:
        return "Refusing to remove the only paragraph in its document location."
    return None


def remove_paragraph(
    document_path: str | Path,
    target_text: str,
    output_path: str | Path | None = None,
    location: tuple[int, ...] | None = None,
) -> OperationResult:
    """Remove one exact-text paragraph, refusing ambiguous or unsafe matches.

    ``location=(paragraph_index,)`` selects a body paragraph. A table-cell
    paragraph is selected with ``(table_index, row_index, cell_index,
    paragraph_index)``; table indexes include nested tables in document order.
    The selected paragraph must still match ``target_text`` exactly.
    """
    operation = "remove_paragraph"
    if not target_text:
        return OperationResult(operation, False, 0, error="target_text must not be empty.")
    document, source, error = _load(document_path)
    if error:
        return OperationResult(operation, False, 0, error=error)
    if location is not None:
        target = _location_paragraph(document, location)
        if target is None or target.text != target_text:
            return OperationResult(operation, False, 0, error="No paragraph with the requested text exists at that location.")
    else:
        matches = [p for p in _paragraphs(document) if p.text == target_text]
        if not matches:
            return OperationResult(operation, False, 0, error="Target paragraph was not found.")
        if len(matches) > 1:
            return OperationResult(operation, False, 0, error="Target paragraph is ambiguous; specify its location.")
        target = matches[0]

    unsafe_reason = _safe_to_remove(target)
    if unsafe_reason:
        return OperationResult(operation, False, 0, error=unsafe_reason)
    target._p.getparent().remove(target._p)
    destination = Path(output_path) if output_path is not None else source
    return _save(document, source, destination, operation, 1)
