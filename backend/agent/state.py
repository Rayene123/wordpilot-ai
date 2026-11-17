"""Serializable state shape for the future LangGraph workflow."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

PlanningStatus = Literal[
    "pending", "awaiting_approval", "needs_clarification", "unsupported", "failed",
]
ApprovalStatus = Literal["pending", "approved", "rejected"]
ExecutionStatus = Literal["pending", "completed", "partial", "failed", "not_executed"]


class DocumentAgentState(TypedDict):
    """Graph state; document objects are represented only by paths and data."""

    document_path: str
    user_instruction: str
    document_summary: dict[str, Any] | None
    proposed_operations: list[dict[str, Any]]
    validated_operations: list[dict[str, Any]]
    validation_errors: list[str]
    errors: list[str]
    execution_results: list[dict[str, Any]]
    warnings: list[str]
    final_response: str | None
    output_path: str | None
    workflow_status: Literal["pending", "inspected", "validated", "completed", "partial", "failed"]
    planning_status: NotRequired[PlanningStatus]
    approval_status: NotRequired[ApprovalStatus]
    approved_operations: NotRequired[list[dict[str, Any]]]
    clarification_request: NotRequired[str | None]
    execution_status: NotRequired[ExecutionStatus]
