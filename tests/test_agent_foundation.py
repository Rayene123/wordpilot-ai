from __future__ import annotations

import json

import pytest
from docx import Document
from langchain_core.tools import BaseTool
from pydantic import TypeAdapter, ValidationError

from backend.agent import (
    DocumentAgentState,
    DocumentOperation,
    InsertOperation,
    RemoveOperation,
    ReplaceOperation,
    create_document_tools,
)


def _tool_map(path, output_path=None) -> dict[str, BaseTool]:
    """Index the fixed document tools by name for concise test invocation."""
    return {item.name: item for item in create_document_tools(path, output_path)}


def test_valid_operation_schemas():
    """Checks valid replace, insert, and remove inputs become typed schemas."""
    replace = ReplaceOperation(operation_type="replace", target_text="old", replacement_text="new")
    insert = InsertOperation(
        operation_type="insert", anchor_paragraph_text="Anchor", inserted_text="Added", position="after"
    )
    remove = RemoveOperation(operation_type="remove", target_text="Delete", location=(0,))
    assert replace.target_text == "old"
    assert insert.position == "after"
    assert remove.location == (0,)
    json_location = RemoveOperation.model_validate({
        "operation_type": "remove", "target_text": "Delete", "location": [1, 2, 3, 0],
    })
    assert json_location.location == (1, 2, 3, 0)


@pytest.mark.parametrize("payload", [
    {"operation_type": "replace", "target_text": "", "replacement_text": "x"},
    {"operation_type": "replace", "target_text": "x"},
    {"operation_type": "insert", "anchor_paragraph_text": "a", "inserted_text": "b", "position": "sideways"},
    {"operation_type": "insert", "anchor_paragraph_text": "a", "inserted_text": "b"},
    {"operation_type": "remove", "target_text": "x", "location": [1, 2]},
    {"operation_type": "remove", "target_text": "x", "function_name": "os.remove"},
])
def test_invalid_operation_schemas_rejected(payload):
    """Checks malformed and unknown operations fail discriminated-union parsing."""
    with pytest.raises(ValidationError):
        TypeAdapter(DocumentOperation).validate_python(payload)


def test_union_parsing_uses_operation_discriminator():
    """Checks the operation type selects the matching Pydantic model."""
    parsed = TypeAdapter(DocumentOperation).validate_python({
        "operation_type": "insert",
        "anchor_paragraph_text": "Anchor",
        "inserted_text": "New text",
        "position": "before",
        "paragraph_style": "Normal",
    })
    assert isinstance(parsed, InsertOperation)


def test_tools_are_fixed_and_do_not_expose_paths(make_docx):
    """Checks only four named tools exist and none accepts filesystem paths."""
    src = make_docx()
    tools = _tool_map(src)
    assert set(tools) == {"inspect_document", "replace_text", "insert_paragraph", "remove_paragraph"}
    for item in tools.values():
        assert "document_path" not in item.args
        assert "output_path" not in item.args


@pytest.mark.parametrize("path", ["missing.docx", "wrong-extension.txt"])
def test_tool_factory_rejects_invalid_document_paths(tmp_path, path):
    """Checks missing files and non-DOCX extensions are rejected at setup."""
    with pytest.raises(ValueError):
        create_document_tools(tmp_path / path)


def test_tool_argument_validation_rejects_empty_target(make_docx):
    """Checks the replace tool schema rejects an empty target before execution."""
    tool = _tool_map(make_docx())["replace_text"]
    with pytest.raises(ValidationError):
        tool.invoke({"operation_type": "replace", "target_text": "", "replacement_text": "x"})


def test_tool_adapters_call_existing_document_operations(make_docx, tmp_path):
    """Checks each adapter calls its editor operation and writes valid DOCX output."""
    src = make_docx()
    out = tmp_path / "tool-output.docx"
    tools = _tool_map(src, out)

    inspected = json.loads(tools["inspect_document"].invoke({}))
    assert inspected["paragraphs"][1]["text"] == "Unrelated paragraph"

    replaced = json.loads(tools["replace_text"].invoke({
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Changed",
    }))
    assert replaced["success"] is True
    assert replaced["affected_elements"] == 1
    assert Document(out).paragraphs[1].text == "Changed paragraph"

    inserted = json.loads(tools["insert_paragraph"].invoke({
        "operation_type": "insert", "anchor_paragraph_text": "Changed paragraph",
        "inserted_text": "Inserted", "position": "after",
    }))
    assert inserted["success"] is True
    assert Document(out).paragraphs[2].text == "Inserted"

    # Build a fresh source because the tool adapter consistently reads its bound input.
    removal_source = make_docx("remove-source.docx")
    removal_tools = _tool_map(removal_source, tmp_path / "remove-output.docx")
    removed = json.loads(removal_tools["remove_paragraph"].invoke({
        "operation_type": "remove", "target_text": "Unrelated paragraph",
    }))
    assert removed["success"] is True
    assert "Unrelated paragraph" not in [p.text for p in Document(tmp_path / "remove-output.docx").paragraphs]


def test_missing_target_is_returned_as_serializable_error(make_docx, tmp_path):
    """Checks a missing match becomes a JSON error result rather than an exception."""
    tools = _tool_map(make_docx(), tmp_path / "missing-target-output.docx")
    result = json.loads(tools["replace_text"].invoke({
        "operation_type": "replace", "target_text": "not present", "replacement_text": "x",
    }))
    assert result["success"] is False
    assert result["affected_elements"] == 0
    assert "not found" in result["error"]


def test_tool_rejects_unrecognized_arguments(make_docx):
    """Checks tool schemas reject extra inputs such as a model-selected path."""
    tool = _tool_map(make_docx())["replace_text"]
    with pytest.raises(ValidationError):
        tool.invoke({
            "operation_type": "replace", "target_text": "x", "replacement_text": "y",
            "document_path": "C:/untrusted.docx",
        })


def test_agent_state_is_json_serializable(make_docx):
    """Checks graph state contains values that can be encoded and decoded as JSON."""
    state: DocumentAgentState = {
        "document_path": str(make_docx()),
        "user_instruction": "Replace old with new",
        "document_summary": None,
        "proposed_operations": [{"operation_type": "replace", "target_text": "old", "replacement_text": "new"}],
        "validation_errors": [],
        "execution_results": [],
        "warnings": [],
        "final_response": None,
        "workflow_status": "pending",
    }
    encoded = json.dumps(state)
    assert json.loads(encoded)["workflow_status"] == "pending"
