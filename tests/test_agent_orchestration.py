from __future__ import annotations

from docx import Document
import pytest

from backend.agent import (
    PlanningResult,
    execute_approved_proposal,
    plan_document,
)


class MockPlanner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def plan(self, instruction, document_summary):
        self.calls.append((instruction, document_summary))
        return self.result


def _replace_result():
    return PlanningResult(
        status="proposed",
        message="Replace the selected word.",
        operations=[{
            "operation_type": "replace",
            "target_text": "Unrelated",
            "replacement_text": "Updated",
        }],
    )


def test_planning_returns_approval_proposal_without_output(make_docx, tmp_path):
    source = make_docx()
    original = source.read_bytes()
    planner = MockPlanner(_replace_result())

    proposal = plan_document(source, "Change Unrelated to Updated", planner=planner)

    assert proposal["planning_status"] == "awaiting_approval"
    assert proposal["approval_status"] == "pending"
    assert proposal["execution_status"] == "not_executed"
    assert proposal["proposed_operations"][0]["target_text"] == "Unrelated"
    assert proposal["proposal_digest"]
    assert proposal["document_summary"]["paragraphs"][1]["text"] == "Unrelated paragraph"
    assert planner.calls
    assert source.read_bytes() == original
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_approval_executes_and_preserves_original(make_docx, tmp_path):
    source = make_docx()
    original = source.read_bytes()
    proposal = plan_document(source, "Change Unrelated to Updated", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["approval_status"] == "approved"
    assert result["execution_status"] == "completed"
    assert result["execution_results"][0]["success"] is True
    assert Document(result["output_path"]).paragraphs[1].text == "Updated paragraph"
    assert source.read_bytes() == original
    assert result["output_path"] != str(source)


def test_rejection_does_not_create_output(make_docx, tmp_path):
    source = make_docx()
    proposal = plan_document(source, "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=False, output_directory=tmp_path)

    assert result["approval_status"] == "rejected"
    assert result["execution_status"] == "not_executed"
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_missing_approval_does_not_create_output(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, output_directory=tmp_path)

    assert result["approval_status"] == "pending"
    assert result["execution_status"] == "not_executed"
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_malformed_proposal_is_rejected_without_execution(tmp_path):
    result = execute_approved_proposal(None, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert "mapping" in result["errors"][0]
    assert not list(tmp_path.glob("*_edited_*.docx"))


@pytest.mark.parametrize("field, mutation", [
    ("proposed_operations", lambda proposal: proposal["proposed_operations"][0].update(replacement_text="Tampered")),
    ("document_path", lambda proposal: proposal.update(document_path="other.docx")),
    ("user_instruction", lambda proposal: proposal.update(user_instruction="Different instruction")),
    ("document_summary", lambda proposal: proposal["document_summary"].update(paragraph_count=999)),
    ("validated_operations", lambda proposal: proposal["validated_operations"][0].update(replacement_text="Tampered")),
    ("final_response", lambda proposal: proposal.update(final_response="Different presented text")),
])
def test_modified_security_relevant_proposal_fields_are_rejected(make_docx, tmp_path, field, mutation):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    mutation(proposal)

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert "changed" in result["errors"][0]
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


@pytest.mark.parametrize("approval", [1, "yes", [], object()])
def test_truthy_non_boolean_approval_is_not_accepted(make_docx, tmp_path, approval):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=approval, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert result["approval_status"] == "pending"
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_schema_invalid_operation_is_rejected_before_execution(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    proposal["proposed_operations"][0]["operation_type"] = "run_python"

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert "changed" in result["errors"][0].lower()
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_caller_cannot_mint_a_valid_marker_for_an_arbitrary_proposal(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    proposal["proposed_operations"] = [{
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Caller edit",
    }]
    proposal["proposal_digest"] = ""

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert "changed" in result["errors"][0]
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_clarification_does_not_execute(make_docx, tmp_path):
    clarification = PlanningResult(
        status="clarification_required", message="Which paragraph?", operations=[],
    )
    proposal = plan_document(make_docx(), "Remove the note", planner=MockPlanner(clarification))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert proposal["planning_status"] == "needs_clarification"
    assert proposal["clarification_request"] == "Which paragraph?"
    assert result["execution_status"] == "not_executed"
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_unsupported_request_does_not_execute(make_docx, tmp_path):
    unsupported = PlanningResult(status="unsupported", message="Formatting is unsupported.", operations=[])
    proposal = plan_document(make_docx(), "Make it blue", planner=MockPlanner(unsupported))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert proposal["planning_status"] == "unsupported"
    assert result["execution_status"] == "not_executed"
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_execution_failure_reports_failed_and_no_original_change(make_docx, tmp_path):
    source = make_docx()
    original = source.read_bytes()
    invalid_target_result = PlanningResult(
        status="proposed", message="Replace missing text.", operations=[{
            "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated",
        }],
    )
    proposal = plan_document(source, "Change Unrelated", planner=MockPlanner(invalid_target_result))
    doc = Document(source)
    doc.paragraphs[1].text = "Changed elsewhere"
    doc.save(source)
    changed_original = source.read_bytes()

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "failed"
    assert "changed after planning" in result["errors"][0]
    assert result["output_path"] is None
    assert source.read_bytes() == changed_original
    assert changed_original != original
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_issued_proposal_token_is_one_time(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    first = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)
    second = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert first["execution_status"] == "completed"
    assert second["execution_status"] == "failed"
    assert "content changed" in second["errors"][0]
    assert len(list(tmp_path.glob("*_edited_*.docx"))) == 1


def test_deterministic_execution_failure_is_returned_without_source_change(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    source = make_docx()
    original = source.read_bytes()
    proposal = plan_document(source, "Change it", planner=MockPlanner(_replace_result()))
    calls = []

    def failed_executor(state, output_directory):
        calls.append((state, output_directory))
        return {
            "execution_results": [{"success": False, "error": "simulated editor failure"}],
            "warnings": [],
            "errors": ["Operation 1 failed: simulated editor failure"],
            "output_path": None,
            "workflow_status": "failed",
        }

    monkeypatch.setattr(orchestration, "_execute_node", failed_executor)
    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert calls
    assert result["execution_status"] == "failed"
    assert result["errors"] == ["Operation 1 failed: simulated editor failure"]
    assert source.read_bytes() == original
