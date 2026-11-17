"""JSON-friendly state shape for one development workflow run."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class DevWorkflowState(TypedDict):
    """Serializable state; runtime process and filesystem handles stay outside."""

    run_id: str
    task_description: str
    repository_path: str
    current_stage: Literal[
        "analysis_pending", "analysis_completed", "awaiting_approval",
        "approved", "rejected", "failed",
    ]
    errors: list[str]
    proposal: NotRequired[dict[str, Any] | None]
    proposal_fingerprint: NotRequired[str | None]
    approval: NotRequired[dict[str, Any] | None]
    execution_result: NotRequired[dict[str, Any] | None]
