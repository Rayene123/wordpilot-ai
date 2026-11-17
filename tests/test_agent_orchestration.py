from __future__ import annotations

from pathlib import Path

from docx import Document
import pytest

from backend.docx_editor.integrity import IntegrityCheck, IntegrityReport
from backend.agent import (
    OperationPlanner,
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


def test_real_planner_gets_compact_context_while_proposal_keeps_full_inspection(make_docx):
    import json

    from backend.docx_editor import inspect_document

    source = make_docx()

    class CapturingProvider:
        messages = None

        def generate(self, messages, schema):
            self.messages = messages
            return {"status": "proposed", "message": "Replace the selected word.", "operations": [
                {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated"},
            ]}

    provider = CapturingProvider()
    proposal = plan_document(source, "Review the document", provider=provider)
    planner_text = provider.messages[1].content
    compact_json = planner_text.split("Untrusted document inspection summary (JSON):\n", 1)[1]
    compact = json.loads(compact_json)
    full = json.loads(json.dumps(inspect_document(source).to_dict(), ensure_ascii=False))

    assert proposal["planning_status"] == "awaiting_approval"
    assert proposal["document_summary"] == full
    assert "runs" in proposal["document_summary"]["paragraphs"][0]
    assert "tables" in proposal["document_summary"]
    assert "Cell target value" in json.dumps(compact)
    assert planner_text.count("Cell target value") == 1
    assert compact["blocks"][0] == {"p": "Keep old text end"}
    assert compact["blocks"][1] == {"p": "Unrelated paragraph"}


def test_exceptionally_large_document_returns_controlled_planning_status(make_docx):
    source = make_docx()
    document = Document(source)
    document.add_paragraph("Large paragraph " + "content " * 3000)
    document.save(source)

    class NeverCalledProvider:
        messages = None

        def generate(self, messages, schema):
            self.messages = messages
            raise AssertionError("oversized context must be stopped before provider invocation")

    provider = NeverCalledProvider()
    result = plan_document(source, "Review this document", provider=provider)

    assert result["planning_status"] == "too_large"
    assert "too large to plan safely" in result["final_response"]
    assert provider.messages is None


def test_approval_executes_and_preserves_original(make_docx, tmp_path):
    source = make_docx()
    original = source.read_bytes()
    proposal = plan_document(source, "Change Unrelated to Updated", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["approval_status"] == "approved"
    assert result["execution_status"] == "completed_verified"
    assert result["integrity_status"] == "passed"
    assert result["integrity_report"]["status"] == "passed"
    assert result["execution_results"][0]["success"] is True
    assert Document(result["output_path"]).paragraphs[1].text == "Updated paragraph"
    assert source.read_bytes() == original
    assert result["output_path"] != str(source)


def test_unicode_proposal_integrity_check_accepts_complex_document(make_docx, tmp_path):
    source = make_docx()
    document = Document(source)
    document.add_paragraph("Résumé — café")
    document.save(source)
    proposal = plan_document(source, "Change Unrelated to Updated", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "completed_verified"
    assert Document(result["output_path"]).paragraphs[-1].text == "Résumé — café"


def test_rejection_does_not_create_output(make_docx, tmp_path):
    source = make_docx()
    proposal = plan_document(source, "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=False, output_directory=tmp_path)

    assert result["approval_status"] == "rejected"
    assert result["execution_status"] == "rejected"
    assert result["integrity_status"] == "not_run"
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_missing_approval_does_not_create_output(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, output_directory=tmp_path)

    assert result["approval_status"] == "pending"
    assert result["execution_status"] == "pending"
    assert result["integrity_status"] == "not_run"
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_malformed_proposal_is_rejected_without_execution(tmp_path):
    result = execute_approved_proposal(None, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
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

    assert result["execution_status"] == "execution_failed"
    assert "changed" in result["errors"][0]
    assert result["output_path"] is None
    assert not list(tmp_path.glob("*_edited_*.docx"))


@pytest.mark.parametrize("approval", [1, "yes", [], object()])
def test_truthy_non_boolean_approval_is_not_accepted(make_docx, tmp_path, approval):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))

    result = execute_approved_proposal(proposal, approved=approval, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
    assert result["approval_status"] == "pending"
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_schema_invalid_operation_is_rejected_before_execution(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    proposal["proposed_operations"][0]["operation_type"] = "run_python"

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
    assert "changed" in result["errors"][0].lower()
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_caller_cannot_mint_a_valid_marker_for_an_arbitrary_proposal(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    proposal["proposed_operations"] = [{
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Caller edit",
    }]
    proposal["proposal_digest"] = ""

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
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

    assert result["execution_status"] == "execution_failed"
    assert "changed after planning" in result["errors"][0]
    assert result["output_path"] is None
    assert source.read_bytes() == changed_original
    assert changed_original != original
    assert not list(tmp_path.glob("*_edited_*.docx"))


def test_issued_proposal_token_is_one_time(make_docx, tmp_path):
    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    first = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)
    second = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert first["execution_status"] == "completed_verified"
    assert second["execution_status"] == "execution_failed"
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
    assert result["execution_status"] == "execution_failed"
    assert result["errors"] == ["Operation 1 failed: simulated editor failure"]
    assert source.read_bytes() == original


def _integrity_report(status, *, unverified=(), unexpected=(), summary=None):
    """Build a small verifier report fixture for orchestration boundary tests."""
    return IntegrityReport(
        status=status,
        checks=[IntegrityCheck(name="synthetic", status=status, details=["test result"])],
        unexpected_changes=list(unexpected),
        expected_changes=[],
        unverified_features=list(unverified),
        errors=[],
        summary=summary or f"synthetic {status} report",
    )


def test_verifier_receives_original_output_and_exact_approved_operations(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    source = make_docx()
    proposal = plan_document(source, "Change it", planner=MockPlanner(_replace_result()))
    calls = []

    def verifier(original_path, edited_path, operations):
        calls.append((original_path, edited_path, operations))
        return _integrity_report("passed")

    monkeypatch.setattr(orchestration, "verify_document_integrity", verifier)
    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert len(calls) == 1
    original_path, edited_path, operations = calls[0]
    assert original_path == source.resolve()
    assert edited_path == Path(result["output_path"]).resolve()
    assert edited_path != original_path
    assert operations == proposal["proposed_operations"] == result["approved_operations"]
    assert result["execution_status"] == "completed_verified"


def test_partial_verification_returns_completed_partially_verified(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: _integrity_report(
        "partially_verified", unverified=["image placement and cropping"],
    ))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "completed_partially_verified"
    assert result["integrity_status"] == "partially_verified"
    assert "image placement and cropping" in result["final_response"]
    assert Document(result["output_path"]).paragraphs[1].text == "Updated paragraph"


def test_integrity_failure_preserves_output_and_marks_it_unverified(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: _integrity_report(
        "failed", unexpected=["unexpected table change"], summary="unexpected table change",
    ))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "integrity_failed"
    assert result["integrity_status"] == "failed"
    assert result["integrity_report"]["unexpected_changes"] == ["unexpected table change"]
    assert "unverified" in result["final_response"].lower()
    assert Document(result["output_path"]).paragraphs[1].text == "Updated paragraph"
    assert Path(result["output_path"]).is_file()


def test_inconsistent_pass_report_is_never_mapped_to_success(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    inconsistent = _integrity_report("passed", unexpected=["unexpected content"])
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: inconsistent)

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "integrity_failed"
    assert result["integrity_status"] == "failed"
    assert "unexpected content" in result["integrity_report"]["unexpected_changes"]


def test_verifier_exception_becomes_integrity_failure_and_keeps_output(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))

    def broken_verifier(*args):
        raise RuntimeError("verifier crashed")

    monkeypatch.setattr(orchestration, "verify_document_integrity", broken_verifier)
    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "integrity_failed"
    assert result["integrity_status"] == "failed"
    assert "verifier crashed" in result["integrity_report"]["errors"][0]
    assert Path(result["output_path"]).is_file()
    assert "unverified" in result["final_response"].lower()


def test_rejection_never_invokes_verifier(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: pytest.fail("must not verify"))

    result = execute_approved_proposal(proposal, approved=False, output_directory=tmp_path)

    assert result["execution_status"] == "rejected"
    assert result["integrity_status"] == "not_run"


def test_modified_proposal_never_invokes_verifier(make_docx, tmp_path, monkeypatch):
    import backend.agent.orchestration as orchestration

    proposal = plan_document(make_docx(), "Change it", planner=MockPlanner(_replace_result()))
    proposal["user_instruction"] = "Tampered instruction"
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: pytest.fail("must not verify"))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
    assert result["integrity_status"] == "not_run"


def test_partial_editor_output_is_preserved_but_not_verified(make_docx, tmp_path, monkeypatch):
    import shutil
    import backend.agent.orchestration as orchestration

    source = make_docx()
    proposal = plan_document(source, "Change it", planner=MockPlanner(_replace_result()))
    partial_output = tmp_path / "partial.docx"
    shutil.copyfile(source, partial_output)

    def partial_executor(state, output_directory):
        return {
            "execution_results": [{"success": True}, {"success": False}],
            "warnings": [], "errors": ["second operation failed"],
            "output_path": str(partial_output), "workflow_status": "partial",
        }

    monkeypatch.setattr(orchestration, "_execute_node", partial_executor)
    monkeypatch.setattr(orchestration, "verify_document_integrity", lambda *args: pytest.fail("partial output must not be verified"))

    result = execute_approved_proposal(proposal, approved=True, output_directory=tmp_path)

    assert result["execution_status"] == "execution_failed"
    assert result["integrity_status"] == "not_run"
    assert result["output_path"] == str(partial_output)
    assert Path(result["output_path"]).is_file()
    assert "unverified" in result["final_response"].lower()
