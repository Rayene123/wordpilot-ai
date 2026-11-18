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
from .schemas import (
    CodexExecutionResult, DiffReport, TaskProposal, VerificationCheckResult, WorkflowDecision,
    proposal_fingerprint, validate_decision_for_proposal,
)
from .reporting import build_final_report
from .state import DevWorkflowState
from .verification import (
    DEFAULT_CHECKS, checks_for_changed_files, run_verification,
    safe_verification_diagnostic,
)
from .worktree import collect_worktree_diff, create_worktree, diff_identity, validate_registered_worktree

Analyzer = Callable[..., tuple[CodexExecutionResult, TaskProposal | Mapping[str, Any] | None]]


def new_workflow_state(
    task_description: str, repository_path: str | Path, *, max_plan_revisions: int = 2,
) -> DevWorkflowState:
    if not task_description.strip():
        raise ValueError("A non-empty task description is required.")
    task = task_description
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
        "worktree_path": None, "diff_report": None, "verification_results": [],
        "final_report": None, "validation_errors": [],
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
    verification_runner: Callable[..., list[VerificationCheckResult]] = run_verification,
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
            worktree = validate_registered_worktree(
                state["repository_path"], worktree_creator(state["repository_path"], state["run_id"]),
            )
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
                        "errors": ["Implementation diff failed scope validation."]}
            status = "implemented" if result.status == "succeeded" else "failed"
            return {"current_stage": status, "final_status": status, "worktree_path": worktree,
                    "execution_result": result.model_dump(mode="json"), "diff_report": diff.model_dump(mode="json"),
                    "errors": [] if status == "implemented" else ["Codex implementation failed."]}
        except Exception:
            return _fail("Approved implementation could not be completed safely.")

    def verification_node(state: DevWorkflowState) -> dict[str, Any]:
        results: list[VerificationCheckResult] = []
        errors = list(state.get("errors", []))
        try:
            worktree = validate_registered_worktree(state["repository_path"], state["worktree_path"])
            diff_report = DiffReport.model_validate(state.get("diff_report"))
            if diff_report.status != "valid":
                raise ValueError("Verification requires a host-validated worktree diff.")
            checks = checks_for_changed_files(diff_report.changed_files)
            before = diff_identity(worktree)
            results = _validate_verification_results(
                verification_runner(worktree, diff_report.changed_files), checks,
            )
        except Exception as exc:
            errors.append("Host verification could not be completed safely: " + safe_verification_diagnostic(exc))
            return {
                "verification_results": [item.model_dump(mode="json") for item in results],
                "errors": errors,
            }
        try:
            after = diff_identity(worktree)
            if before != after:
                results = _invalidate_verification_results(
                    results, "Worktree source diff changed while verification was running.",
                )
                errors.append("Host verification invalidated: worktree source diff changed while checks were running.")
                try:
                    diff_report = DiffReport.model_validate(
                        diff_collector(worktree, TaskProposal.model_validate(state.get("proposal"))),
                    )
                except Exception as exc:
                    diff_report = _invalidate_diff_evidence(
                        diff_report,
                        "Final worktree diff could not be revalidated: " + safe_verification_diagnostic(exc),
                    )
                return {
                    "verification_results": [item.model_dump(mode="json") for item in results],
                    "diff_report": diff_report.model_dump(mode="json"),
                    "errors": errors,
                }
        except Exception as exc:
            results = _invalidate_verification_results(
                results, "Worktree source diff could not be revalidated after verification.",
            )
            errors.append("Host verification could not revalidate the worktree diff: " + safe_verification_diagnostic(exc))
            diff_report = _invalidate_diff_evidence(
                diff_report,
                "Final worktree diff identity could not be verified after checks.",
            )
        return {
            "verification_results": [item.model_dump(mode="json") for item in results],
            "diff_report": diff_report.model_dump(mode="json"),
            "errors": errors,
        }

    def report_node(state: DevWorkflowState) -> dict[str, Any]:
        try:
            report_state: Mapping[str, Any] = state
            if state.get("verification_results"):
                try:
                    diff = DiffReport.model_validate(state.get("diff_report"))
                    checks = checks_for_changed_files(diff.changed_files)
                    verified = _validate_verification_results(state.get("verification_results", []), checks)
                    report_state = {**state, "verification_results": [item.model_dump(mode="json") for item in verified]}
                except (TypeError, ValueError, ValidationError) as exc:
                    report_state = {
                        **state, "verification_results": [],
                        "errors": [*state.get("errors", []),
                                   "Verification evidence did not match host configuration: " + safe_verification_diagnostic(exc)],
                    }
            report = build_final_report(
                report_state,
                required_check_ids=[
                    check.check_id for check in _required_verification_checks(report_state)
                ],
            )
        except Exception:
            return {"current_stage": "failed", "final_status": "failed",
                    "errors": [*state.get("errors", []), "A final report could not be constructed from workflow evidence."]}
        status = {"passed": "implemented", "rejected": "rejected",
                  "awaiting_approval": "awaiting_approval", "failed": "failed"}[report.overall_status]
        return {"final_report": report.model_dump(mode="json"), "final_status": report.overall_status,
                "current_stage": status}

    builder = StateGraph(DevWorkflowState)
    builder.add_node("analyze_task", analyze_node)
    builder.add_node("validate_proposal", validate_node)
    builder.add_node("request_approval", approval_node)
    builder.add_node("implement_approved_proposal", implement_node)
    builder.add_node("verify_implementation", verification_node)
    builder.add_node("build_final_report", report_node)
    builder.add_edge(START, "analyze_task")
    builder.add_conditional_edges("analyze_task", lambda s: "validate_proposal" if s.get("current_stage") == "analysis_completed" else "build_final_report")
    builder.add_conditional_edges("validate_proposal", lambda s: "request_approval" if s.get("current_stage") == "awaiting_approval" else "build_final_report")
    def route_decision(state: DevWorkflowState) -> str:
        if state.get("current_stage") == "analysis_pending":
            return "analyze_task"
        if state.get("current_stage") == "approved":
            return "implement_approved_proposal"
        if state.get("current_stage") == "rejected" or state.get("current_stage") == "failed":
            return "build_final_report"
        return END
    builder.add_conditional_edges("request_approval", route_decision)
    builder.add_conditional_edges(
        "implement_approved_proposal",
        lambda s: "verify_implementation" if s.get("current_stage") == "implemented" else "build_final_report",
    )
    builder.add_edge("verify_implementation", "build_final_report")
    builder.add_edge("build_final_report", END)
    return builder.compile(checkpointer=saver)


def _required_verification_checks(state: Mapping[str, Any]):
    """Select report requirements only from the host-collected, validated diff."""
    diff_value = state.get("diff_report")
    if diff_value is None:
        return ()
    diff = DiffReport.model_validate(diff_value)
    if diff.status != "valid":
        return ()
    return checks_for_changed_files(diff.changed_files)


def _invalidate_verification_results(
    results: list[VerificationCheckResult], reason: str,
) -> list[VerificationCheckResult]:
    """Retain completed evidence while ensuring source-mutated checks cannot pass."""
    invalidated: list[VerificationCheckResult] = []
    for result in results:
        if result.status == "not_run":
            invalidated.append(result)
            continue
        detail = (result.stderr + ("\n" if result.stderr else "") + reason)[:8_192]
        invalidated.append(result.model_copy(update={"status": "failed", "stderr": detail}))
    return invalidated


def _invalidate_diff_evidence(diff: DiffReport, reason: str) -> DiffReport:
    """Do not retain a valid-diff claim when the final worktree cannot be confirmed."""
    return diff.model_copy(update={
        "status": "invalid",
        "errors": [*diff.errors, reason],
    })


def _validate_verification_results(
    values: Any, checks=DEFAULT_CHECKS,
) -> list[VerificationCheckResult]:
    """Require exactly one matching result for every configured host check."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("Verification results must be a sequence.")
    expected = {check.check_id: check for check in checks}
    actual: dict[str, VerificationCheckResult] = {}
    for value in values:
        result = VerificationCheckResult.model_validate(value)
        check = expected.get(result.check_id)
        if check is None or result.check_id in actual:
            raise ValueError("Verification returned an unexpected or duplicate check.")
        if (
            result.command_identity != list(check.arguments)
            or result.working_directory != "worktree"
            or result.working_directory_identity != check.working_directory
        ):
            raise ValueError("Verification evidence does not match host configuration.")
        actual[result.check_id] = result
    if set(actual) != set(expected):
        raise ValueError("Verification did not return every configured host check.")
    return [actual[check.check_id] for check in checks]


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
