"""Read-only integrity verification for supported DOCX editing operations.

This verifier checks document-body paragraphs, tables/cells, inline image
references, and package preservation. It does not claim complete OOXML or
Microsoft Word feature compatibility.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from xml.etree import ElementTree

from lxml import etree
from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from backend.operation_schemas import DocumentOperation, InsertOperation, RemoveOperation, ReplaceOperation

_OPERATIONS = TypeAdapter(list[DocumentOperation])
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


class IntegrityCheck(BaseModel):
    """Outcome for one independently reported integrity check."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["passed", "failed", "partially_verified"]
    details: list[str]


class IntegrityReport(BaseModel):
    """Serializable report describing expected and unexpected DOCX differences."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "partially_verified"]
    checks: list[IntegrityCheck]
    unexpected_changes: list[str]
    expected_changes: list[str]
    unverified_features: list[str]
    errors: list[str]
    summary: str


@dataclass(frozen=True)
class _Paragraph:
    """Comparable paragraph properties collected without editing the document."""

    text: str
    replacement_segments: tuple[tuple[bool, str], ...]
    style: str | None
    alignment: str | None
    run_properties: tuple[tuple[bool | None, bool | None, bool | None, str | None, float | None], ...]
    other_run_properties: tuple[tuple[str, ...], ...]
    other_paragraph_properties: tuple[str, ...]
    image_hashes: tuple[str, ...]
    replacement_model_complete: bool


@dataclass
class _Snapshot:
    """Supported structural view and package hashes for one DOCX file."""

    regions: dict[tuple[Any, ...], list[_Paragraph]]
    block_orders: dict[tuple[Any, ...], list[tuple[str, Any]]]
    table_shapes: list[tuple[int, tuple[int, ...]]]
    table_properties: list[tuple[int, tuple[Any, ...]]]
    section_properties: str
    package_hashes: dict[str, str]
    unverified_features: list[str]


def verify_document_integrity(
    original_path: str | Path,
    edited_path: str | Path,
    approved_operations: list[Mapping[str, Any] | DocumentOperation],
) -> IntegrityReport:
    """Compare an original DOCX to edited output without modifying either file.

    Approved operations are validated with the existing discriminated Pydantic
    schemas and applied to an in-memory structural expectation. The actual edit
    must match that expected result while unrelated formatting, table shape,
    images, and package parts remain unchanged.
    """
    checks: list[IntegrityCheck] = []
    expected: list[str] = []
    unexpected: list[str] = []
    unverified: list[str] = []
    errors: list[str] = []
    try:
        operations = _OPERATIONS.validate_python(approved_operations)
    except (ValidationError, TypeError, ValueError) as exc:
        return _failure_report("approved_operations", f"Approved operations are invalid: {exc}")

    package_diffs: list[str] = []
    try:
        original_path = Path(original_path)
        edited_path = Path(edited_path)
        original_hashes, original_names = _package_hashes(original_path)
        edited_hashes, edited_names = _package_hashes(edited_path)
        package_diffs = _compare_packages(original_hashes, edited_hashes)
        original = _snapshot(original_path, original_hashes, original_names)
        edited = _snapshot(edited_path, edited_hashes, edited_names)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        report = _failure_report("document_readability", str(exc))
        _append_package_failures(report, package_diffs)
        return report
    except Exception as exc:
        # python-docx and ZIP errors vary by malformed package; always return a
        # structured failure rather than treating an incomplete read as valid.
        report = _failure_report("document_readability", f"Could not inspect DOCX package: {exc}")
        _append_package_failures(report, package_diffs)
        return report

    unverified.extend(original.unverified_features)
    unverified.extend(item for item in edited.unverified_features if item not in unverified)
    expected_regions, expected_block_orders, operation_errors, operation_changes = _apply_expected_operations(original, operations)
    expected.extend(operation_changes)
    if operation_errors:
        unexpected.extend(operation_errors)
    checks.append(_check("approved_operation_outcomes", operation_errors))

    text_diffs, formatting_diffs, image_diffs = _compare_regions(expected_regions, edited.regions)
    text_diffs.extend(_compare_block_orders(expected_block_orders, edited.block_orders))
    unexpected.extend(text_diffs)
    unexpected.extend(formatting_diffs)
    unexpected.extend(image_diffs)
    checks.append(_check("paragraph_text_and_order", text_diffs))
    checks.append(_check("paragraph_formatting", formatting_diffs))

    table_diffs = _compare_table_shapes(original.table_shapes, edited.table_shapes)
    unexpected.extend(table_diffs)
    checks.append(_check("table_count_and_dimensions", table_diffs))

    table_property_diffs = _compare_table_properties(original.table_properties, edited.table_properties)
    unexpected.extend(table_property_diffs)
    checks.append(_check("table_formatting", table_property_diffs))

    section_diffs = _compare_section_properties(original.section_properties, edited.section_properties)
    unexpected.extend(section_diffs)
    checks.append(_check("section_properties", section_diffs))

    if image_diffs:
        checks.append(_check("inline_images", image_diffs))
    else:
        checks.append(IntegrityCheck(
            name="inline_images", status="passed",
            details=["Inline image references and binary hashes match the expected document."],
        ))

    unexpected.extend(package_diffs)
    checks.append(_check("docx_package", package_diffs))

    if unverified:
        checks.append(IntegrityCheck(
            name="unsupported_features", status="partially_verified",
            details=list(dict.fromkeys(unverified)),
        ))
    status: Literal["passed", "failed", "partially_verified"]
    if unexpected:
        status = "failed"
    elif unverified:
        status = "partially_verified"
    else:
        status = "passed"
    if status == "passed":
        summary = "All supported integrity checks passed; no unverified features were detected."
    elif status == "partially_verified":
        summary = "Supported checks passed, but the report includes features outside the verifier's full inspection scope."
    else:
        summary = f"Integrity verification found {len(unexpected)} unexpected change(s)."
    return IntegrityReport(
        status=status,
        checks=checks,
        unexpected_changes=unexpected,
        expected_changes=expected,
        unverified_features=list(dict.fromkeys(unverified)),
        errors=errors,
        summary=summary,
    )


def _failure_report(check_name: str, error: str) -> IntegrityReport:
    """Create a failed report when validation or file inspection cannot proceed."""
    return IntegrityReport(
        status="failed",
        checks=[IntegrityCheck(name=check_name, status="failed", details=[error])],
        unexpected_changes=[],
        expected_changes=[],
        unverified_features=[],
        errors=[error],
        summary=f"Integrity verification could not complete: {error}",
    )


def _append_package_failures(report: IntegrityReport, differences: list[str]) -> None:
    """Retain package removals discovered before a document parser failure."""
    if not differences:
        return
    report.unexpected_changes.extend(differences)
    report.checks.append(_check("docx_package", differences))
    report.summary = f"Integrity verification found {len(report.unexpected_changes)} unexpected change(s) and could not complete document inspection."


def _check(name: str, differences: list[str]) -> IntegrityCheck:
    """Represent an empty difference list as passed and a non-empty one as failed."""
    return IntegrityCheck(
        name=name,
        status="failed" if differences else "passed",
        details=differences or ["No unexpected differences detected."],
    )


def _snapshot(path: Path, package_hashes: dict[str, str], names: set[str]) -> _Snapshot:
    """Inspect supported DOCX structures using previously bounded package data."""
    if not path.exists():
        raise FileNotFoundError(f"DOCX file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"DOCX path is not a file: {path}")
    required = {"[Content_Types].xml", "word/document.xml"}
    if not required.issubset(names):
        raise ValueError(f"DOCX package is missing required components: {sorted(required - names)}")
    try:
        document = Document(str(path))
    except Exception as exc:
        raise ValueError(f"Could not open DOCX document {path}: {exc}") from exc

    regions: dict[tuple[Any, ...], list[_Paragraph]] = {("body",): []}
    block_orders: dict[tuple[Any, ...], list[tuple[str, Any]]] = {("body",): []}
    table_shapes: list[tuple[int, tuple[int, ...]]] = []
    table_properties: list[tuple[int, tuple[Any, ...]]] = []
    _collect_parent(document, ("body",), regions, block_orders, table_shapes, table_properties)
    section_properties = _xml_signature(document.element.body.sectPr)
    unverified = _find_unverified_features(names, path)
    for paragraphs in regions.values():
        if any(not paragraph.replacement_model_complete for paragraph in paragraphs):
            unverified.append("Some paragraph text is not represented by the editor's direct safe-run model.")
        if any(any(image.startswith("unresolved-") for image in paragraph.image_hashes) for paragraph in paragraphs):
            unverified.append("An external or unresolved image reference could not be mapped to binary content.")
    return _Snapshot(
        regions, block_orders, table_shapes, table_properties,
        section_properties, package_hashes, unverified,
    )


def _package_hashes(path: Path) -> tuple[dict[str, str], set[str]]:
    """Hash every ZIP member with entry-count and decompressed-size limits."""
    hashes: dict[str, str] = {}
    names: set[str] = set()
    total_size = 0
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise ValueError(f"DOCX package exceeds the {_MAX_ZIP_ENTRIES} entry safety limit.")
        for info in infos:
            if info.filename in names:
                raise ValueError(f"DOCX package contains a duplicate ZIP entry: {info.filename}")
            names.add(info.filename)
            if info.file_size > _MAX_MEMBER_BYTES:
                raise ValueError(f"DOCX component exceeds the per-entry size limit: {info.filename}")
            total_size += info.file_size
            if total_size > _MAX_TOTAL_BYTES:
                raise ValueError("DOCX package exceeds the total uncompressed size limit.")
            digest = hashlib.sha256()
            with archive.open(info, "r") as member:
                for chunk in iter(lambda: member.read(_READ_CHUNK), b""):
                    digest.update(chunk)
            hashes[info.filename] = digest.hexdigest()
    return hashes, names


def _collect_parent(
    parent: DocumentObject | _Cell,
    region: tuple[Any, ...],
    regions: dict[tuple[Any, ...], list[_Paragraph]],
    block_orders: dict[tuple[Any, ...], list[tuple[str, Any]]],
    table_shapes: list[tuple[int, tuple[int, ...]]],
    table_properties: list[tuple[int, tuple[Any, ...]]],
) -> None:
    """Collect direct paragraphs and recurse through table cells in document order."""
    regions.setdefault(region, [])
    block_orders.setdefault(region, [])
    for block in parent.iter_inner_content():
        if isinstance(block, Paragraph):
            signature = _paragraph_signature(block)
            regions[region].append(signature)
            block_orders[region].append(("paragraph", signature))
        elif isinstance(block, Table):
            table_index = len(table_shapes)
            table_shapes.append((table_index, tuple(len(row.cells) for row in block.rows)))
            table_properties.append((table_index, _table_property_signature(block)))
            block_orders[region].append(("table", table_index))
            for row_index, row in enumerate(block.rows):
                for cell_index, cell in enumerate(row.cells):
                    _collect_parent(
                        cell, ("table", table_index, row_index, cell_index),
                        regions, block_orders, table_shapes, table_properties,
                    )


def _paragraph_signature(paragraph: Paragraph) -> _Paragraph:
    """Read text, paragraph formatting, run properties, and referenced image hashes."""
    run_properties = tuple((
        run.bold,
        run.italic,
        run.underline,
        run.font.name,
        run.font.size.pt if run.font.size is not None else None,
    ) for run in paragraph.runs)
    known_run_properties = {qn("w:b"), qn("w:i"), qn("w:u"), qn("w:rFonts"), qn("w:sz")}
    other_run_properties = tuple(
        tuple(_xml_signature(child) for child in run._r.rPr if child.tag not in known_run_properties)
        if run._r.rPr is not None else ()
        for run in paragraph.runs
    )
    paragraph_properties = tuple(
        _xml_signature(child)
        for child in paragraph._p.pPr
        if child.tag not in (qn("w:pStyle"), qn("w:jc"))
    ) if paragraph._p.pPr is not None else ()
    images: list[str] = []
    for blip in paragraph._p.xpath(".//a:blip"):
        relationship_id = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if relationship_id is None:
            images.append("unresolved-image-link")
            continue
        part = paragraph.part.related_parts.get(relationship_id)
        if part is None or not hasattr(part, "blob"):
            images.append("unresolved-image-part")
            continue
        images.append(hashlib.sha256(part.blob).hexdigest())
    replacement_segments = _replacement_segments(paragraph)
    replacement_model_complete = "".join(text for _, text in replacement_segments) == paragraph.text
    if not replacement_model_complete:
        # Content exposed by python-docx but not represented as direct text runs
        # is preserved as an uneditable piece by the expectation model.
        replacement_segments = ((False, paragraph.text),)
    return _Paragraph(
        text=paragraph.text,
        replacement_segments=replacement_segments,
        style=paragraph.style.name if paragraph.style is not None else None,
        alignment=str(paragraph.alignment) if paragraph.alignment is not None else None,
        run_properties=run_properties,
        other_run_properties=other_run_properties,
        other_paragraph_properties=paragraph_properties,
        image_hashes=tuple(images),
        replacement_model_complete=replacement_model_complete,
    )


def _replacement_segments(paragraph: Paragraph) -> tuple[tuple[bool, str], ...]:
    """Mirror the editor's contiguous safe-run segments for replacement matching."""
    segments: list[tuple[bool, str]] = []
    for run_element in paragraph._p.r_lst:
        safe = all(child.tag in (qn("w:rPr"), qn("w:t")) for child in run_element)
        if safe:
            text = "".join(node.text or "" for node in run_element.findall(qn("w:t")))
            if segments and segments[-1][0]:
                previous = segments[-1][1]
                segments[-1] = (True, previous + text)
            else:
                segments.append((True, text))
        else:
            segments.append((False, Run(run_element, paragraph).text))
    return tuple(segments)


def _apply_expected_operations(
    snapshot: _Snapshot,
    operations: list[DocumentOperation],
) -> tuple[
    dict[tuple[Any, ...], list[_Paragraph]],
    dict[tuple[Any, ...], list[tuple[str, Any]]],
    list[str],
    list[str],
]:
    """Simulate approved text and paragraph operations over a copied snapshot."""
    regions = {key: list(value) for key, value in snapshot.regions.items()}
    block_orders = {key: list(value) for key, value in snapshot.block_orders.items()}
    errors: list[str] = []
    expected: list[str] = []
    for index, operation in enumerate(operations, start=1):
        if isinstance(operation, ReplaceOperation):
            occurrences = 0
            updated: dict[tuple[Any, ...], list[_Paragraph]] = {}
            replacements: dict[int, _Paragraph] = {}
            for region, paragraphs in regions.items():
                changed: list[_Paragraph] = []
                for paragraph in paragraphs:
                    count = sum(
                        text.count(operation.target_text)
                        for is_replaceable, text in paragraph.replacement_segments
                        if is_replaceable
                    )
                    occurrences += count
                    changed_segments = tuple(
                        (is_replaceable,
                         text.replace(operation.target_text, operation.replacement_text) if is_replaceable else text)
                        for is_replaceable, text in paragraph.replacement_segments
                    )
                    changed_text = "".join(text for _, text in changed_segments)
                    updated_paragraph = _Paragraph(
                        text=changed_text,
                        replacement_segments=changed_segments,
                        style=paragraph.style,
                        alignment=paragraph.alignment,
                        run_properties=paragraph.run_properties,
                        other_run_properties=paragraph.other_run_properties,
                        other_paragraph_properties=paragraph.other_paragraph_properties,
                        image_hashes=paragraph.image_hashes,
                        replacement_model_complete=paragraph.replacement_model_complete,
                    )
                    changed.append(updated_paragraph)
                    replacements[id(paragraph)] = updated_paragraph
                updated[region] = changed
            regions = updated
            for region, tokens in block_orders.items():
                block_orders[region] = [
                    (kind, replacements.get(id(value), value) if kind == "paragraph" else value)
                    for kind, value in tokens
                ]
            if occurrences:
                expected.append(f"Operation {index}: replaced {occurrences} occurrence(s) of {operation.target_text!r}.")
            else:
                errors.append(
                    f"Operation {index}: replacement target {operation.target_text!r} was not found in an editable safe text segment."
                )
            continue

        selected = _select_paragraph(regions, operation)
        if selected is None:
            target = operation.anchor_paragraph_text if isinstance(operation, InsertOperation) else operation.target_text
            errors.append(f"Operation {index}: could not uniquely resolve paragraph target {target!r}.")
            continue
        region, position, target_paragraph = selected
        if isinstance(operation, InsertOperation):
            style = operation.paragraph_style if operation.paragraph_style is not None else target_paragraph.style
            inserted = _Paragraph(
                text=operation.inserted_text,
                replacement_segments=((True, operation.inserted_text),),
                style=style,
                alignment=None,
                run_properties=((None, None, None, None, None),),
                other_run_properties=((),),
                other_paragraph_properties=(),
                image_hashes=(),
                replacement_model_complete=True,
            )
            insert_at = position if operation.position == "before" else position + 1
            regions[region].insert(insert_at, inserted)
            target_token_index = next(
                (token_index for token_index, (kind, value) in enumerate(block_orders[region])
                 if kind == "paragraph" and value is target_paragraph),
                None,
            )
            if target_token_index is None:
                errors.append(f"Operation {index}: paragraph block location could not be resolved.")
                regions[region].pop(insert_at)
                continue
            block_orders[region].insert(
                target_token_index if operation.position == "before" else target_token_index + 1,
                ("paragraph", inserted),
            )
            expected.append(f"Operation {index}: inserted a paragraph {operation.position} {target_paragraph.text!r}.")
        elif isinstance(operation, RemoveOperation):
            regions[region].pop(position)
            block_orders[region] = [
                token for token in block_orders[region]
                if not (token[0] == "paragraph" and token[1] is target_paragraph)
            ]
            expected.append(f"Operation {index}: removed paragraph {target_paragraph.text!r}.")
    return regions, block_orders, errors, expected


def _select_paragraph(
    regions: dict[tuple[Any, ...], list[_Paragraph]],
    operation: InsertOperation | RemoveOperation,
) -> tuple[tuple[Any, ...], int, _Paragraph] | None:
    """Resolve a paragraph target uniquely or by its explicitly approved location."""
    target = operation.anchor_paragraph_text if isinstance(operation, InsertOperation) else operation.target_text
    if operation.location is not None:
        location = tuple(operation.location)
        if len(location) == 1:
            region = ("body",)
            position = location[0]
            items = regions.get(region, [])
            if 0 <= position < len(items) and items[position].text == target:
                return region, position, items[position]
            return None
        if len(location) == 4:
            table_index, row_index, cell_index, position = location
            region = ("table", table_index, row_index, cell_index)
            items = regions.get(region, [])
            if 0 <= position < len(items) and items[position].text == target:
                return region, position, items[position]
            return None
        return None
    matches = [
        (region, position, paragraph)
        for region, items in regions.items()
        for position, paragraph in enumerate(items)
        if paragraph.text == target
    ]
    return matches[0] if len(matches) == 1 else None


def _compare_regions(
    expected: dict[tuple[Any, ...], list[_Paragraph]],
    actual: dict[tuple[Any, ...], list[_Paragraph]],
) -> tuple[list[str], list[str], list[str]]:
    """Compare ordered paragraph text, formatting properties, and image hashes."""
    text_diffs: list[str] = []
    format_diffs: list[str] = []
    image_diffs: list[str] = []
    for region in sorted(set(expected) | set(actual), key=repr):
        wanted = expected.get(region, [])
        found = actual.get(region, [])
        label = _region_label(region)
        if [item.text for item in wanted] != [item.text for item in found]:
            text_diffs.append(f"Paragraph text or order differs in {label}.")
        if [(
            item.style, item.alignment, item.run_properties,
            item.other_run_properties, item.other_paragraph_properties,
        ) for item in wanted] != [(
            item.style, item.alignment, item.run_properties,
            item.other_run_properties, item.other_paragraph_properties,
        ) for item in found
        ]:
            format_diffs.append(f"Paragraph style, alignment, or run formatting differs in {label}.")
        if [item.image_hashes for item in wanted] != [item.image_hashes for item in found]:
            image_diffs.append(f"Inline image count or binary content differs in {label}.")
    return text_diffs, format_diffs, image_diffs


def _compare_block_orders(
    expected: dict[tuple[Any, ...], list[tuple[str, Any]]],
    actual: dict[tuple[Any, ...], list[tuple[str, Any]]],
) -> list[str]:
    """Ensure paragraph blocks stay in place relative to tables in each container."""
    differences: list[str] = []
    for region in sorted(set(expected) | set(actual), key=repr):
        expected_order = [(kind, value if kind == "table" else None) for kind, value in expected.get(region, [])]
        actual_order = [(kind, value if kind == "table" else None) for kind, value in actual.get(region, [])]
        if expected_order != actual_order:
            differences.append(f"Paragraph/table block order differs in {_region_label(region)}.")
    return differences


def _compare_table_shapes(
    original: list[tuple[int, tuple[int, ...]]],
    edited: list[tuple[int, tuple[int, ...]]],
) -> list[str]:
    """Report table count, row count, and cell-count-per-row changes."""
    if original == edited:
        return []
    return [f"Table count or dimensions changed from {original!r} to {edited!r}."]


def _table_property_signature(table: Table) -> tuple[Any, ...]:
    """Capture table, row, and cell properties not represented by dimensions."""
    table_props = _xml_signature(table._tbl.tblPr)
    grid_props = _xml_signature(table._tbl.tblGrid)
    row_props = tuple(
        (_xml_signature(row._tr.trPr), tuple(_xml_signature(cell._tc.tcPr) for cell in row.cells))
        for row in table.rows
    )
    return table_props, grid_props, row_props


def _xml_signature(element: Any | None) -> str:
    """Serialize an OOXML element deterministically for supported-property comparison."""
    if element is None:
        return ""
    return etree.tostring(element, encoding="unicode", pretty_print=True)


def _compare_table_properties(
    original: list[tuple[int, tuple[Any, ...]]],
    edited: list[tuple[int, tuple[Any, ...]]],
) -> list[str]:
    """Detect changes to table, row, and cell formatting properties."""
    if original == edited:
        return []
    return ["Table, row, or cell properties changed unexpectedly."]


def _compare_section_properties(original: str, edited: str) -> list[str]:
    """Ensure page and section properties outside paragraph comparisons are preserved."""
    if original == edited:
        return []
    return ["Document section properties changed unexpectedly."]


def _compare_packages(original: dict[str, str], edited: dict[str, str]) -> list[str]:
    """Detect package part additions/removals and all changed non-document parts."""
    differences: list[str] = []
    for name in sorted(set(original) - set(edited)):
        differences.append(f"DOCX package component was removed: {name}.")
    for name in sorted(set(edited) - set(original)):
        differences.append(f"DOCX package component was added unexpectedly: {name}.")
    for name in sorted(set(original) & set(edited)):
        if name == "word/document.xml":
            continue
        if original[name] != edited[name]:
            category = "relationship" if name.endswith(".rels") else "media asset" if name.startswith("word/media/") else "package component"
            differences.append(f"DOCX {category} changed unexpectedly: {name}.")
    return differences


def _find_unverified_features(names: set[str], path: Path) -> list[str]:
    """Identify known Word feature families outside the structural comparison scope."""
    features: list[str] = []
    # Header/footer parts and their relationships are already compared byte for
    # byte by _compare_packages; section references are checked separately.
    # Since the editor only changes body content, their semantics do not need
    # a separate partial-verification downgrade when those package parts match.
    package_prefixes = {
        "word/footnotes": "footnotes",
        "word/endnotes": "endnotes",
        "word/comments": "comments",
        "word/embeddings/": "embedded objects",
        "word/charts/": "charts",
        "word/diagrams/": "diagrams and SmartArt",
    }
    binary_prefixes = {
        "word/embeddings/": "embedded objects",
        "word/activeX/": "ActiveX objects",
    }
    with zipfile.ZipFile(path, "r") as archive:
        for prefix, label in package_prefixes.items():
            matching = [name for name in names if name.startswith(prefix)]
            if matching and any(_xml_part_has_content(archive.read(name)) for name in matching):
                features.append(f"{label} are preserved as package parts but their internal document semantics are not verified.")
        for prefix, label in binary_prefixes.items():
            if any(name.startswith(prefix) for name in names):
                features.append(f"{label} are preserved as package parts but their internal semantics are not verified.")
        document_xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    drawing_word_ns = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    relationships_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    markup_compat_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    recognized_namespaces = {word_ns, drawing_ns, drawing_word_ns, relationships_ns, markup_compat_ns}
    unsupported_word_tags = {
        "ins": "tracked insertions", "del": "tracked deletions", "sdt": "content controls",
        "hyperlink": "hyperlinks", "txbxContent": "text boxes", "altChunk": "altChunk content",
        "pict": "VML pictures or shapes", "object": "embedded objects in document content",
        "fldChar": "complex fields", "fldSimple": "simple fields", "instrText": "field instructions",
        "bookmarkStart": "bookmarks", "commentRangeStart": "comment anchors",
        "moveFrom": "tracked move content", "moveTo": "tracked move content",
        "customXml": "custom XML wrappers", "footnoteReference": "footnote references",
        "endnoteReference": "endnote references", "permStart": "document permission ranges",
        "proofErr": "proofing annotations", "smartTag": "smart tags",
    }
    observed: set[str] = set()
    for element in root.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith("{"):
            continue
        namespace, local_name = element.tag[1:].split("}", 1)
        if namespace == word_ns and local_name in unsupported_word_tags:
            observed.add(unsupported_word_tags[local_name])
        elif namespace == markup_compat_ns and local_name == "AlternateContent":
            observed.add("markup compatibility alternate content")
        elif namespace == drawing_ns and local_name == "blip":
            observed.add("inline image placement and cropping")
        elif namespace not in recognized_namespaces:
            observed.add(f"extension XML namespace {namespace}")
    for label in sorted(observed):
        features.append(f"{label} are present and are not fully interpreted by this verifier.")
    return features


def _xml_part_has_content(xml_data: bytes) -> bool:
    """Return whether a header-like XML part has visible or structural content."""
    root = ElementTree.fromstring(xml_data)
    content_tags = {"drawing", "pict", "tbl", "tab", "br", "object", "fldChar"}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "t" and element.text and element.text.strip():
            return True
        if local_name in content_tags:
            return True
    return False


def _region_label(region: tuple[Any, ...]) -> str:
    """Format a structural paragraph region for report messages."""
    if region == ("body",):
        return "the document body"
    return f"table {region[1]}, row {region[2]}, cell {region[3]}"
