"""Save controlled body edits without rewriting unrelated DOCX package parts."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

from docx.document import Document as DocumentObject


def save_preserving_package_parts(
    document: DocumentObject,
    source_path: str | Path,
    destination_path: str | Path,
) -> None:
    """Save edited document XML while retaining every other source package part.

    Supported editor operations modify only body paragraphs. python-docx
    serializes every part when saving, which can change unrelated package XML,
    relationships, or metadata. Use its edited ``word/document.xml`` and copy
    all other members directly from the current source package.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    temporary_paths: list[Path] = []

    def make_temporary_path() -> Path:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp.docx",
        )
        os.close(descriptor)
        path = Path(name)
        temporary_paths.append(path)
        return path

    try:
        serialized_path = make_temporary_path()
        package_path = make_temporary_path()
        document.save(str(serialized_path))

        with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(serialized_path, "r") as serialized:
            original_infos = original.infolist()
            original_names = [info.filename for info in original_infos]
            serialized_names = serialized.namelist()
            if len(original_names) != len(set(original_names)):
                raise ValueError("DOCX package contains duplicate component names.")
            if len(serialized_names) != len(set(serialized_names)):
                raise ValueError("Serialized DOCX package contains duplicate component names.")
            if set(original_names) != set(serialized_names):
                raise ValueError("Document editing unexpectedly changed the DOCX package component set.")
            if "word/document.xml" not in original_names:
                raise ValueError("DOCX package is missing word/document.xml.")

            with zipfile.ZipFile(package_path, "w") as output:
                output.comment = original.comment
                for info in original_infos:
                    content = (
                        serialized.read("word/document.xml")
                        if info.filename == "word/document.xml"
                        else original.read(info.filename)
                    )
                    output.writestr(info, content)

        os.replace(package_path, destination)
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
