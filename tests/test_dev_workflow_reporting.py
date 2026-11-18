from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dev_workflow.reporting import build_final_report
from dev_workflow.schemas import (
    CodexExecutionResult, DiffReport, TaskProposal, VerificationCheckResult,
    WorkflowDecision, proposal_fingerprint,
)


def _proposal():
    return TaskProposal.model_validate({
        "task_summary": "Add a small feature.", "requirements": ["Keep behavior stable."],
        "acceptance_criteria": ["Feature works."],
        "implementation_steps": [{"order": 1, "description": "Implement it."}],
        "expected_files": [{"path": "src/app.py", "action": "modify", "rationale": "Feature code."}],
        "file_scope": {"included_files": ["src/app.py"]},
        "technical_decisions": [{"decision": "Use existing pattern", "rationale": "Consistency."}],
        "risks": ["Compatibility risk."], "testing_strategy": ["Run tests."],
    })


def _state(*, action="approve", execution="succeeded", diff="valid", check_status="passed", checks=True):
    proposal = _proposal()
    decision = WorkflowDecision(
        action=action, proposal_fingerprint=proposal_fingerprint(proposal), proposal_revision=2,
        decision_timestamp=datetime(2026, 11, 18, tzinfo=timezone.utc),
    )
    result = None if execution is None else CodexExecutionResult(
        command_identity=["codex", "exec"], exit_code=0 if execution == "succeeded" else None if execution == "timeout" else 1,
        stdout="", stderr="", last_message=None, parsed_result=None, duration_seconds=1,
        status=execution, timed_out=execution == "timeout",
    )
    diff_result = None if diff is None else DiffReport(
        status=diff, changed_files=[{"path": "src/app.py", "action": "modify"}] if diff != "not_collected" else [],
        errors=["Scope violation."] if diff == "invalid" else [],
    )
    check = VerificationCheckResult(
        check_id="unit", display_name="Unit tests", command_identity=["pytest"],
        working_directory="worktree", status=check_status,
        exit_code=0 if check_status == "passed" else None if check_status in {"timed_out", "not_run"} else 1,
        duration_seconds=0 if check_status == "not_run" else 1,
    )
    return {
        "run_id": "run-1", "task_description": proposal.task_summary,
        "current_stage": "rejected" if action == "reject" else "implemented",
        "proposal": proposal.model_dump(mode="json"), "decisions": [decision.model_dump(mode="json")],
        "execution_result": result.model_dump(mode="json") if result else None,
        "diff_report": diff_result.model_dump(mode="json") if diff_result else None,
        "verification_results": [check.model_dump(mode="json")] if checks else [],
        "errors": [],
    }


def test_success_requires_all_required_evidence_and_preserves_approval():
    report = build_final_report(_state(), required_check_ids=["unit"])
    assert report.overall_status == "passed"
    assert report.proposal_revision == 2
    assert report.approved_proposal_fingerprint == proposal_fingerprint(_proposal())
    assert report.changed_files[0].path == "src/app.py"


@pytest.mark.parametrize(("state", "expected"), [
    (_state(execution="failed"), "failed"),
    (_state(diff="invalid"), "failed"),
    (_state(check_status="failed"), "failed"),
    (_state(check_status="timed_out"), "failed"),
    (_state(checks=False), "failed"),
    (_state(action="reject", execution=None), "rejected"),
    (_state(execution=None, diff=None, checks=False), "failed"),
])
def test_failure_and_incomplete_outcomes_never_pass(state, expected):
    report = build_final_report(state, required_check_ids=["unit"])
    assert report.overall_status == expected


def test_missing_evidence_is_not_fabricated():
    state = _state(execution=None, diff=None, checks=False)
    report = build_final_report(state, required_check_ids=["unit"])
    assert report.implementation_status == "not_run"
    assert report.diff_validation_status == "not_collected"
    assert report.changed_files == []
    assert report.verification_results == []
    assert any("missing" in failure for failure in report.failures)


def test_documentation_only_report_can_pass_without_irrelevant_checks():
    state = _state(checks=False)
    state["diff_report"]["changed_files"] = [{"path": "docs/guide.md", "action": "modify"}]

    report = build_final_report(state, required_check_ids=[])

    assert report.overall_status == "passed"
    assert report.verification_results == []
    assert report.failures == []


def test_approval_pending_is_distinct():
    state = _state(execution=None, diff=None, checks=False)
    state["decisions"] = []
    state["current_stage"] = "awaiting_approval"
    report = build_final_report(state, required_check_ids=["unit"])
    assert report.overall_status == "awaiting_approval"
