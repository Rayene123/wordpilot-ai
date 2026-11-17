"""Read-only proposal, human review, and bounded implementation workflow."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from .codex_cli import analyze_task as run_codex_analysis, implement_approved_plan
from .preflight import run_write_capability_preflight
from .schemas import (
    CodexExecutionResult, DiffReport, TaskProposal, WorkflowDecision,
    WriteCapabilityPreflightResult,
    proposal_fingerprint, validate_decision_for_proposal,
)
from .state import DevWorkflowState
from .worktree import collect_worktree_diff, create_worktree

Analyzer = Callable[..., tuple[CodexExecutionResult, TaskProposal | Mapping[str, Any] | None]]


def new_workflow_state(
    task_description: str, repository_path: str | Path, *, max_plan_revisions: int = 2,
) -> DevWorkflowState:
    task = task_description.strip()
    if not task:
        raise ValueError("A non-empty task description is required.")
    repository = Path(repository_path).expanduser().resolve()
    if not repository.is_dir():
        raise ValueError("Repository path must be an existing directory.")
    if not 0 <= max_plan_revisions <= 5:
        raise ValueError("max_plan_revisions must be between 0 and 5.")
    return {
        "run_id": str(uuid.uuid4()), "task_description": task,
        "repository_path": str(repository), "current_stage": "analysis_pending",
        "errors": [], "proposal": None, "proposal_fingerprint": None,
        "decisions": [], "proposal_history": [], "revision_count": 0,
        "max_plan_revisions": max_plan_revisions, "revision_feedback": None,
        "analysis_evidence": None, "execution_result": None,
        "worktree_path": None, "diff_report": None, "preflight_result": None,
        "validation_errors": [],
        "final_status": "pending",
    }


def workflow_config(run_id: str) -> RunnableConfig:
    if not run_id.strip():
        raise ValueError("A non-empty run ID is required for checkpointing.")
    return {"configurable": {"thread_id": run_id, "__pregel_strict_resume": True}}


def build_dev_workflow(
    *, analyzer: Analyzer = run_codex_analysis,
    implementer: Callable[..., CodexExecutionResult] = implement_approved_plan,
    worktree_creator: Callable[..., str] = create_worktree,
    diff_collector: Callable[..., DiffReport] = collect_worktree_diff,
    preflight_runner: Callable[..., WriteCapabilityPreflightResult | Mapping[str, Any]] = run_write_capability_preflight,
    checkpointer: BaseCheckpointSaver | None = None,
    max_plan_revisions: int = 2,
) -> Any:
    """Build a checkpointed workflow; adapters are injectable for tests."""
    if not 0 <= max_plan_revisions <= 5:
        raise ValueError("max_plan_revisions must be between 0 and 5.")
    if checkpointer is None:
        raise ValueError(
            "An explicit checkpointer is required; use SQLite for resumable workflows "
            "or inject InMemorySaver in isolated tests."
        )
    saver = checkpointer

    def analyze_node(state: DevWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        if config.get("configurable", {}).get("thread_id") != state["run_id"]:
            return _fail("Workflow thread ID must match the run ID.")
        try:
            execution, raw = analyzer(
                state["task_description"], state["repository_path"],
                feedback=state.get("revision_feedback"),
                previous_proposal=state.get("proposal"),
            )
            execution = CodexExecutionResult.model_validate(execution)
        except Exception:
            return _fail("Codex task analysis failed.")
        evidence = execution.model_dump(mode="json")
        if execution.status != "succeeded" or raw is None:
            return {**_fail(execution.error or "Codex did not return a proposal."), "analysis_evidence": evidence}
        try:
            raw_value = raw.model_dump(mode="json") if isinstance(raw, TaskProposal) else dict(raw)
            proposal_value = json.loads(json.dumps(raw_value, ensure_ascii=False))
            proposal = TaskProposal.model_validate(proposal_value)
            proposal_value = proposal.model_dump(mode="json")
        except (ValidationError, TypeError, ValueError):
            return {**_fail("Codex proposal failed strict validation."), "analysis_evidence": evidence}
        return {
            "current_stage": "analysis_completed", "final_status": "pending", "errors": [],
            "proposal": proposal_value,
            "proposal_fingerprint": proposal_fingerprint(
                proposal, revision=state.get("revision_count", 0),
            ),
            "analysis_evidence": evidence, "validation_errors": [], "revision_feedback": None,
        }

    def validate_node(state: DevWorkflowState) -> dict[str, Any]:
        try:
            proposal = TaskProposal.model_validate(state.get("proposal"))
            if proposal_fingerprint(proposal, revision=state.get("revision_count", 0)) != state.get("proposal_fingerprint"):
                raise ValueError("Proposal fingerprint mismatch.")
            root = Path(state["repository_path"]).resolve()
            scope_errors = _validate_proposal_paths(proposal, root)
            if scope_errors:
                return _validation_failure(*scope_errors)
        except (ValidationError, ValueError, OSError, RuntimeError):
            return _validation_failure("Proposal is invalid, unsafe, or has a mismatched fingerprint.")
        return {
            "current_stage": "awaiting_approval", "final_status": "awaiting_approval",
            "proposal": proposal.model_dump(mode="json"), "errors": [], "validation_errors": [],
        }

    def approval_node(state: DevWorkflowState) -> dict[str, Any]:
        # The full proposal is checkpointed and returned as interrupt data for a future UI.
        decision_value = interrupt({
            "kind": "implementation_proposal_review", "run_id": state["run_id"],
            "proposal": state.get("proposal"),
            "proposal_fingerprint": state.get("proposal_fingerprint"),
            "revision_count": state.get("revision_count", 0),
            "max_plan_revisions": state.get("max_plan_revisions", 2),
        })
        try:
            proposal = TaskProposal.model_validate(state.get("proposal"))
            decision = WorkflowDecision.model_validate(decision_value)
            current_revision = state.get("revision_count", 0)
            validate_decision_for_proposal(
                decision, proposal, expected_revision=current_revision,
            )
            if proposal_fingerprint(proposal, revision=current_revision) != state.get("proposal_fingerprint"):
                raise ValueError("Stale proposal.")
        except (ValidationError, TypeError, ValueError):
            return _fail("Decision is missing, invalid, stale, or bound to a modified proposal.", decisions=state.get("decisions", []))
        history = [*state.get("decisions", []), decision.model_dump(mode="json")]
        if decision.action == "reject":
            return {"current_stage": "rejected", "final_status": "rejected", "decisions": history, "errors": []}
        if decision.action == "request_changes":
            revised_count = state.get("revision_count", 0) + 1
            limit = min(state.get("max_plan_revisions", max_plan_revisions), max_plan_revisions)
            if revised_count > limit:
                return _fail("Maximum proposal revision count exceeded.", decisions=history)
            return {
                "current_stage": "analysis_pending", "final_status": "pending",
                "decisions": history, "proposal_history": [*state.get("proposal_history", []), {
                    "proposal": state.get("proposal"),
                    "proposal_fingerprint": state.get("proposal_fingerprint"),
                    "proposal_revision": state.get("revision_count", 0),
                }], "revision_count": revised_count, "revision_feedback": decision.feedback,
                "proposal_fingerprint": None,
            }
        return {"current_stage": "approved", "final_status": "approved", "decisions": history, "errors": []}

    def implement_node(state: DevWorkflowState) -> dict[str, Any]:
        preflight_value: dict[str, Any] | None = None
        try:
            proposal = TaskProposal.model_validate(state.get("proposal"))
            decision = WorkflowDecision.model_validate((state.get("decisions") or [])[-1])
            current_revision = state.get("revision_count", 0)
            validate_decision_for_proposal(
                decision, proposal, expected_revision=current_revision,
            )
            if decision.action != "approve" or decision.proposal_fingerprint != state.get("proposal_fingerprint"):
                raise ValueError("Implementation lacks approval for the latest proposal.")
            repository = Path(state["repository_path"]).resolve()
            if any(
                not proposal.file_scope.allows(item.path)
                or repository not in (repository / item.path).resolve().parents
                for item in proposal.expected_files
            ):
                raise ValueError("Approved file scope is unsafe.")
            preflight = WriteCapabilityPreflightResult.model_validate(
                preflight_runner(
                    state["repository_path"], implementer=implementer,
                    worktree_creator=worktree_creator, diff_collector=diff_collector,
                )
            )
            preflight_value = preflight.model_dump(mode="json")
            if preflight.status != "passed":
                return _fail(
                    "Workspace-write preflight failed; task implementation was not started.",
                    preflight_result=preflight_value,
                )
            worktree = worktree_creator(state["repository_path"], state["run_id"])
            result = CodexExecutionResult.model_validate(
                implementer(
                    proposal, state["proposal_fingerprint"], worktree,
                    proposal_revision=current_revision,
                )
            )
            diff = DiffReport.model_validate(diff_collector(worktree, proposal))
            if diff.status != "valid":
                return {"current_stage": "failed", "final_status": "failed", "worktree_path": worktree,
                        "execution_result": result.model_dump(mode="json"), "diff_report": diff.model_dump(mode="json"),
                        "preflight_result": preflight_value,
                        "errors": ["Implementation diff failed scope validation."]}
            status = "implemented" if result.status == "succeeded" else "failed"
            return {"current_stage": status, "final_status": status, "worktree_path": worktree,
                    "execution_result": result.model_dump(mode="json"), "diff_report": diff.model_dump(mode="json"),
                    "preflight_result": preflight_value,
                    "errors": [] if status == "implemented" else ["Codex implementation failed."]}
        except Exception:
            return _fail("Approved implementation could not be completed safely.", preflight_result=preflight_value)

    builder = StateGraph(DevWorkflowState)
    builder.add_node("analyze_task", analyze_node)
    builder.add_node("validate_proposal", validate_node)
    builder.add_node("request_approval", approval_node)
    builder.add_node("implement_approved_proposal", implement_node)
    builder.add_edge(START, "analyze_task")
    builder.add_conditional_edges("analyze_task", lambda s: "validate_proposal" if s.get("current_stage") == "analysis_completed" else END)
    builder.add_conditional_edges("validate_proposal", lambda s: "request_approval" if s.get("current_stage") == "awaiting_approval" else END)
    def route_decision(state: DevWorkflowState) -> str:
        if state.get("current_stage") == "analysis_pending":
            return "analyze_task"
        if state.get("current_stage") == "approved":
            return "implement_approved_proposal"
        return END
    builder.add_conditional_edges("request_approval", route_decision)
    builder.add_edge("implement_approved_proposal", END)
    return builder.compile(checkpointer=saver)


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"current_stage": "failed", "final_status": "failed", "errors": [message], **extra}


def _validation_failure(*errors: str) -> dict[str, Any]:
    return {"current_stage": "failed", "final_status": "failed", "proposal_fingerprint": None,
            "validation_errors": list(errors), "errors": list(errors)}


def _validate_proposal_paths(proposal: TaskProposal, repository: Path) -> list[str]:
    """Check proposal paths against the repository before human review."""
    root = repository.resolve()
    errors: list[str] = []

    def resolve_relative(value: str) -> tuple[Path, Path, bool]:
        relative = Path(value.rstrip("/"))
        lexical = root / relative
        resolved = lexical.resolve()
        inside = resolved == root or root in resolved.parents
        has_symlink = False
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                has_symlink = True
        return lexical, resolved, inside and not has_symlink

    expected = {item.path.casefold(): item for item in proposal.expected_files}
    for item in proposal.expected_files:
        lexical, _, safe = resolve_relative(item.path)
        if not safe:
            errors.append("Expected file path escapes the repository or traverses a symlink.")
            continue
        if item.action == "create":
            if lexical.exists() or lexical.is_symlink():
                errors.append("A create action must target a path that does not already exist.")
        elif not lexical.is_file():
            errors.append("Modify and delete actions must target an existing regular file.")

    for field_name, entries in (
        ("included_files", proposal.file_scope.included_files),
        ("excluded_files", proposal.file_scope.excluded_files),
    ):
        for entry in entries:
            is_directory = entry.endswith("/")
            lexical, _, safe = resolve_relative(entry)
            if not safe:
                errors.append(f"{field_name} contains a path outside the repository or through a symlink.")
                continue
            if lexical.exists():
                if is_directory != lexical.is_dir():
                    errors.append(f"{field_name} entry has an incorrect file/directory form.")
                continue
            if field_name == "excluded_files":
                continue
            entry_key = entry.casefold()
            if entry_key in expected and expected[entry_key].action == "create":
                continue
            if is_directory and any(
                item.action == "create" and item.path.casefold().startswith(entry_key)
                for item in proposal.expected_files
            ):
                continue
            errors.append("An included path must exist or contain an expected create target.")
    return errors
