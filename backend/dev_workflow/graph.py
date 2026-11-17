"""Read-only analysis and human approval graph for WordPilot DevFlow.

The default ``InMemorySaver`` checkpoint is process-local and does not survive
process restarts. Keep one compiled graph instance alive while an interrupted
run awaits a decision, and resume it with the same ``thread_id``.

This milestone deliberately has no implementation, worktree, or verification
node. Approval ends the graph; it does not authorize any hidden side effect.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from .codex_cli import analyze_task as run_codex_analysis
from .schemas import (
    CodexExecutionResult,
    TaskProposal,
    WorkflowApproval,
    proposal_fingerprint,
    validate_approval_for_proposal,
)
from .state import DevWorkflowState

Analyzer = Callable[
    [str, str | Path], tuple[CodexExecutionResult, TaskProposal | Mapping[str, Any] | None]
]


def new_workflow_state(task_description: str, repository_path: str | Path) -> DevWorkflowState:
    """Create serializable input state with a unique run ID."""
    task = task_description.strip()
    if not task:
        raise ValueError("A non-empty task description is required.")
    repository = Path(repository_path).expanduser().resolve()
    if not repository.is_dir():
        raise ValueError("Repository path must be an existing directory.")
    return {
        "run_id": str(uuid.uuid4()),
        "task_description": task,
        "repository_path": str(repository),
        "current_stage": "analysis_pending",
        "errors": [],
        "proposal": None,
        "proposal_fingerprint": None,
        "approval": None,
        "execution_result": None,
        "analysis_evidence": None,
        "validation_errors": [],
        "final_status": "pending",
    }


def workflow_config(run_id: str) -> RunnableConfig:
    """Return the strict, stable LangGraph checkpoint config for this run.

    LangGraph otherwise treats ``Command(resume={})`` as an empty interrupt-ID
    map and leaves the run paused. Strict resume mode rejects that input before
    it can be mistaken for a valid decision or silently ignored.
    """
    if not run_id.strip():
        raise ValueError("A non-empty run ID is required for checkpointing.")
    return {"configurable": {"thread_id": run_id, "__pregel_strict_resume": True}}


def build_dev_workflow(
    *,
    analyzer: Analyzer = run_codex_analysis,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build the read-only analysis → validation → approval workflow.

    ``analyzer`` is injectable for tests. The compiled graph retains it in a
    host-side closure; it is never stored in checkpointed state.
    """
    saver = checkpointer if checkpointer is not None else InMemorySaver()

    def analyze_node(state: DevWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config.get("configurable", {}).get("thread_id")
        if thread_id != state["run_id"]:
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "errors": ["Workflow thread ID must match the run ID."],
                "analysis_evidence": None,
                "proposal": None,
                "validation_errors": [],
            }
        try:
            execution, raw_proposal = analyzer(
                state["task_description"], state["repository_path"],
            )
            execution = CodexExecutionResult.model_validate(execution)
        except Exception:
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "errors": ["Codex task analysis failed."],
                "analysis_evidence": None,
                "proposal": None,
                "validation_errors": [],
            }

        evidence = json.loads(json.dumps(execution.model_dump(mode="json"), ensure_ascii=False))
        if execution.status != "succeeded" or raw_proposal is None:
            reason = execution.error or "Codex did not return a valid structured proposal."
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "errors": [reason],
                "analysis_evidence": evidence,
                "proposal": None,
                "validation_errors": [],
            }

        if isinstance(raw_proposal, TaskProposal):
            proposal_value: Any = raw_proposal.model_dump(mode="json")
        elif isinstance(raw_proposal, Mapping):
            proposal_value = dict(raw_proposal)
        else:
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "errors": ["Codex did not return a structured task proposal."],
                "analysis_evidence": evidence,
                "proposal": None,
                "validation_errors": ["Proposal must be a JSON object."],
            }

        try:
            # Keep checkpoint state JSON-friendly even for injected adapters.
            proposal_value = json.loads(json.dumps(proposal_value, ensure_ascii=False))
        except (TypeError, ValueError):
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "errors": ["Codex task proposal was not JSON-serializable."],
                "analysis_evidence": evidence,
                "proposal": None,
                "validation_errors": ["Proposal must be JSON-serializable."],
            }

        return {
            "current_stage": "analysis_completed",
            "final_status": "pending",
            "errors": [],
            "analysis_evidence": evidence,
            "proposal": proposal_value,
            "validation_errors": [],
        }

    def validate_node(state: DevWorkflowState) -> dict[str, Any]:
        try:
            proposal = TaskProposal.model_validate(state.get("proposal"))
        except (ValidationError, TypeError, ValueError):
            return _validation_failure("Proposal does not match the required task schema.")

        scope_errors = _validate_scope(proposal)
        if scope_errors:
            return _validation_failure(*scope_errors)

        fingerprint = proposal_fingerprint(proposal)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "proposal_fingerprint": fingerprint,
            "current_stage": "awaiting_approval",
            "final_status": "awaiting_approval",
            "validation_errors": [],
            "errors": [],
        }

    def approval_node(state: DevWorkflowState) -> dict[str, Any]:
        # interrupt() is intentionally the first operation in this node. On
        # resume LangGraph re-enters the node and supplies the host's decision.
        decision = interrupt({
            "kind": "proposal_approval",
            "run_id": state["run_id"],
            "proposal": state.get("proposal"),
            "proposal_fingerprint": state.get("proposal_fingerprint"),
        })
        try:
            proposal = TaskProposal.model_validate(state.get("proposal"))
            current_fingerprint = proposal_fingerprint(proposal)
            if current_fingerprint != state.get("proposal_fingerprint"):
                raise ValueError("The proposal changed after it was presented for approval.")
            approval = WorkflowApproval.model_validate(decision)
            validate_approval_for_proposal(approval, proposal)
        except (ValidationError, TypeError, ValueError):
            return {
                "current_stage": "failed",
                "final_status": "failed",
                "approval": None,
                "errors": ["Approval is missing, invalid, stale, or bound to a modified proposal."],
            }

        if approval.approved is not True:
            return {
                "current_stage": "rejected",
                "final_status": "rejected",
                "approval": approval.model_dump(mode="json"),
                "errors": [],
            }
        return {
            "current_stage": "approved",
            "final_status": "approved",
            "approval": approval.model_dump(mode="json"),
            "errors": [],
        }

    builder = StateGraph(DevWorkflowState)
    builder.add_node("analyze_task", analyze_node)
    builder.add_node("validate_proposal", validate_node)
    builder.add_node("request_approval", approval_node)
    builder.add_edge(START, "analyze_task")
    builder.add_conditional_edges(
        "analyze_task",
        lambda state: "validate_proposal" if state.get("current_stage") == "analysis_completed" else END,
    )
    builder.add_conditional_edges(
        "validate_proposal",
        lambda state: "request_approval" if state.get("current_stage") == "awaiting_approval" else END,
    )
    builder.add_edge("request_approval", END)
    return builder.compile(checkpointer=saver)


def _validation_failure(*errors: str) -> dict[str, Any]:
    """Return a terminal validation failure without carrying unsafe details."""
    return {
        "current_stage": "failed",
        "final_status": "failed",
        "proposal_fingerprint": None,
        "validation_errors": list(errors),
        "errors": list(errors),
    }


def _validate_scope(proposal: TaskProposal) -> list[str]:
    """Reject unsafe, ambiguous, empty, or conflicting proposal file scopes."""
    proposed = _normalize_paths(proposal.proposed_files, "proposed_files")
    excluded = _normalize_paths(proposal.excluded_files, "excluded_files")
    errors = proposed[1] + excluded[1]
    proposed_paths = proposed[0]
    excluded_paths = excluded[0]
    if not proposed_paths:
        errors.append("At least one proposed file is required to define the task scope.")
    if set(proposed_paths).intersection(excluded_paths):
        errors.append("A file cannot be both proposed and excluded.")

    for proposed_path in proposed_paths:
        for excluded_path in excluded_paths:
            if _paths_overlap(proposed_path, excluded_path):
                errors.append("Proposed and excluded file scopes overlap.")
                return errors
    return errors


def _normalize_paths(paths: list[str], field: str) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for value in paths:
        candidate = value.strip()
        windows_path = PureWindowsPath(candidate)
        portable = candidate.replace("\\", "/")
        posix_path = PurePosixPath(portable)
        parts = portable.rstrip("/").split("/")
        unsafe = (
            not candidate
            or "\x00" in candidate
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or posix_path.is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or ":" in candidate
            or any(char in candidate for char in "*?[]")
        )
        if unsafe:
            errors.append(f"{field} contains a path that is not a safe relative path.")
            continue
        path = "/".join(posix_path.parts)
        key = path.casefold()
        if key in seen:
            errors.append(f"{field} contains duplicate paths.")
            continue
        seen.add(key)
        normalized.append(path)
    return normalized, errors


def _paths_overlap(left: str, right: str) -> bool:
    """Check exact and parent/child conflicts independent of path casing."""
    left_key, right_key = left.casefold(), right.casefold()
    return (
        left_key == right_key
        or left_key.startswith(right_key.rstrip("/") + "/")
        or right_key.startswith(left_key.rstrip("/") + "/")
    )
