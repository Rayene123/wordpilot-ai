from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dev_workflow.schemas import (
    FileScope, TaskProposal, WorkflowDecision, codex_strict_json_schema, new_workflow_decision,
    proposal_fingerprint, validate_decision_for_proposal,
)


def _proposal(**updates):
    return TaskProposal.model_validate({
        "task_summary": "Add a response field.",
        "requirements": ["Keep the API response compatible."],
        "acceptance_criteria": ["The field appears in a successful response."],
        "implementation_steps": [{"order": 1, "description": "Update the response model."}],
        "expected_files": [{"path": "backend/api.py", "action": "modify", "rationale": "Add field."}],
        "file_scope": {"included_files": ["backend/api.py"]},
        "technical_decisions": [{"decision": "Use existing response model", "rationale": "Keep conventions."}],
        "risks": [], "edge_cases": [], "testing_strategy": ["Run the API tests."],
        "assumptions": [], **updates,
    })


def test_proposal_requires_all_structured_sections_and_rejects_unknown_fields():
    proposal = _proposal()
    assert proposal.requirements == ["Keep the API response compatible."]
    with pytest.raises(ValidationError):
        _proposal(unexpected="not accepted")


@pytest.mark.parametrize("field", ["requirements", "acceptance_criteria", "implementation_steps", "expected_files", "technical_decisions", "testing_strategy"])
def test_proposal_rejects_empty_required_lists(field):
    with pytest.raises(ValidationError):
        _proposal(**{field: []})


def test_proposal_rejects_blank_items_unordered_steps_and_scope_conflicts():
    with pytest.raises(ValidationError):
        _proposal(risks=["  "])
    with pytest.raises(ValidationError):
        _proposal(implementation_steps=[{"order": 2, "description": "Step."}])
    with pytest.raises(ValidationError):
        _proposal(file_scope={"included_files": ["backend/api.py"], "excluded_files": ["backend/api.py"]})
    with pytest.raises(ValidationError):
        _proposal(file_scope={"included_files": ["backend/api.py"], "excluded_files": ["backend/"]})


@pytest.mark.parametrize("scope_path", [
    "All other repository files, including fixture code and WordPilot application code",
    "descriptive prose",
    "tests/with spaces/README.md",
    " tests/README.md",
])
def test_file_scope_rejects_prose_and_unsupported_whitespace(scope_path):
    with pytest.raises(ValidationError):
        _proposal(file_scope={"included_files": [scope_path]})


@pytest.mark.parametrize("path", ["../outside.py", "C:\\secret.py", "/etc/passwd", "backend/*.py"])
def test_proposal_rejects_unsafe_paths(path):
    with pytest.raises(ValidationError):
        _proposal(expected_files=[{"path": path, "action": "modify", "rationale": "bad"}],
                  file_scope={"included_files": [path]})


def test_proposal_fingerprint_is_stable_and_sensitive_to_content():
    original = _proposal()
    assert proposal_fingerprint(original) == proposal_fingerprint(_proposal())
    assert proposal_fingerprint(original) != proposal_fingerprint(_proposal(task_summary="Other task."))
    assert proposal_fingerprint(original, revision=0) != proposal_fingerprint(original, revision=1)


@pytest.mark.parametrize("action", ["create", "modify", "delete"])
def test_proposal_keeps_supported_expected_file_actions(action):
    proposal = _proposal(expected_files=[{
        "path": "tests/devflow_demo/README.md", "action": action,
        "rationale": "The approved task changes this exact file.",
    }], file_scope={"included_files": ["tests/devflow_demo/README.md"]})
    assert proposal.expected_files[0].action == action


def test_documentation_only_proposal_has_exact_machine_checkable_scope():
    proposal = _proposal(
        task_summary="Document safe DevFlow demo fixtures.",
        requirements=["Create only tests/devflow_demo/README.md."],
        acceptance_criteria=["The README states the directory purpose and synthetic-only boundary."],
        implementation_steps=[{"order": 1, "description": "Create the README."}],
        expected_files=[{
            "path": "tests/devflow_demo/README.md", "action": "create",
            "rationale": "This is the sole requested document.",
        }],
        file_scope={"included_files": ["tests/devflow_demo/README.md"], "excluded_files": []},
        testing_strategy=["After implementation, inspect the README content and validate the complete diff contains only this created path."],
    )
    assert proposal.file_scope.included_files == ["tests/devflow_demo/README.md"]
    assert proposal.file_scope.excluded_files == []
    assert proposal.expected_files[0].action == "create"


def _assert_strict_objects(value):
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert set(value.get("required", [])) == set(properties)
            assert value.get("additionalProperties") is False
        assert "default" not in value
        for child in value.values():
            _assert_strict_objects(child)
    elif isinstance(value, list):
        for child in value:
            _assert_strict_objects(child)


def test_complete_proposal_schema_is_strict_at_every_nested_object():
    schema = codex_strict_json_schema(TaskProposal)
    _assert_strict_objects(schema)
    assert {"task_summary", "risks", "edge_cases", "assumptions"} <= set(schema["required"])
    assert "required" in schema["$defs"]["FileScope"]
    assert schema["$defs"]["FileScope"]["additionalProperties"] is False


def test_strict_schema_keeps_nullable_optionals_and_pydantic_defaults():
    class OptionalDetails(BaseModel):
        model_config = ConfigDict(extra="forbid")
        note: str | None = None

    class NestedProposal(BaseModel):
        model_config = ConfigDict(extra="forbid")
        details: OptionalDetails
        labels: list[str] = Field(default_factory=list)

    schema = codex_strict_json_schema(NestedProposal)
    _assert_strict_objects(schema)
    assert schema["required"] == ["details", "labels"]
    optional_field = schema["$defs"]["OptionalDetails"]["properties"]["note"]
    assert {"type": "null"} in optional_field["anyOf"]
    assert NestedProposal.model_validate({"details": {}}).labels == []
    assert OptionalDetails.model_validate({}).note is None
    assert FileScope.model_validate({"included_files": ["tests/"]}).excluded_files == []


def test_decisions_are_timezone_aware_and_bound_to_exact_proposal():
    proposal = _proposal()
    decision = new_workflow_decision(proposal, action="approve",
                                     decision_timestamp=datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert validate_decision_for_proposal(decision, proposal) is decision
    with pytest.raises(ValueError, match="different task proposal"):
        validate_decision_for_proposal(decision, _proposal(task_summary="Other task."))
    with pytest.raises(ValueError, match="different proposal revision"):
        validate_decision_for_proposal(decision, proposal, expected_revision=1)
    with pytest.raises(ValidationError):
        WorkflowDecision(action="approve", proposal_fingerprint=proposal_fingerprint(proposal),
                         proposal_revision=0, decision_timestamp=datetime(2026, 11, 17))
    with pytest.raises(ValidationError):
        new_workflow_decision(proposal, action="request_changes")
