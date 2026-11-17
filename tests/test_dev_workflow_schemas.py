from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.dev_workflow.schemas import (
    TaskProposal,
    WorkflowApproval,
    new_approval,
    proposal_fingerprint,
    validate_approval_for_proposal,
)


def _proposal(**updates):
    values = {
        "task_description": "Add a response field.",
        "requirements": ["Keep the API response compatible."],
        "acceptance_criteria": ["The field appears in a successful response."],
        "implementation_plan": ["Update the response model."],
        "risks": [],
        "proposed_files": ["backend/api.py"],
        "excluded_files": ["frontend/"],
        "assumptions": [],
    }
    return TaskProposal.model_validate({**values, **updates})


def test_task_proposal_validates_and_rejects_unknown_fields():
    proposal = _proposal()
    assert proposal.requirements == ["Keep the API response compatible."]
    with pytest.raises(ValidationError):
        _proposal(unexpected="not accepted")


@pytest.mark.parametrize("field", ["requirements", "acceptance_criteria", "implementation_plan"])
def test_task_proposal_requires_nonempty_core_lists(field):
    with pytest.raises(ValidationError):
        _proposal(**{field: []})


def test_task_proposal_rejects_blank_list_items():
    with pytest.raises(ValidationError):
        _proposal(risks=["  "])


def test_proposal_fingerprint_is_stable_and_sensitive_to_content():
    original = _proposal()
    assert proposal_fingerprint(original) == proposal_fingerprint(_proposal())
    assert proposal_fingerprint(original) != proposal_fingerprint(
        _proposal(task_description="Change a different thing.")
    )


def test_approval_is_explicit_timezone_aware_and_bound_to_proposal():
    proposal = _proposal()
    approval = new_approval(
        proposal, approved=True, decision_timestamp=datetime(2026, 11, 17, tzinfo=timezone.utc),
    )
    assert validate_approval_for_proposal(approval, proposal) is approval
    with pytest.raises(ValueError, match="different task proposal"):
        validate_approval_for_proposal(approval, _proposal(task_description="Other task."))
    with pytest.raises(ValidationError):
        WorkflowApproval(
            approved="true", proposal_fingerprint=proposal_fingerprint(proposal),
            decision_timestamp=datetime.now(timezone.utc),
        )
    with pytest.raises(ValidationError):
        WorkflowApproval(
            approved=True, proposal_fingerprint=proposal_fingerprint(proposal),
            decision_timestamp=datetime(2026, 11, 17),
        )
