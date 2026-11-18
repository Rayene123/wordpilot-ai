"""Serializable checkpoint state for one DevFlow run."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class DevWorkflowState(TypedDict):
    run_id: str
    task_description: str
    repository_path: str
    current_stage: Literal[
        "analysis_pending", "analysis_completed", "awaiting_approval", "approved",
        "rejected", "implemented", "failed",
    ]
    errors: list[str]
    proposal: NotRequired[dict[str, Any] | None]
    proposal_fingerprint: NotRequired[str | None]
    decisions: NotRequired[list[dict[str, Any]]]
    proposal_history: NotRequired[list[dict[str, Any]]]
    revision_count: NotRequired[int]
    max_plan_revisions: NotRequired[int]
    revision_feedback: NotRequired[str | None]
    analysis_evidence: NotRequired[dict[str, Any] | None]
    execution_result: NotRequired[dict[str, Any] | None]
    worktree_path: NotRequired[str | None]
    diff_report: NotRequired[dict[str, Any] | None]
    preflight_result: NotRequired[dict[str, Any] | None]
    preflight_result: NotRequired[dict[str, Any] | None]
    verification_results: NotRequired[list[dict[str, Any]]]
    final_report: NotRequired[dict[str, Any] | None]
    validation_errors: NotRequired[list[str]]
    final_status: NotRequired[Literal[
        "pending", "awaiting_approval", "approved", "rejected", "implemented", "passed", "failed",
    ]]
