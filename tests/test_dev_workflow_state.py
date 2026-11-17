from __future__ import annotations

import json

from dev_workflow.graph import new_workflow_state
from dev_workflow.schemas import TaskProposal, new_workflow_decision


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
    encoded = json.dumps(state)
    assert json.loads(encoded)["current_stage"] == "analysis_pending"
    assert "subprocess" not in encoded
