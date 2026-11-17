from __future__ import annotations

import shutil
import zipfile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches
from docx.shared import RGBColor
import pytest

from backend.docx_editor.integrity import verify_document_integrity
from backend.docx_editor import insert_paragraph, remove_paragraph, replace_text
from backend.agent.schemas import ReplaceOperation as AgentReplaceOperation
from backend.operation_schemas import ReplaceOperation


def _verify(source, edited, operations=()):
    return verify_document_integrity(source, edited, list(operations))


def test_unchanged_document_passes(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "unchanged.docx"
    shutil.copyfile(source, edited)

    report = _verify(source, edited)

    assert report.status == "passed"
    assert not report.unexpected_changes
    assert not report.unverified_features


def test_approved_replacement_is_expected(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "replaced.docx"
    operation = {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated"}
    replace_text(source, "Unrelated", "Updated", edited)

    report = _verify(source, edited, [operation])

    assert report.status == "passed"
    assert report.expected_changes
    assert not report.unexpected_changes


def test_approved_replacement_across_adjacent_runs_preserves_formatting(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "cross-run-replaced.docx"
    operation = {"operation_type": "replace", "target_text": "old text", "replacement_text": "new"}
    replace_text(source, "old text", "new", edited)

    report = _verify(source, edited, [operation])

    assert report.status == "passed"
    assert Document(edited).paragraphs[0].text == "Keep new end"


def test_approved_replacement_models_multiple_occurrences(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    doc.paragraphs[1].runs[0].text = "cat cat cat"
    doc.save(source)
    edited = tmp_path / "multiple-replaced.docx"
    operation = {"operation_type": "replace", "target_text": "cat", "replacement_text": "dog"}
    replace_text(source, "cat", "dog", edited)

    report = _verify(source, edited, [operation])

    assert report.status == "passed"
    assert "3 occurrence(s)" in report.expected_changes[0]


def test_replacement_across_non_text_boundary_is_not_expected(make_docx, tmp_path):
    source = make_docx(with_image=True)
    doc = Document(source)
    paragraph = doc.add_paragraph()
    paragraph.add_run("before")
    paragraph.add_run().add_picture(str(tmp_path / "tiny.png"))
    paragraph.add_run("after")
    doc.save(source)
    edited = tmp_path / "boundary-unchanged.docx"
    shutil.copyfile(source, edited)
    operation = {"operation_type": "replace", "target_text": "beforeafter", "replacement_text": "joined"}

    report = _verify(source, edited, [operation])

    assert report.status == "failed"
    assert any("replacement target" in item for item in report.unexpected_changes)


def test_replacement_does_not_excuse_unrelated_change_in_same_paragraph(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "replace-plus-extra.docx"
    operation = {"operation_type": "replace", "target_text": "old", "replacement_text": "new"}
    replace_text(source, "old", "new", edited)
    doc = Document(edited)
    doc.paragraphs[0].runs[0].text = "Changed "
    doc.save(edited)

    report = _verify(source, edited, [operation])

    assert report.status == "failed"
    assert any("Paragraph text or order" in item for item in report.unexpected_changes)


def test_approved_insertion_is_expected(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "inserted.docx"
    operation = {
        "operation_type": "insert", "anchor_paragraph_text": "Unrelated paragraph",
        "inserted_text": "Added paragraph", "position": "after", "paragraph_style": None,
    }
    insert_paragraph(source, "Unrelated paragraph", "Added paragraph", output_path=edited)

    report = _verify(source, edited, [operation])

    assert report.status == "passed"
    assert not report.unexpected_changes


def test_approved_removal_is_expected(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "removed.docx"
    operation = {"operation_type": "remove", "target_text": "Unrelated paragraph"}
    remove_paragraph(source, "Unrelated paragraph", output_path=edited)

    report = _verify(source, edited, [operation])

    assert report.status == "passed"
    assert not report.unexpected_changes


def test_approved_insert_before_and_after_are_expected(make_docx, tmp_path):
    source = make_docx()
    before = tmp_path / "insert-before.docx"
    after = tmp_path / "insert-after.docx"
    before_op = {
        "operation_type": "insert", "anchor_paragraph_text": "Unrelated paragraph",
        "inserted_text": "Before", "position": "before",
    }
    after_op = {**before_op, "inserted_text": "After", "position": "after"}
    insert_paragraph(source, "Unrelated paragraph", "Before", "before", output_path=before)
    insert_paragraph(source, "Unrelated paragraph", "After", "after", output_path=after)

    assert _verify(source, before, [before_op]).status == "passed"
    assert _verify(source, after, [after_op]).status == "passed"


def test_sequential_operations_can_target_replaced_body_paragraph(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "replace-then-insert.docx"
    ops = [
        {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Changed"},
        {
            "operation_type": "insert", "anchor_paragraph_text": "Changed paragraph",
            "inserted_text": "Following", "position": "after",
        },
    ]
    replace_text(source, "Unrelated", "Changed", edited)
    insert_paragraph(edited, "Changed paragraph", "Following", output_path=edited)

    report = _verify(source, edited, ops)

    assert report.status == "passed"


def test_approved_insertion_inside_table_cell_is_expected(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "cell-insert.docx"
    operation = {
        "operation_type": "insert", "anchor_paragraph_text": "Cell target value",
        "inserted_text": "Cell addition", "position": "after", "location": [0, 0, 0, 0],
    }
    insert_paragraph(source, "Cell target value", "Cell addition", output_path=edited, location=(0, 0, 0, 0))

    report = _verify(source, edited, [operation])

    assert report.status == "passed"


def test_approved_removal_from_table_cell_is_expected(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    cell = doc.tables[0].cell(0, 0)
    cell.add_paragraph("Preserve this")
    doc.save(source)
    edited = tmp_path / "cell-remove.docx"
    operation = {"operation_type": "remove", "target_text": "Cell target value", "location": [0, 0, 0, 0]}
    remove_paragraph(source, "Cell target value", output_path=edited, location=(0, 0, 0, 0))

    report = _verify(source, edited, [operation])

    assert report.status == "passed"


def test_explicit_location_resolves_duplicate_targets(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(source)
    edited = tmp_path / "located-insert.docx"
    operation = {
        "operation_type": "insert", "anchor_paragraph_text": "Repeated",
        "inserted_text": "Selected", "position": "after", "location": [3],
    }
    insert_paragraph(source, "Repeated", "Selected", output_path=edited, location=(3,))

    report = _verify(source, edited, [operation])

    assert report.status == "passed"


def test_explicit_removal_location_resolves_duplicate_targets(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(source)
    edited = tmp_path / "located-remove.docx"
    operation = {"operation_type": "remove", "target_text": "Repeated", "location": [2]}
    remove_paragraph(source, "Repeated", output_path=edited, location=(2,))

    report = _verify(source, edited, [operation])

    assert report.status == "passed"


def test_ambiguous_target_without_location_is_rejected(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(source)
    edited = tmp_path / "ambiguous-unchanged.docx"
    shutil.copyfile(source, edited)
    operation = {
        "operation_type": "remove", "target_text": "Repeated",
    }

    report = _verify(source, edited, [operation])

    assert report.status == "failed"
    assert any("uniquely resolve" in item for item in report.unexpected_changes)


def test_unapproved_paragraph_deletion_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "deleted.docx"
    doc = Document(source)
    doc.paragraphs[1]._p.getparent().remove(doc.paragraphs[1]._p)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("Paragraph text or order" in item for item in report.unexpected_changes)


def test_unapproved_paragraph_modification_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "modified.docx"
    doc = Document(source)
    doc.paragraphs[1].text = "Unexpected change"
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert report.unexpected_changes


def test_paragraph_moved_across_table_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "moved-across-table.docx"
    doc = Document(source)
    paragraph = doc.paragraphs[1]
    doc.tables[0]._tbl.addnext(paragraph._p)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("block order" in item for item in report.unexpected_changes)


def test_unrelated_table_cell_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "cell-change.docx"
    doc = Document(source)
    doc.tables[0].cell(0, 0).text = "Changed cell"
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("table 0, row 0, cell 0" in item for item in report.unexpected_changes)


def test_table_dimension_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "table-shape.docx"
    doc = Document(source)
    doc.tables[0].add_row()
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("dimensions changed" in item for item in report.unexpected_changes)


def test_unapproved_formatting_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "format-change.docx"
    doc = Document(source)
    doc.paragraphs[1].runs[0].bold = True
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("run formatting" in item for item in report.unexpected_changes)


def test_unapproved_direct_paragraph_property_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "indent-change.docx"
    doc = Document(source)
    doc.paragraphs[1].paragraph_format.left_indent = Inches(0.5)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("run formatting" in item for item in report.unexpected_changes)


def test_unapproved_table_cell_property_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "cell-format-change.docx"
    doc = Document(source)
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), "FFFF00")
    doc.tables[0].cell(0, 0)._tc.get_or_add_tcPr().append(shading)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("cell properties" in item for item in report.unexpected_changes)


def test_unapproved_run_color_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "font-color-change.docx"
    doc = Document(source)
    doc.paragraphs[1].runs[0].font.color.rgb = RGBColor(255, 0, 0)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("run formatting" in item for item in report.unexpected_changes)


def test_unapproved_section_property_change_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "page-margin-change.docx"
    doc = Document(source)
    doc.sections[0].top_margin = Inches(2)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("section properties" in item for item in report.unexpected_changes)


def test_inline_image_loss_fails(make_docx, tmp_path):
    source = make_docx(with_image=True)
    edited = tmp_path / "image-loss.docx"
    doc = Document(source)
    image_paragraph = next(p for p in doc.paragraphs if p._p.xpath(".//a:blip"))
    image_paragraph._p.getparent().remove(image_paragraph._p)
    doc.save(edited)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("Inline image" in item for item in report.unexpected_changes)


def test_inline_image_binary_replacement_fails(make_docx, tmp_path):
    source = make_docx(with_image=True)
    edited = tmp_path / "image-replaced.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(edited, "w") as output:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename.startswith("word/media/"):
                content = content + b"unexpected"
            output.writestr(item, content)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("image" in item.lower() for item in report.unexpected_changes)


def test_changed_image_relationship_is_detected(make_docx, tmp_path):
    source = make_docx(with_image=True)
    edited = tmp_path / "relationship-changed.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(edited, "w") as output:
        image = original.read("word/media/image1.png")
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                content = content.replace(b"media/image1.png", b"media/image2.png")
            output.writestr(item, content)
        output.writestr("word/media/image2.png", image)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("relationship changed" in item for item in report.unexpected_changes)


def test_changed_unrelated_package_part_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "package-part-changed.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(edited, "w") as output:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename == "docProps/core.xml":
                content = content.replace(b"python-docx", b"modified")
            output.writestr(item, content)

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("package component changed" in item for item in report.unexpected_changes)


def test_harmless_zip_recompression_and_entry_order_pass(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "repacked.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
        edited, "w", compression=zipfile.ZIP_STORED,
    ) as output:
        for item in reversed(original.infolist()):
            output.writestr(item.filename, original.read(item.filename))

    report = _verify(source, edited)

    assert report.status == "passed"


def test_python_docx_reserialization_without_edits_passes(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "reserialized.docx"
    Document(source).save(edited)

    report = _verify(source, edited)

    assert report.status == "passed"
    assert not report.unexpected_changes


def test_image_layout_scope_is_reported_as_partial(make_docx, tmp_path):
    source = make_docx(with_image=True)
    edited = tmp_path / "image-reserialized.docx"
    Document(source).save(edited)

    report = _verify(source, edited)

    assert report.status == "partially_verified"
    assert any("image placement" in item for item in report.unverified_features)
    assert not report.unexpected_changes


def test_zip_component_size_limit_is_enforced(make_docx, tmp_path, monkeypatch):
    import backend.docx_editor.integrity as integrity

    source = make_docx()
    oversized = tmp_path / "oversized-component.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(oversized, "w") as output:
        for item in original.infolist():
            output.writestr(item.filename, original.read(item.filename))
        output.writestr("oversized.bin", b"x" * 32)
    monkeypatch.setattr(integrity, "_MAX_MEMBER_BYTES", 16)

    report = _verify(source, oversized)

    assert report.status == "failed"
    assert any("per-entry size limit" in error for error in report.errors)


@pytest.mark.parametrize(("limit_name", "limit_value", "message"), [
    ("_MAX_ZIP_ENTRIES", 1, "entry safety limit"),
    ("_MAX_TOTAL_BYTES", 16, "total uncompressed size limit"),
])
def test_zip_entry_count_and_total_size_limits_are_enforced(
    make_docx, tmp_path, monkeypatch, limit_name, limit_value, message,
):
    import backend.docx_editor.integrity as integrity

    source = make_docx()
    monkeypatch.setattr(integrity, limit_name, limit_value)

    report = _verify(source, source)

    assert report.status == "failed"
    assert any(message in error for error in report.errors)


def test_removed_package_component_fails(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "component-removed.docx"
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(edited, "w") as output:
        for item in original.infolist():
            if item.filename != "docProps/core.xml":
                output.writestr(item, original.read(item.filename))

    report = _verify(source, edited)

    assert report.status == "failed"
    assert any("component was removed" in item for item in report.unexpected_changes)


def test_missing_and_malformed_documents_return_structured_failures(tmp_path, make_docx):
    source = make_docx()
    missing = _verify(source, tmp_path / "absent.docx")
    malformed_path = tmp_path / "malformed.docx"
    malformed_path.write_bytes(b"not a zip")
    malformed = _verify(source, malformed_path)

    assert missing.status == "failed" and missing.errors
    assert malformed.status == "failed" and malformed.errors
    assert missing.checks[0].status == "failed"
    assert malformed.checks[0].status == "failed"


def test_verifier_never_changes_either_file(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "edited.docx"
    replace_text(source, "Unrelated", "Changed", edited)
    before_source, before_edited = source.read_bytes(), edited.read_bytes()

    _verify(source, edited, [{
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Changed",
    }])

    assert source.read_bytes() == before_source
    assert edited.read_bytes() == before_edited


def test_nested_table_cells_are_compared(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    nested = doc.tables[0].cell(0, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "Nested original"
    doc.save(source)
    unchanged = tmp_path / "nested-unchanged.docx"
    shutil.copyfile(source, unchanged)

    passed = _verify(source, unchanged)
    changed_doc = Document(source)
    changed_doc.tables[0].cell(0, 0).tables[0].cell(0, 0).text = "Nested changed"
    changed_path = tmp_path / "nested-changed.docx"
    changed_doc.save(changed_path)
    failed = _verify(source, changed_path)

    assert passed.status == "passed"
    assert failed.status == "failed"
    assert any("table 1, row 0, cell 0" in item for item in failed.unexpected_changes)


def test_expected_operations_in_nested_table_use_flat_table_locations(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    nested = doc.tables[0].cell(0, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).paragraphs[0].text = "Nested original"
    nested.cell(0, 0).add_paragraph("Keep this")
    doc.save(source)
    edited = tmp_path / "nested-operations.docx"
    ops = [
        {"operation_type": "replace", "target_text": "Nested", "replacement_text": "Inside"},
        {
            "operation_type": "insert", "anchor_paragraph_text": "Inside original",
            "inserted_text": "Added nested", "position": "after", "location": [1, 0, 0, 0],
        },
        {"operation_type": "remove", "target_text": "Keep this", "location": [1, 0, 0, 2]},
    ]
    replace_text(source, "Nested", "Inside", edited)
    insert_paragraph(edited, "Inside original", "Added nested", output_path=edited, location=(1, 0, 0, 0))
    remove_paragraph(edited, "Keep this", output_path=edited, location=(1, 0, 0, 2))

    report = _verify(source, edited, ops)

    assert report.status == "passed"


def test_header_feature_is_partially_verified(make_docx, tmp_path):
    source = make_docx()
    doc = Document(source)
    doc.sections[0].header.paragraphs[0].text = "Header feature"
    doc.save(source)
    edited = tmp_path / "header-copy.docx"
    shutil.copyfile(source, edited)

    report = _verify(source, edited)

    assert report.status == "partially_verified"
    assert any("headers" in item for item in report.unverified_features)


def test_invalid_approved_operation_returns_structured_failure(make_docx, tmp_path):
    source = make_docx()
    edited = tmp_path / "same.docx"
    shutil.copyfile(source, edited)

    report = _verify(source, edited, [{"operation_type": "run_python", "code": "pass"}])

    assert report.status == "failed"
    assert report.checks[0].name == "approved_operations"
    assert report.errors


def test_agent_schema_path_reexports_shared_schema_identity():
    assert AgentReplaceOperation is ReplaceOperation
