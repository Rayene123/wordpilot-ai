"""First executable LangGraph workflow for controlled DOCX operations."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from .schemas import DocumentOperation, InsertOperation, RemoveOperation, ReplaceOperation
from .state import DocumentAgentState
from .tools import create_document_tools

_OPERATION_ADAPTER = TypeAdapter(DocumentOperation)


def _operation_tool_name(operation: dict[str, Any]) -> str:
    """Map a validated operation type to its one allowed document tool."""
    return {
        "replace": "replace_text",
        "insert": "insert_paragraph",
        "remove": "remove_paragraph",
    }[operation["operation_type"]]


def _parse_tool_result(value: Any) -> dict[str, Any]:
    """Convert a tool's JSON text or mapping result into a plain dictionary."""
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    if isinstance(value, dict):
        return value
    raise ValueError("Document tool returned an unsupported result format.")


def _inspect_node(state: DocumentAgentState) -> dict[str, Any]:
    """Inspect the input DOCX and initialize serializable workflow fields."""
    path = state.get("document_path", "")
    defaults = {
        "document_summary": None,
        "validated_operations": [],
        "validation_errors": [],
        "errors": [],
        "execution_results": [],
        "warnings": [],
        "final_response": None,
        "output_path": None,
    }
    try:
        tools = {item.name: item for item in create_document_tools(path)}
        result = _parse_tool_result(tools["inspect_document"].invoke({}))
    except Exception as exc:
        return {
            **defaults,
            "errors": [f"Document inspection failed: {exc}"],
            "workflow_status": "failed",
        }
    if result.get("success") is False:
        return {
            **defaults,
            "errors": [f"Document inspection failed: {result.get('error', 'Unknown inspection error.') }"],
            "warnings": result.get("warnings", []),
            "workflow_status": "failed",
        }
    return {
        **defaults,
        "document_summary": result,
        "warnings": result.get("warnings", []),
        "workflow_status": "inspected",
    }


def _validate_node(state: DocumentAgentState) -> dict[str, Any]:
    """Validate every proposed edit before allowing any operation to execute."""
    raw_operations = state.get("proposed_operations", [])
    errors: list[str] = []
    validated: list[dict[str, Any]] = []
    if not raw_operations:
        errors.append("At least one proposed operation is required.")
    for index, operation in enumerate(raw_operations):
        try:
            # Pydantic's TypeAdapter validates the discriminated union as one schema.
            validated_operation = _OPERATION_ADAPTER.validate_python(operation)
            validated.append(validated_operation.model_dump(mode="json"))
        except Exception as exc:
            errors.append(f"Operation {index + 1} is invalid: {exc}")
    return {
        "validated_operations": validated if not errors else [],
        "validation_errors": errors,
        "workflow_status": "failed" if errors else "validated",
    }


def _execute_node(state: DocumentAgentState, output_directory: Path) -> dict[str, Any]:
    """Run validated edits in order, saving to a unique output and stopping on failure."""
    source = Path(state["document_path"]).expanduser().resolve()
    destination = output_directory / f"{source.stem}_edited_{uuid.uuid4().hex}.docx"
    while destination.resolve() == source:
        destination = output_directory / f"{source.stem}_edited_{uuid.uuid4().hex}.docx"
    try:
        tools = {item.name: item for item in create_document_tools(source, destination)}
    except Exception as exc:
        return {
            "execution_results": [],
            "errors": list(state.get("errors", [])) + [f"Could not prepare document tools: {exc}"],
            "output_path": None,
            "workflow_status": "failed",
        }
    results: list[dict[str, Any]] = []
    warnings = list(state.get("warnings", []))
    errors = list(state.get("errors", []))
    successful_operations = 0
    failed = False

    for index, operation in enumerate(state.get("validated_operations", [])):
        tool_name = _operation_tool_name(operation)
        try:
            result = _parse_tool_result(tools[tool_name].invoke(operation))
        except Exception as exc:
            result = {
                "operation_type": operation["operation_type"],
                "success": False,
                "affected_elements": 0,
                "warnings": [],
                "error": str(exc),
                "output_path": None,
            }
        result["operation_index"] = index
        result["operation"] = operation
        results.append(result)
        warnings.extend(result.get("warnings", []))
        if not result.get("success", False):
            errors.append(f"Operation {index + 1} failed: {result.get('error', 'Unknown operation error.')}")
            failed = True
            break
        successful_operations += 1

    output_exists = destination.is_file() and successful_operations > 0
    if failed:
        status = "partial" if successful_operations else "failed"
    else:
        status = "completed"
    return {
        "execution_results": results,
        "warnings": warnings,
        "errors": errors,
        "output_path": str(destination) if output_exists else None,
        "workflow_status": status,
    }


def _finalize_node(state: DocumentAgentState) -> dict[str, Any]:
    """Build the user-facing response from final status, errors, and warnings."""
    status = state.get("workflow_status", "failed")
    output_path = state.get("output_path")
    validation_errors = state.get("validation_errors", [])
    errors = state.get("errors", [])
    results = state.get("execution_results", [])
    warnings = state.get("warnings", [])

    if status == "completed":
        response = f"Completed {len(results)} document operation(s). Output: {output_path}"
    elif status == "partial":
        response = (
            f"Stopped after {sum(bool(item.get('success')) for item in results)} successful operation(s). "
            f"The original is unchanged; partial output: {output_path}."
        )
    else:
        details = validation_errors + errors
        response = "Document workflow failed. " + (" ".join(details) if details else "No output was created.")
    if warnings:
        response += " Warnings: " + " ".join(warnings)
    return {"final_response": response}


def _route_after_inspection(state: DocumentAgentState) -> Literal["validate_operations", "finalize"]:
    """Send failed inspections to finalization and valid documents to validation."""
    return "finalize" if state.get("workflow_status") == "failed" else "validate_operations"


def _route_after_validation(state: DocumentAgentState) -> Literal["execute_operations", "finalize"]:
    """Skip execution on any validation error; otherwise continue to editing."""
    return "finalize" if state.get("validation_errors") else "execute_operations"


def build_document_graph(output_directory: str | Path):
    """Build the compiled graph using a host-controlled output directory.

    Invoke with a state mapping containing ``document_path``,
    ``user_instruction``, and ``proposed_operations``. The output filename is
    generated by the graph and never comes from model-provided state.
    """
    output_dir = Path(output_directory).expanduser().resolve()
    if not output_dir.exists() or not output_dir.is_dir():
        raise ValueError(f"Output directory must already exist: {output_dir}")

    graph = StateGraph(DocumentAgentState)
    graph.add_node("inspect_document", _inspect_node)
    graph.add_node("validate_operations", _validate_node)
    graph.add_node("execute_operations", lambda state: _execute_node(state, output_dir))
    graph.add_node("finalize", _finalize_node)
    graph.add_edge(START, "inspect_document")
    graph.add_conditional_edges(
        "inspect_document",
        _route_after_inspection,
        {"validate_operations": "validate_operations", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "validate_operations",
        _route_after_validation,
        {"execute_operations": "execute_operations", "finalize": "finalize"},
    )
    graph.add_edge("execute_operations", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
