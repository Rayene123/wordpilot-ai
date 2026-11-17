"""Explicit planning and human-approved execution entry points."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import TypeAdapter, ValidationError

from .graph import _execute_node, _parse_tool_result
from .planner import OperationPlanner, PlanningError, PlanningModel
from .schemas import DocumentOperation
from .tools import create_document_tools

_OPERATIONS = TypeAdapter(list[DocumentOperation])
_PROPOSAL_VERSION = 1
_PROPOSALS: dict[str, str] = {}
_PROPOSALS_LOCK = threading.Lock()


def plan_document(
    document_path: str | Path,
    instruction: str,
    *,
    planner: OperationPlanner | None = None,
    provider: PlanningModel | None = None,
) -> dict[str, Any]:
    """Inspect a document and return a proposal without creating edited output.

    A returned ``awaiting_approval`` proposal includes its source path,
    instruction, validated operations, and integrity digest. Clarification,
    unsupported, and failure results never contain executable operations.
    Inject ``planner`` or ``provider`` to keep tests and hosts API-independent.
    """
    try:
        source = Path(document_path).expanduser().resolve()
        source_fingerprint = _file_fingerprint(source)
        tools = {tool.name: tool for tool in create_document_tools(source)}
        summary = _parse_tool_result(tools["inspect_document"].invoke({}))
        if _file_fingerprint(source) != source_fingerprint:
            raise ValueError("Source document changed while it was being inspected; please plan again.")
        active_planner = planner or OperationPlanner(provider)
        planned = active_planner.plan(instruction, summary)
        if planned.status == "clarification_required":
            return _planning_result("needs_clarification", source, instruction, summary,
                                    [], planned.message)
        if planned.status == "unsupported":
            return _planning_result("unsupported", source, instruction, summary,
                                    [], planned.message)
        validated = _OPERATIONS.validate_python(planned.operations)
        operations = [operation.model_dump(mode="json") for operation in validated]
        proposal = _planning_result("awaiting_approval", source, instruction, summary,
                                    operations, planned.message)
        proposal["source_fingerprint"] = source_fingerprint
        proposal["proposal_digest"] = _register_proposal(proposal)
        return proposal
    except Exception as exc:
        return {
            "planning_status": "failed",
            "approval_status": "pending",
            "execution_status": "not_executed",
            "document_path": str(document_path),
            "user_instruction": instruction,
            "document_summary": None,
            "proposed_operations": [],
            "validated_operations": [],
            "approved_operations": [],
            "clarification_request": None,
            "execution_results": [],
            "warnings": [],
            "errors": [str(exc)],
            "output_path": None,
            "final_response": f"Planning failed. {exc}",
        }


def execute_approved_proposal(
    proposal: Mapping[str, Any],
    *,
    approved: bool | None = None,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Reject, defer, or execute a proposal after an explicit approval decision.

    Only the exact proposal returned by :func:`plan_document` can be approved:
    its opaque one-time token and complete contents are checked before operation validation. Approved operations are
    validated again and executed through the existing fixed DOCX tools. Output
    is always written to a unique file in the host-selected directory.
    """
    if not isinstance(proposal, Mapping):
        return _execution_failure(_empty_execution_result(), "Proposal must be a mapping returned by plan_document.")
    result = _execution_base(proposal)
    if approved is None:
        result.update({
            "approval_status": "pending",
            "execution_status": "not_executed",
            "final_response": "Approval was not supplied; no document was edited.",
        })
        return result
    if approved is False:
        result.update({
            "approval_status": "rejected",
            "execution_status": "not_executed",
            "final_response": "The proposal was rejected; no document was edited.",
        })
        return result
    if approved is not True:
        return _execution_failure(result, "Approval must be explicitly true or false.")
    if proposal.get("planning_status") in {"needs_clarification", "unsupported"}:
        result.update({
            "execution_status": "not_executed",
            "final_response": "This request cannot be executed until it is clarified or supported.",
        })
        return result
    if proposal.get("planning_status") != "awaiting_approval":
        return _execution_failure(result, "This proposal is not awaiting approval.")
    expected_digest = proposal.get("proposal_digest")
    if not isinstance(expected_digest, str) or not _proposal_is_unchanged(proposal, expected_digest):
        return _execution_failure(result, "Proposal content changed after it was presented for approval.")
    try:
        operations = _OPERATIONS.validate_python(proposal.get("proposed_operations"))
        if not operations:
            raise ValueError("At least one approved operation is required.")
        output_dir = Path(output_directory).expanduser().resolve()
        if not output_dir.exists() or not output_dir.is_dir():
            raise ValueError(f"Output directory must already exist: {output_dir}")
        source = Path(proposal["document_path"]).expanduser().resolve()
        approved_ops = [item.model_dump(mode="json") for item in operations]
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return _execution_failure(result, f"Approved proposal is invalid: {exc}")

    try:
        if _file_fingerprint(source) != proposal.get("source_fingerprint"):
            return _execution_failure(result, "Source document changed after planning; please plan again.")
    except OSError as exc:
        return _execution_failure(result, f"Could not verify source document identity: {exc}")

    # Consume the approval token before the first edit so the same approval
    # cannot be replayed to create another output document.
    with _PROPOSALS_LOCK:
        if _PROPOSALS.pop(expected_digest, None) is None:
            return _execution_failure(result, "Proposal token is unknown or has already been used.")

    execution_state = {
        "document_path": str(source),
        "validated_operations": approved_ops,
        "errors": [],
        "warnings": [],
    }
    execution = _execute_node(execution_state, output_dir)
    status = execution["workflow_status"]
    result.update({
        "approval_status": "approved",
        "execution_status": status,
        "approved_operations": approved_ops,
        "execution_results": execution["execution_results"],
        "warnings": execution["warnings"],
        "errors": execution["errors"],
        "output_path": execution["output_path"],
        "final_response": _execution_message(status, execution),
    })
    return result


def _planning_result(
    status: Literal["awaiting_approval", "needs_clarification", "unsupported"],
    source: Path,
    instruction: str,
    summary: dict[str, Any],
    operations: list[dict[str, Any]],
    message: str,
) -> dict[str, Any]:
    """Build the serializable planning result shared by all planner outcomes."""
    return {
        "planning_status": status,
        "approval_status": "pending",
        "execution_status": "not_executed",
        "document_path": str(source),
        "user_instruction": instruction,
        "document_summary": summary,
        "proposed_operations": operations,
        "validated_operations": operations,
        "approved_operations": [],
        "clarification_request": message if status == "needs_clarification" else None,
        "execution_results": [],
        "warnings": list(summary.get("warnings", [])),
        "errors": [],
        "output_path": None,
        "final_response": message,
        "proposal_version": _PROPOSAL_VERSION,
    }


def _register_proposal(proposal: Mapping[str, Any]) -> str:
    """Register the exact serialized proposal under an unpredictable process token.

    Unlike a plain hash or exposed signing helper, a caller cannot mint a
    registry entry by editing proposal fields or recomputing a digest.
    """
    canonical = _canonical_proposal(proposal)
    token = secrets.token_urlsafe(32)
    with _PROPOSALS_LOCK:
        _PROPOSALS[token] = canonical
    return token


def _proposal_is_unchanged(proposal: Mapping[str, Any], token: str) -> bool:
    """Check exact proposal contents against the process-local issued record."""
    try:
        candidate = _canonical_proposal({key: value for key, value in proposal.items() if key != "proposal_digest"})
    except (TypeError, ValueError):
        return False
    with _PROPOSALS_LOCK:
        original = _PROPOSALS.get(token)
    return original is not None and hmac.compare_digest(candidate, original)


def _canonical_proposal(proposal: Mapping[str, Any]) -> str:
    """Serialize every proposal field deterministically for exact comparison."""
    content = {key: value for key, value in proposal.items() if key != "proposal_digest"}
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _file_fingerprint(path: Path) -> str:
    """Return SHA-256 of a file using bounded-memory reads."""
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execution_base(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Initialize a structured execution response from untrusted proposal data."""
    return {
        "planning_status": proposal.get("planning_status", "failed"),
        "approval_status": "pending",
        "execution_status": "pending",
        "document_path": proposal.get("document_path"),
        "user_instruction": proposal.get("user_instruction"),
        "document_summary": proposal.get("document_summary"),
        "proposed_operations": proposal.get("proposed_operations", []),
        "validated_operations": [],
        "approved_operations": [],
        "clarification_request": proposal.get("clarification_request"),
        "execution_results": [],
        "warnings": [],
        "errors": [],
        "output_path": None,
        "final_response": None,
    }


def _empty_execution_result() -> dict[str, Any]:
    """Return the standard response fields when the proposal itself is malformed."""
    return {
        "planning_status": "failed",
        "approval_status": "pending",
        "execution_status": "pending",
        "document_path": None,
        "user_instruction": None,
        "document_summary": None,
        "proposed_operations": [],
        "validated_operations": [],
        "approved_operations": [],
        "clarification_request": None,
        "execution_results": [],
        "warnings": [],
        "errors": [],
        "output_path": None,
        "final_response": None,
    }


def _execution_failure(result: dict[str, Any], error: str) -> dict[str, Any]:
    """Mark an invalid approval attempt failed without invoking document tools."""
    result.update({
        "approval_status": "pending",
        "execution_status": "failed",
        "errors": [error],
        "final_response": f"Execution was blocked. {error}",
    })
    return result


def _execution_message(status: str, execution: Mapping[str, Any]) -> str:
    """Create the concise final response for completed, partial, or failed edits."""
    if status == "completed":
        return f"Completed {len(execution['execution_results'])} document operation(s). Output: {execution['output_path']}"
    if status == "partial":
        return f"Stopped after a failure. The original is unchanged; partial output: {execution['output_path']}."
    errors = " ".join(execution.get("errors", []))
    return "Document execution failed. " + (errors or "No output was created.")
