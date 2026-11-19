"""Explicit planning and human-approved execution entry points."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import traceback
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import TypeAdapter, ValidationError

from backend.docx_editor.integrity import verify_document_integrity

from backend.docx_editor.inspection import inspect_document_with_planner_context

from .graph import build_document_graph
from .planner import OperationPlanner, PlanningContextTooLarge, PlanningModel
from .schemas import DocumentOperation

_OPERATIONS = TypeAdapter(list[DocumentOperation])
_PROPOSAL_VERSION = 1
_PROPOSALS: dict[str, str] = {}
_PROPOSALS_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)


def _safe_error_message(exc: Exception) -> str:
    """Redact credentials from exception messages before writing server logs."""
    message = str(exc)
    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", message)
    return message


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
        inspected, planner_context = inspect_document_with_planner_context(source)
        # Match the JSON-normalized shape previously returned by the inspect tool.
        summary = json.loads(json.dumps(inspected.to_dict(), ensure_ascii=False))
        if _file_fingerprint(source) != source_fingerprint:
            raise ValueError("Source document changed while it was being inspected; please plan again.")
        active_planner = planner or OperationPlanner(provider)
        if planner is not None and not isinstance(planner, OperationPlanner):
            # Preserve the existing injectable planner interface for hosts/tests.
            planned = active_planner.plan(instruction, summary)
        else:
            planned = active_planner.plan(instruction, summary, planner_context=planner_context)
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
    except PlanningContextTooLarge as exc:
        return {
            "planning_status": "too_large",
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
            "final_response": str(exc),
        }
    except Exception as exc:
        _logger.error(
            "Document planning failed: type=%s message=%s\n%s",
            type(exc).__name__,
            _safe_error_message(exc),
            "".join(traceback.format_tb(exc.__traceback__)),
        )
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
    its opaque one-time token and complete contents are checked before operation
    validation. Approved operations are validated again and executed through the
    existing fixed DOCX tools. A completed output is then checked against the
    source and those exact validated operations. Output is always written to a
    unique file in the host-selected directory.
    """
    if not isinstance(proposal, Mapping):
        return _execution_failure(_empty_execution_result(), "Proposal must be a mapping returned by plan_document.")
    result = _execution_base(proposal)
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
    if approved is None:
        result.update({
            "approval_status": "pending",
            "execution_status": "pending",
            "final_response": "Approval was not supplied; no document was edited.",
        })
        return result
    if approved is False:
        result.update({
            "approval_status": "rejected",
            "execution_status": "rejected",
            "final_response": "The proposal was rejected; no document was edited.",
        })
        return result
    if approved is not True:
        return _execution_failure(result, "Approval must be explicitly true or false.")
    try:
        source = Path(proposal["document_path"]).expanduser().resolve()
        if _file_fingerprint(source) != proposal.get("source_fingerprint"):
            return _execution_failure(result, "Source document changed after planning; please plan again.")
    except (KeyError, TypeError, OSError, ValueError) as exc:
        return _execution_failure(result, f"Could not verify source document identity: {exc}")

    try:
        operations = _OPERATIONS.validate_python(proposal.get("proposed_operations"))
        if not operations:
            raise ValueError("At least one approved operation is required.")
        output_dir = Path(output_directory).expanduser().resolve()
        if not output_dir.exists() or not output_dir.is_dir():
            raise ValueError(f"Output directory must already exist: {output_dir}")
        approved_ops = [item.model_dump(mode="json") for item in operations]
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return _execution_failure(result, f"Approved proposal is invalid: {exc}")

    # Consume the approval token before the first edit so the same approval
    # cannot be replayed to create another output document.
    with _PROPOSALS_LOCK:
        if _PROPOSALS.pop(expected_digest, None) is None:
            return _execution_failure(result, "Proposal token is unknown or has already been used.")

    execution_state = {
        "document_path": str(source), "user_instruction": str(proposal.get("user_instruction", "")),
        "proposed_operations": approved_ops, "source_fingerprint": proposal.get("source_fingerprint"),
    }
    try:
        execution = build_document_graph(output_dir, verifier=verify_document_integrity).invoke(execution_state)
    except Exception as exc:
        _logger.error("Approved document graph failed: type=%s message=%s", type(exc).__name__, _safe_error_message(exc))
        result.update({"approval_status": "approved", "execution_status": "execution_failed", "integrity_status": "not_run",
                       "errors": [f"Document execution failed ({type(exc).__name__})."],
                       "final_response": "Document execution failed. No verified output is available."})
        return result
    result.update({"approval_status": "approved", "approved_operations": approved_ops,
                   "validated_operations": execution.get("validated_operations", []),
                   "execution_results": execution.get("execution_results", []), "warnings": execution.get("warnings", []),
                   "errors": execution.get("errors", []), "integrity_report": execution.get("integrity_report"),
                   "integrity_status": execution.get("integrity_status", "not_run"),
                   "output_path": execution.get("output_path")})
    if execution.get("workflow_status") != "completed" or execution.get("integrity_status") != "passed":
        if execution.get("integrity_status") == "partially_verified":
            report = execution.get("integrity_report") or {}
            limits = " ".join(report.get("unverified_features", [])) or report.get("summary", "Some features were not fully checked.")
            result.update({"execution_status": "completed_partially_verified",
                           "final_response": f"Document execution completed with partial integrity verification. {limits} No downloadable output was created."})
            return result
        result.update({"execution_status": "integrity_failed" if execution.get("integrity_status") == "failed" else "execution_failed",
                       "final_response": "Document execution failed or remains unverified. No downloadable output was created."})
        return result
    output_path = execution.get("output_path")
    try:
        output = Path(output_path).resolve() if output_path else None
        if output is None or output == source or output.parent != output_dir or not output.is_file():
            raise ValueError("The graph did not return a valid host-owned output file.")
    except (OSError, TypeError, ValueError) as exc:
        if output_path:
            try: Path(output_path).unlink(missing_ok=True)
            except OSError: pass
        result.update({"execution_status": "execution_failed", "integrity_status": "not_run",
                       "errors": [f"Document output validation failed: {exc}"],
                       "final_response": "Document execution failed. No downloadable output was created."})
        return result
    result.update({"execution_status": "completed_verified", "output_path": str(output),
                   "final_response": f"Completed and verified {len(result['execution_results'])} document operation(s). Output: {output}"})
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
    return original is not None and hmac.compare_digest(
        candidate.encode("utf-8"), original.encode("utf-8"),
    )


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
        "integrity_status": "not_run",
        "integrity_report": None,
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
        "integrity_status": "not_run",
        "integrity_report": None,
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
        "execution_status": "execution_failed",
        "integrity_status": "not_run",
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
