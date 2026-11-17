"""Read-only inspection of DOCX paragraph, run, table, and image content."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from docx import Document
from docx.document import Document as DocumentObject
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn


@dataclass(frozen=True)
class RunInfo:
    text: str
    bold: bool | None
    italic: bool | None
    underline: bool | None
    font_name: str | None
    font_size_pt: float | None


@dataclass(frozen=True)
class ParagraphInfo:
    text: str
    style: str | None
    alignment: str | None
    runs: tuple[RunInfo, ...]


@dataclass(frozen=True)
class TableInfo:
    rows: tuple[tuple[tuple[str, ...], ...], ...]


@dataclass(frozen=True)
class DocumentSummary:
    paragraphs: tuple[ParagraphInfo, ...]
    tables: tuple[TableInfo, ...]
    paragraph_count: int
    table_count: int
    inline_image_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of this summary."""
        return asdict(self)


def _iter_blocks(parent: DocumentObject | _Cell) -> Iterator[Paragraph | Table]:
    """Yield paragraphs and tables in their document order."""
    yield from parent.iter_inner_content()


def _paragraphs(parent: DocumentObject | _Cell) -> Iterator[Paragraph]:
    """Yield all paragraphs under a document or cell, descending into tables."""
    for block in _iter_blocks(parent):
        if isinstance(block, Paragraph):
            yield block
        else:
            for row in block.rows:
                for cell in row.cells:
                    yield from _paragraphs(cell)


def _tables(parent: DocumentObject | _Cell) -> Iterator[Table]:
    """Yield each table under a document or cell, including nested tables."""
    for block in _iter_blocks(parent):
        if isinstance(block, Table):
            yield block
            for row in block.rows:
                for cell in row.cells:
                    yield from _tables(cell)


def _paragraph_info(paragraph: Paragraph) -> ParagraphInfo:
    """Read one paragraph's text, style, alignment, and run formatting."""
    runs = tuple(
        RunInfo(
            text=run.text,
            bold=run.bold,
            italic=run.italic,
            underline=run.underline,
            font_name=run.font.name,
            font_size_pt=run.font.size.pt if run.font.size is not None else None,
        )
        for run in paragraph.runs
    )
    return ParagraphInfo(
        text=paragraph.text,
        style=paragraph.style.name if paragraph.style is not None else None,
        alignment=str(paragraph.alignment) if paragraph.alignment is not None else None,
        runs=runs,
    )


def _planner_blocks(parent: DocumentObject | _Cell) -> list[dict[str, Any]]:
    """Return ordered paragraph/table blocks without duplicating run text.

    Paragraph strings appear once in this projection. Table rows and cells are
    retained as containers, so the planner can distinguish identical anchors
    by document order and location.
    """
    result: list[dict[str, Any]] = []
    for block in _iter_blocks(parent):
        if isinstance(block, Paragraph):
            # Sequence position plus enclosing table/row/cell list positions
            # retain ordering and provide an unambiguous structural anchor.
            result.append({"p": block.text})
            continue

        rows = [
            [_planner_blocks(cell) for cell in row.cells]
            for row in block.rows
        ]
        result.append({"t": rows})
    return result


def inspect_document_with_planner_context(
    path: str | Path,
) -> tuple[DocumentSummary, dict[str, Any]]:
    """Inspect once and return the unchanged full summary plus compact structure.

    The planner projection keeps document block order and table/cell nesting,
    while omitting run formatting and the duplicate table text representation.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_file():
        raise IsADirectoryError(source)
    try:
        document = Document(str(source))
    except Exception as exc:
        raise ValueError(f"Could not open DOCX document {source}: {exc}") from exc

    paragraphs = tuple(_paragraph_info(p) for p in _paragraphs(document))
    tables = tuple(
        TableInfo(rows=tuple(
            tuple(tuple(p.text for p in _paragraphs(cell)) for cell in row.cells)
            for row in table.rows
        ))
        for table in _tables(document)
    )
    summary = DocumentSummary(
        paragraphs=paragraphs,
        tables=tables,
        paragraph_count=len(paragraphs),
        table_count=len(tables),
        inline_image_count=len(document.inline_shapes),
    )
    return summary, {
        "format": "ordered blocks: p=paragraph; t=table(rows>cells>blocks)",
        "blocks": _planner_blocks(document),
    }


def inspect_document(path: str | Path) -> DocumentSummary:
    """Inspect an existing DOCX without writing to it.

    Raises ``FileNotFoundError``, ``IsADirectoryError`` or ``ValueError`` for
    invalid paths and documents that python-docx cannot open.
    """
    summary, _ = inspect_document_with_planner_context(path)
    return summary
