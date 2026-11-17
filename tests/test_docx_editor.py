import zipfile

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH

from backend.docx_editor import insert_paragraph, inspect_document, remove_paragraph, replace_text
from backend.docx_editor.inspection import inspect_document_with_planner_context


def test_single_run_replacement_and_saved_reopen(make_docx, tmp_path):
    """Checks a simple replacement by reopening and reading the saved DOCX."""
    src = make_docx()
    out = tmp_path / "edited.docx"
    result = replace_text(src, "Unrelated", "Changed", out)
    assert result.replacements == 1
    reopened = Document(out)
    assert reopened.paragraphs[1].text == "Changed paragraph"


def test_replacing_within_single_run_keeps_surrounding_text(make_docx, tmp_path):
    """Checks that replacing part of one run leaves the rest of the paragraph."""
    src = make_docx()
    out = tmp_path / "edited.docx"
    result = replace_text(src, "Unrelated", "Edited", out)
    assert result.replacements == 1
    assert Document(out).paragraphs[1].text == "Edited paragraph"


def test_cross_run_preserves_formatting_and_surrounding_text(make_docx, tmp_path):
    """Checks cross-run replacement keeps surrounding text and first-run styling."""
    src = make_docx()
    out = tmp_path / "edited.docx"
    result = replace_text(src, "old text", "new", out)
    assert result.replacements == 1
    p = Document(out).paragraphs[0]
    assert p.text == "Keep new end"
    assert p.runs[0].text == "Keep " and p.runs[0].bold is True
    assert p.runs[1].text == "new" and p.runs[1].italic is True
    assert p.runs[2].text == " end"
    assert p.runs[2].italic is None
    assert p.style.name == "Title"
    assert p.alignment == WD_ALIGN_PARAGRAPH.CENTER


def test_table_cell_replacement_preserves_other_content(make_docx, tmp_path):
    """Checks a cell edit survives saving without changing neighboring content."""
    src = make_docx()
    out = tmp_path / "edited.docx"
    assert replace_text(src, "target", "result", out).replacements == 1
    reopened = Document(out)
    assert reopened.tables[0].cell(0, 0).text == "Cell result value"
    assert reopened.paragraphs[1].text == "Unrelated paragraph"


def test_image_and_structure_survive_replacement(make_docx, tmp_path):
    """Checks an unrelated inline image, table, and paragraph survive replacement."""
    src = make_docx(with_image=True)
    out = tmp_path / "edited.docx"
    replace_text(src, "old text", "replacement", out)
    summary = inspect_document(out)
    assert summary.inline_image_count == 1
    assert summary.table_count == 1
    assert summary.paragraphs[1].text == "Unrelated paragraph"
    assert Document(out).tables[0].cell(0, 0).text == "Cell target value"


def test_absent_target_does_not_save_or_modify(make_docx):
    """Checks a missing target reports zero edits and leaves source bytes intact."""
    src = make_docx()
    original_bytes = src.read_bytes()
    result = replace_text(src, "missing", "ignored")
    assert result.replacements == 0
    assert result.warnings
    assert src.read_bytes() == original_bytes


def test_inspection_reports_runs_styles_tables_and_images(make_docx):
    """Checks inspection reports formatting, table contents, and inline pictures."""
    src = make_docx(with_image=True)
    summary = inspect_document(src)
    assert summary.paragraph_count >= 3
    assert summary.paragraphs[0].style == "Title"
    assert summary.paragraphs[0].runs[0].bold is True
    assert summary.paragraphs[0].runs[1].italic is True
    assert summary.table_count == 1
    assert summary.tables[0].rows[0][0] == ("Cell target value",)
    assert summary.inline_image_count == 1


def test_planner_inspection_projection_preserves_order_and_cell_context(make_docx):
    src = make_docx()
    full_summary, context = inspect_document_with_planner_context(src)
    full_after = inspect_document(src).to_dict()
    import json
    planner_json = json.dumps(context, ensure_ascii=False)

    assert full_summary.to_dict() == full_after
    assert planner_json.count("Keep old text end") == 1
    assert planner_json.count("Unrelated paragraph") == 1
    assert planner_json.count("Cell target value") == 1
    blocks = context["blocks"]
    assert blocks[0] == {"p": "Keep old text end"}
    assert blocks[1] == {"p": "Unrelated paragraph"}
    assert blocks[2]["t"][0][0][0] == {"p": "Cell target value"}


def test_replaces_multiple_occurrences_in_one_paragraph(make_docx, tmp_path):
    """Checks every original occurrence in a paragraph is replaced once."""
    src = make_docx()
    doc = Document(src)
    doc.paragraphs[1].runs[0].text = "cat cat cat"
    doc.save(src)
    out = tmp_path / "multiple.docx"
    result = replace_text(src, "cat", "dog", out)
    assert result.replacements == 3
    assert Document(out).paragraphs[1].text == "dog dog dog"


def test_replaces_same_target_across_paragraphs(make_docx, tmp_path):
    """Checks matching text is replaced in more than one saved paragraph."""
    src = make_docx()
    doc = Document(src)
    doc.add_paragraph("Target one")
    doc.add_paragraph("Target two")
    doc.save(src)
    out = tmp_path / "paragraphs.docx"
    assert replace_text(src, "Target", "Updated", out).replacements == 2
    reopened = Document(out)
    assert reopened.paragraphs[-2].text == "Updated one"
    assert reopened.paragraphs[-1].text == "Updated two"


def test_replaces_text_in_nested_table(make_docx, tmp_path):
    """Checks matching text is replaced in both a parent and nested table cell."""
    src = make_docx()
    doc = Document(src)
    nested = doc.tables[0].cell(0, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).paragraphs[0].add_run("Nested target")
    doc.save(src)
    out = tmp_path / "nested.docx"
    result = replace_text(src, "target", "changed", out)
    assert result.replacements == 2
    reopened = Document(out)
    assert reopened.tables[0].cell(0, 0).paragraphs[0].text == "Cell changed value"
    assert reopened.tables[0].cell(0, 0).tables[0].cell(0, 0).text == "Nested changed"


def test_replacement_does_not_cross_picture_boundary(make_docx, tmp_path):
    """Checks a match cannot join text located on opposite sides of a picture."""
    src = make_docx(with_image=True)
    doc = Document(src)
    p = doc.add_paragraph()
    p.add_run("before")
    p.add_run().add_picture(str(tmp_path / "tiny.png"))
    p.add_run("after")
    doc.save(src)
    out = tmp_path / "boundary.docx"
    result = replace_text(src, "beforeafter", "joined", out)
    assert result.replacements == 0
    assert not out.exists()


def test_non_text_elements_remain_after_nearby_replacement(make_docx, tmp_path):
    """Checks editing nearby text preserves both inline pictures in the DOCX."""
    src = make_docx(with_image=True)
    doc = Document(src)
    p = doc.add_paragraph()
    p.add_run("before")
    p.add_run().add_picture(str(tmp_path / "tiny.png"))
    p.add_run("after")
    doc.save(src)
    out = tmp_path / "picture-preserved.docx"
    assert replace_text(src, "before", "start", out).replacements == 1
    reopened = Document(out)
    assert reopened.paragraphs[-1].text == "startafter"
    assert len(reopened.inline_shapes) == 2


def test_output_path_leaves_original_input_unchanged(make_docx, tmp_path):
    """Checks an explicit output path keeps the original document byte-for-byte."""
    src = make_docx()
    original = src.read_bytes()
    out = tmp_path / "separate-output.docx"
    assert replace_text(src, "Unrelated", "Changed", out).replacements == 1
    assert src.read_bytes() == original
    assert Document(out).paragraphs[1].text == "Changed paragraph"


def test_missing_target_does_not_create_output(make_docx, tmp_path):
    """Checks a missing match does not create the requested output file."""
    src = make_docx()
    out = tmp_path / "not-created.docx"
    result = replace_text(src, "absent", "value", out)
    assert result.replacements == 0
    assert not out.exists()


def test_empty_replacement_removes_only_target(make_docx, tmp_path):
    """Checks an empty replacement deletes only the matched text."""
    src = make_docx()
    out = tmp_path / "empty-replacement.docx"
    result = replace_text(src, "Unrelated ", "", out)
    assert result.replacements == 1
    assert Document(out).paragraphs[1].text == "paragraph"


def test_malformed_docx_raises_clear_error(tmp_path):
    """Checks invalid DOCX bytes raise a readable document-open error."""
    malformed = tmp_path / "malformed.docx"
    malformed.write_text("not a zip or DOCX file", encoding="utf-8")
    with pytest.raises(ValueError, match="Could not open DOCX document"):
        replace_text(malformed, "target", "replacement")


def test_replacements_do_not_duplicate_or_lose_unrelated_text(make_docx, tmp_path):
    """Checks multiple edits preserve all unmatched text around their targets."""
    src = make_docx()
    doc = Document(src)
    doc.paragraphs[1].runs[0].text = "alpha TARGET beta TARGET gamma"
    doc.save(src)
    out = tmp_path / "unrelated-text.docx"
    result = replace_text(src, "TARGET", "X", out)
    assert result.replacements == 2
    assert Document(out).paragraphs[1].text == "alpha X beta X gamma"


def test_replacement_text_containing_target_is_not_reprocessed(make_docx, tmp_path):
    """Checks inserted replacement text is not searched again during the pass."""
    src = make_docx()
    doc = Document(src)
    doc.paragraphs[1].runs[0].text = "start QZ end QZ"
    doc.save(src)
    out = tmp_path / "target-in-replacement.docx"
    result = replace_text(src, "QZ", "QZQZ", out)
    assert result.replacements == 2
    assert Document(out).paragraphs[1].text == "start QZQZ end QZQZ"


def test_package_parts_preserved_or_modified_as_expected(make_docx, tmp_path):
    """Compares DOCX ZIP entries to check expected document and media preservation."""
    src = make_docx(with_image=True)
    doc = Document(src)
    doc.core_properties.author = "Synthetic Test Author"
    doc.save(src)
    out = tmp_path / "package-check.docx"
    replace_text(src, "old text", "new text", out)

    with zipfile.ZipFile(src) as before, zipfile.ZipFile(out) as after:
        before_names = set(before.namelist())
        after_names = set(after.namelist())
        assert before_names == after_names
        assert "docProps/core.xml" in before_names
        assert before.read("word/document.xml") != after.read("word/document.xml")
        assert before.read("docProps/core.xml") == after.read("docProps/core.xml")
        for unchanged_part in (
            "word/styles.xml",
            "word/_rels/document.xml.rels",
            "docProps/app.xml",
        ):
            assert before.read(unchanged_part) == after.read(unchanged_part)
        media_names = [name for name in before_names if name.startswith("word/media/")]
        assert media_names
        assert all(before.read(name) == after.read(name) for name in media_names)


def test_insert_before_preserves_original_paragraph(make_docx, tmp_path):
    """Checks insertion before a target leaves its text and run formatting intact."""
    src = make_docx()
    out = tmp_path / "insert-before.docx"
    result = insert_paragraph(src, "Keep old text end", "Inserted", "before", output_path=out)
    assert result.success and result.affected_elements == 1
    reopened = Document(out)
    assert reopened.paragraphs[0].text == "Inserted"
    assert reopened.paragraphs[1].text == "Keep old text end"
    assert reopened.paragraphs[1].runs[0].bold is True
    assert reopened.paragraphs[1].runs[1].italic is True


def test_insert_after_preserves_original_paragraph(make_docx, tmp_path):
    """Checks insertion after a target preserves the original paragraph and order."""
    src = make_docx()
    out = tmp_path / "insert-after.docx"
    result = insert_paragraph(src, "Unrelated paragraph", "Following", "after", output_path=out)
    assert result.success
    reopened = Document(out)
    assert reopened.paragraphs[1].text == "Unrelated paragraph"
    assert reopened.paragraphs[2].text == "Following"


def test_insert_inside_table_cell_when_target_is_unique(make_docx, tmp_path):
    """Checks a unique cell paragraph can anchor a new paragraph in that cell."""
    src = make_docx()
    out = tmp_path / "insert-cell.docx"
    result = insert_paragraph(src, "Cell target value", "Cell addition", output_path=out)
    assert result.success
    cell = Document(out).tables[0].cell(0, 0)
    assert [p.text for p in cell.paragraphs] == ["Cell target value", "Cell addition"]


def test_insert_uses_requested_style(make_docx, tmp_path):
    """Checks a newly inserted paragraph receives the requested style name."""
    src = make_docx()
    out = tmp_path / "insert-style.docx"
    insert_paragraph(src, "Unrelated paragraph", "Styled", style="Heading 2", output_path=out)
    inserted = Document(out).paragraphs[2]
    assert inserted.text == "Styled"
    assert inserted.style.name == "Heading 2"


def test_insert_missing_target_returns_error_without_output(make_docx, tmp_path):
    """Checks missing insertion anchors return an error without creating output."""
    src = make_docx()
    out = tmp_path / "missing-insert.docx"
    result = insert_paragraph(src, "absent", "New", output_path=out)
    assert not result.success and result.error == "Target paragraph was not found."
    assert not out.exists()


def test_insert_ambiguous_target_returns_error(make_docx, tmp_path):
    """Checks duplicate insertion anchors are rejected when no location is given."""
    src = make_docx()
    doc = Document(src)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(src)
    result = insert_paragraph(src, "Repeated", "New", output_path=tmp_path / "ambiguous.docx")
    assert not result.success and "ambiguous" in result.error


def test_insert_duplicate_target_at_explicit_location(make_docx, tmp_path):
    """Checks a location selects the intended paragraph among duplicate anchors."""
    src = make_docx()
    doc = Document(src)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(src)
    out = tmp_path / "located-insert.docx"
    result = insert_paragraph(src, "Repeated", "Chosen", position="before", output_path=out, location=(3,))
    assert result.success
    assert [p.text for p in Document(out).paragraphs[2:]] == ["Repeated", "Chosen", "Repeated"]


def test_insert_location_must_match_target_text(make_docx, tmp_path):
    """Checks insertion refuses a location whose paragraph text differs."""
    src = make_docx()
    out = tmp_path / "wrong-located-insert.docx"
    result = insert_paragraph(src, "Unrelated paragraph", "New", output_path=out, location=(0,))
    assert not result.success and "requested text" in result.error and "location" in result.error
    assert not out.exists()


def test_remove_paragraph_and_preserve_neighbors_and_image(make_docx, tmp_path):
    """Checks safe removal preserves adjacent paragraphs and an unrelated image."""
    src = make_docx(with_image=True)
    out = tmp_path / "remove.docx"
    result = remove_paragraph(src, "Unrelated paragraph", output_path=out)
    assert result.success and result.affected_elements == 1
    reopened = Document(out)
    assert [p.text for p in reopened.paragraphs[:2]] == ["Keep old text end", ""]
    assert len(reopened.inline_shapes) == 1


def test_remove_paragraph_in_specified_table_cell(make_docx, tmp_path):
    """Checks an explicitly located cell paragraph is removed while its neighbor stays."""
    src = make_docx()
    doc = Document(src)
    cell = doc.tables[0].cell(0, 0)
    cell.add_paragraph("Keep this cell paragraph")
    doc.save(src)
    out = tmp_path / "remove-cell.docx"
    result = remove_paragraph(src, "Cell target value", output_path=out, location=(0, 0, 0, 0))
    assert result.success
    reopened = Document(out)
    assert [p.text for p in reopened.tables[0].cell(0, 0).paragraphs] == ["Keep this cell paragraph"]


def test_remove_ambiguous_text_requires_location(make_docx, tmp_path):
    """Checks duplicate removal matches require an explicit paragraph location."""
    src = make_docx()
    doc = Document(src)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(src)
    out = tmp_path / "ambiguous-remove.docx"
    result = remove_paragraph(src, "Repeated", output_path=out)
    assert not result.success and "ambiguous" in result.error
    assert not out.exists()


def test_remove_duplicate_by_explicit_body_location(make_docx, tmp_path):
    """Checks a body index removes only the selected duplicate paragraph."""
    src = make_docx()
    doc = Document(src)
    doc.add_paragraph("Repeated")
    doc.add_paragraph("Repeated")
    doc.save(src)
    out = tmp_path / "located-remove.docx"
    result = remove_paragraph(src, "Repeated", output_path=out, location=(2,))
    assert result.success
    assert [p.text for p in Document(out).paragraphs] == ["Keep old text end", "Unrelated paragraph", "Repeated"]


def test_remove_refuses_paragraph_with_image(make_docx, tmp_path):
    """Checks removal refuses image-containing paragraphs and creates no output."""
    src = make_docx(with_image=True)
    doc = Document(src)
    p = doc.add_paragraph("Image caption")
    p.add_run().add_picture(str(tmp_path / "tiny.png"))
    doc.save(src)
    out = tmp_path / "unsafe-remove.docx"
    result = remove_paragraph(src, "Image caption", output_path=out)
    assert not result.success and "non-text" in result.error
    assert not out.exists()
    assert len(Document(src).inline_shapes) == 2


def test_remove_refuses_heading(make_docx, tmp_path):
    """Checks heading-style paragraphs are protected from accidental removal."""
    src = make_docx()
    out = tmp_path / "heading-remove.docx"
    result = remove_paragraph(src, "Keep old text end", output_path=out)
    assert not result.success and "heading" in result.error
    assert not out.exists()


def test_operation_results_share_status_fields(make_docx, tmp_path):
    """Checks replace, insert, and remove failures share common status fields."""
    src = make_docx()
    replacement = replace_text(src, "absent", "x", tmp_path / "none.docx")
    insertion = insert_paragraph(src, "absent", "x", output_path=tmp_path / "none2.docx")
    removal = remove_paragraph(src, "absent", output_path=tmp_path / "none3.docx")
    for result in (replacement, insertion, removal):
        assert result.operation_type
        assert result.success is False
        assert result.affected_elements == 0
        assert result.error
