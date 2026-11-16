from __future__ import annotations

import base64

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches


@pytest.fixture
def make_docx(tmp_path):
    """Build small synthetic documents for editing tests."""
    def make(name="source.docx", with_image=False):
        """Create a formatted sample DOCX, optionally adding a tiny picture."""
        path = tmp_path / name
        doc = Document()
        p = doc.add_paragraph(style="Title")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("Keep ").bold = True
        p.add_run("old ").italic = True
        p.add_run("text end")
        doc.add_paragraph("Unrelated paragraph")
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).paragraphs[0].add_run("Cell target value")
        if with_image:
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4k0AAAAASUVORK5CYII="
            )
            image_path = tmp_path / "tiny.png"
            image_path.write_bytes(png)
            doc.add_paragraph().add_run().add_picture(str(image_path), width=Inches(0.1))
        doc.save(path)
        return path
    return make
