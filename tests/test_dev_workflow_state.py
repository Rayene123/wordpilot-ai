from __future__ import annotations

import json

from dev_workflow.graph import new_workflow_state
from dev_workflow.schemas import (
    FinalReport, TaskProposal, VerificationCheckResult, new_workflow_decision,
)


def test_state_is_json_serializable_and_contains_no_runtime_handles(tmp_path):
    state = new_workflow_state("Add a response field.", tmp_path)
    proposal = TaskProposal.model_validate({
        "task_summary": "Add a response field.", "requirements": ["Keep compatibility."],
        "acceptance_criteria": ["Field is returned."],
        "implementation_steps": [{"order": 1, "description": "Update API."}],
        "expected_files": [{"path": "backend/api.py", "action": "modify", "rationale": "Add field."}],
        "file_scope": {"included_files": ["backend/api.py"]},
        "technical_decisions": [{"decision": "Extend response", "rationale": "Existing pattern."}],
        "testing_strategy": ["Run API tests."],
    })
    state["proposal"] = proposal.model_dump(mode="json")
    state["proposal_fingerprint"] = new_workflow_decision(proposal, action="reject").proposal_fingerprint
    state["decisions"] = [new_workflow_decision(proposal, action="reject").model_dump(mode="json")]
    check = VerificationCheckResult(
        check_id="pytest", display_name="Tests", command_identity=["python", "-m", "pytest"],
        working_directory="worktree", status="not_run", exit_code=None,
        duration_seconds=0, stdout="", stderr="",
    )
    report = FinalReport(
        run_id=state["run_id"], overall_status="rejected", task_summary=proposal.task_summary,
        approved_proposal_fingerprint=state["proposal_fingerprint"], proposal_revision=0,
        human_decisions=[new_workflow_decision(proposal, action="reject")],
        implementation_status="not_run", changed_files=[], diff_validation_status="not_collected",
        verification_results=[check], failures=[], remaining_risks=[], evidence_references=[],
    )
    state["verification_results"] = [check.model_dump(mode="json")]
    state["final_report"] = report.model_dump(mode="json")
    encoded = json.dumps(state)
    assert json.loads(encoded)["current_stage"] == "analysis_pending"
    assert "subprocess" not in encoded
    restored = json.loads(encoded)
    assert VerificationCheckResult.model_validate(restored["verification_results"][0]).status == "not_run"
    assert FinalReport.model_validate(restored["final_report"]).overall_status == "rejected"
