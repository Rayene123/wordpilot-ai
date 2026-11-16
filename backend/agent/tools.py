"""LangChain tool adapters for the existing DOCX editor functions."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict

from backend.docx_editor import (
    insert_paragraph as editor_insert_paragraph,
    inspect_document as editor_inspect_document,
    remove_paragraph as editor_remove_paragraph,
    replace_text as editor_replace_text,
)
from .schemas import InsertOperation, RemoveOperation, ReplaceOperation


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_docx_path(path: Path, *, must_exist: bool) -> str | None:
    """Check DOCX extension and required input file or output directory."""
    if path.suffix.lower() != ".docx":
        return "Document paths must use the .docx extension."
    if must_exist and not path.exists():
        return f"Document does not exist: {path}"
    if must_exist and not path.is_file():
        return f"Document path is not a file: {path}"
    if not must_exist and (not path.parent.exists() or not path.parent.is_dir()):
        return f"Output directory does not exist: {path.parent}"
    if not must_exist and path.exists() and not path.is_file():
        return f"Output path is not a file: {path}"
    return None


def _json_result(value: Any) -> str:
    """Serialize a dataclass, summary, or mapping as JSON tool output."""
    if is_dataclass(value):
        value = asdict(value)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return json.dumps(value, ensure_ascii=False)


def _error_result(operation: str, error: str) -> str:
    """Format an adapter error with the same JSON fields as operation results."""
    return json.dumps({
        "operation_type": operation,
        "success": False,
        "affected_elements": 0,
        "warnings": [],
        "error": error,
        "output_path": None,
    }, ensure_ascii=False)


def create_document_tools(
    document_path: str | Path,
    output_path: str | Path | None = None,
) -> list[BaseTool]:
    """Create four fixed tools bound to a host-selected document path.

    The model receives no path or function-name argument. The optional output
    path is also selected by the host, validated here, and never model supplied.
    """
    source = Path(document_path).expanduser().resolve()
    source_error = _validate_docx_path(source, must_exist=True)
    if source_error:
        raise ValueError(source_error)
    destination = Path(output_path).expanduser().resolve() if output_path is not None else None
    if destination is not None:
        output_error = _validate_docx_path(destination, must_exist=False)
        if output_error:
            raise ValueError(output_error)
    active_path = source

    def check_bound_paths(operation: str) -> str | None:
        """Recheck the host-bound input and output before each tool call."""
        error = _validate_docx_path(active_path, must_exist=True)
        if error:
            return _error_result(operation, error)
        if destination is not None:
            error = _validate_docx_path(destination, must_exist=False)
            if error:
                return _error_result(operation, error)
        return None

    @tool("inspect_document", args_schema=_NoArguments)
    def inspect_tool() -> str:
        """Inspect the bound DOCX and return its structured summary as JSON."""
        error = check_bound_paths("inspect")
        if error:
            return error
        try:
            return _json_result(editor_inspect_document(active_path))
        except (OSError, ValueError) as exc:
            return _error_result("inspect", str(exc))

    @tool("replace_text", args_schema=ReplaceOperation)
    def replace_tool(
        operation_type: Literal["replace"], target_text: str, replacement_text: str,
    ) -> str:
        """Replace exact text in the bound DOCX."""
        nonlocal active_path
        error = check_bound_paths("replace")
        if error:
            return error
        try:
            args = ReplaceOperation.model_validate({
                "operation_type": operation_type,
                "target_text": target_text,
                "replacement_text": replacement_text,
            })
            result = editor_replace_text(active_path, args.target_text, args.replacement_text, destination)
            if result.success and destination is not None:
                active_path = destination
            return _json_result(result)
        except (OSError, ValueError) as exc:
            return _error_result("replace", str(exc))

    @tool("insert_paragraph", args_schema=InsertOperation)
    def insert_tool(
        operation_type: Literal["insert"], anchor_paragraph_text: str,
        inserted_text: str, position: Literal["before", "after"],
        paragraph_style: str | None = None, location: tuple[int, ...] | None = None,
    ) -> str:
        """Insert a paragraph around an exact anchor in the bound DOCX."""
        nonlocal active_path
        error = check_bound_paths("insert_paragraph")
        if error:
            return error
        try:
            args = InsertOperation.model_validate({
                "operation_type": operation_type,
                "anchor_paragraph_text": anchor_paragraph_text,
                "inserted_text": inserted_text,
                "position": position,
                "paragraph_style": paragraph_style,
                "location": location,
            })
            result = editor_insert_paragraph(
                active_path, args.anchor_paragraph_text, args.inserted_text,
                args.position, args.paragraph_style, destination, args.location,
            )
            if result.success and destination is not None:
                active_path = destination
            return _json_result(result)
        except (OSError, ValueError) as exc:
            return _error_result("insert_paragraph", str(exc))

    @tool("remove_paragraph", args_schema=RemoveOperation)
    def remove_tool(
        operation_type: Literal["remove"], target_text: str,
        location: tuple[int, ...] | None = None,
    ) -> str:
        """Remove one exact-text paragraph from the bound DOCX."""
        nonlocal active_path
        error = check_bound_paths("remove_paragraph")
        if error:
            return error
        try:
            args = RemoveOperation.model_validate({
                "operation_type": operation_type,
                "target_text": target_text,
                "location": location,
            })
            result = editor_remove_paragraph(active_path, args.target_text, destination, args.location)
            if result.success and destination is not None:
                active_path = destination
            return _json_result(result)
        except (OSError, ValueError) as exc:
            return _error_result("remove_paragraph", str(exc))

    return [inspect_tool, replace_tool, insert_tool, remove_tool]
