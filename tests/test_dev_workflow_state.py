from __future__ import annotations

import json

from backend.dev_workflow.schemas import TaskProposal, new_approval
from backend.dev_workflow.state import DevWorkflowState


def test_state_is_json_serializable_and_contains_no_runtime_handles():
    proposal = TaskProposal(
        task_description="Add a response field.",
        requirements=["Keep compatibility."],
        acceptance_criteria=["Field is returned."],
        implementation_plan=["Update API."],
    )
    approval = new_approval(proposal, approved=False)
    state: DevWorkflowState = {
        "run_id": "run-1",
        "task_description": proposal.task_description,
        "repository_path": "C:/repo",
        "current_stage": "analysis_completed",
        "errors": [],
        "proposal": proposal.model_dump(mode="json"),
        "proposal_fingerprint": approval.proposal_fingerprint,
        "approval": approval.model_dump(mode="json"),
        "execution_result": None,
    }
    encoded = json.dumps(state)
    assert json.loads(encoded)["current_stage"] == "analysis_completed"
    assert "subprocess" not in encoded
