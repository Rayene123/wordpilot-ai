"""Deterministic final reports from validated DevFlow state and evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .schemas import (
    ChangedFile, CodexExecutionResult, DiffReport, FinalReport, TaskProposal,
    VerificationCheckResult, WorkflowDecision,
)
from .state import DevWorkflowState


def build_final_report(
    state: DevWorkflowState | Mapping[str, Any],
    *,
    required_check_ids: Sequence[str],
) -> FinalReport:
    """Build a report from checkpointed data; required check IDs come from host config."""
    proposal = _validate_optional(TaskProposal, state.get("proposal"))
    decisions = [WorkflowDecision.model_validate(item) for item in state.get("decisions", [])]
    execution = _validate_optional(CodexExecutionResult, state.get("execution_result"))
    diff = _validate_optional(DiffReport, state.get("diff_report"))
    checks = [VerificationCheckResult.model_validate(item) for item in state.get("verification_results", [])]

    approved = next((item for item in reversed(decisions) if item.action == "approve"), None)
    rejected = next((item for item in reversed(decisions) if item.action == "reject"), None)
    latest = decisions[-1] if decisions else None
    implementation = (
        "succeeded" if execution and execution.status == "succeeded"
        else "timed_out" if execution and execution.status == "timeout"
        else "failed" if execution
        else "not_run"
    )
    diff_status = diff.status if diff else "not_collected"
    recorded = {result.check_id: result for result in checks}
    required = list(required_check_ids)
    failures = list(state.get("errors") or [])
    if state.get("validation_errors"):
        failures.extend(state["validation_errors"])

    if rejected:
        overall = "rejected"
        failures.append("Proposal was rejected by a human reviewer.")
    elif latest is None or latest.action != "approve":
        overall = "awaiting_approval" if state.get("current_stage") in {"awaiting_approval", "analysis_completed"} else "failed"
        if overall == "failed":
            failures.append("Workflow ended before approval evidence was recorded.")
    else:
        overall = "failed"

    if execution and execution.status != "succeeded":
        failures.append("Implementation did not complete successfully.")
    if diff and diff.status in {"invalid", "empty"}:
        failures.extend(diff.errors or ["Diff validation did not pass."])
    missing = [check_id for check_id in required if check_id not in recorded]
    if missing:
        failures.append("Required verification results are missing: " + ", ".join(missing) + ".")
    for result in checks:
        if result.status == "failed":
            failures.append(f"Verification check '{result.check_id}' failed.")
        elif result.status == "timed_out":
            failures.append(f"Verification check '{result.check_id}' timed out.")
        elif result.status == "not_run":
            failures.append(f"Verification check '{result.check_id}' was not run.")

    complete_checks = all(
        check_id in recorded and recorded[check_id].status == "passed" for check_id in required
    )
    success = (
        latest is not None and latest.action == "approve"
        and implementation == "succeeded" and diff_status == "valid"
        and complete_checks and not failures
    )
    if success:
        overall = "passed"

    return FinalReport(
        run_id=state["run_id"], overall_status=overall,
        task_summary=proposal.task_summary if proposal else state.get("task_description", "Workflow incomplete"),
        approved_proposal_fingerprint=approved.proposal_fingerprint if approved else None,
        proposal_revision=approved.proposal_revision if approved else (latest.proposal_revision if latest else state.get("revision_count", 0)),
        human_decisions=decisions, implementation_status=implementation,
        changed_files=diff.changed_files if diff else [], diff_validation_status=diff_status,
        verification_results=checks, failures=_unique(failures),
        remaining_risks=proposal.risks if proposal else [],
        evidence_references=_evidence_references(state),
    )


def _validate_optional(model: type, value: Any):
    if value is None:
        return None
    return model.model_validate(value)


def _evidence_references(state: Mapping[str, Any]) -> list[str]:
    refs = list(state.get("evidence_references") or [])
    preflight = state.get("preflight_result")
    if isinstance(preflight, Mapping) and preflight.get("evidence_path"):
        # Absolute host paths are deliberately omitted by FinalReport's safe-reference contract.
        value = preflight["evidence_path"]
        if isinstance(value, str) and not _is_absolute(value) and ".." not in value.replace("\\", "/").split("/"):
            refs.append(value)
    return _unique(refs)


def _is_absolute(value: str) -> bool:
    from pathlib import Path, PureWindowsPath
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or bool(PureWindowsPath(value).drive)


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))
