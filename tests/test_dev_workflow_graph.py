from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import UnknownInterruptError
from langgraph.types import Command

from backend.dev_workflow.graph import (
    build_dev_workflow,
    new_workflow_state,
    workflow_config,
)
from backend.dev_workflow.schemas import (
    CodexExecutionResult,
    TaskProposal,
    new_approval,
)


def _proposal(**updates) -> TaskProposal:
    values = {
        "task_description": "Add an explicit API health detail.",
        "requirements": ["Keep current health fields."],
        "acceptance_criteria": ["Health response contains the detail."],
        "implementation_plan": ["Update the health response and its API test."],
        "risks": ["Response consumers may rely on exact shape."],
        "proposed_files": ["backend/api.py", "tests/test_api.py"],
        "excluded_files": ["frontend/"],
        "assumptions": ["The existing endpoint remains synchronous."],
    }
    return TaskProposal.model_validate({**values, **updates})


def _execution(status="succeeded", error=None) -> CodexExecutionResult:
    return CodexExecutionResult(
        command_identity=["codex", "exec", "--sandbox", "read-only"],
        exit_code=0 if status == "succeeded" else 1,
        stdout="analysis event", stderr="", last_message="proposal",
        parsed_result=None, duration_seconds=0.1, status=status,
        timed_out=False, error=error,
    )


def _setup(tmp_path, *, proposal=None, execution=None):
    calls = []

    def analyzer(task_description, repository_path):
        calls.append((task_description, repository_path))
        return execution or _execution(), proposal or _proposal()

    saver = InMemorySaver()
    graph = build_dev_workflow(analyzer=analyzer, checkpointer=saver)
    state = new_workflow_state("Add an explicit API health detail", tmp_path)
    config = workflow_config(state["run_id"])
    return graph, state, config, calls


def _pause_for_approval(graph, state, config):
    result = graph.invoke(state, config)
    snapshot = graph.get_state(config)
    return result, snapshot


def _interrupt_value(snapshot):
    return snapshot.tasks[0].interrupts[0].value


def test_successful_analysis_validates_proposal_and_pauses_for_approval(tmp_path):
    graph, state, config, calls = _setup(tmp_path)
    result, snapshot = _pause_for_approval(graph, state, config)

    assert result["final_status"] == "awaiting_approval"
    assert result["proposal"]["requirements"] == ["Keep current health fields."]
    assert result["proposal_fingerprint"]
    assert result["analysis_evidence"]["status"] == "succeeded"
    assert calls == [(state["task_description"], str(tmp_path.resolve()))]
    assert snapshot.next == ("request_approval",)
    request = _interrupt_value(snapshot)
    assert request["proposal"] == result["proposal"]
    assert request["proposal_fingerprint"] == result["proposal_fingerprint"]
    for field in (
        "requirements", "acceptance_criteria", "implementation_plan", "risks",
        "proposed_files", "excluded_files", "assumptions",
    ):
        assert field in request["proposal"]


def test_checkpoint_thread_must_match_unique_run_id(tmp_path):
    graph, state, _, calls = _setup(tmp_path)
    result = graph.invoke(state, workflow_config("another-run"))
    assert result["final_status"] == "failed"
    assert "thread ID" in result["errors"][0]
    assert calls == []


def test_invalid_proposal_scope_stops_before_approval(tmp_path):
    graph, state, config, _ = _setup(tmp_path, proposal=_proposal(proposed_files=["../outside.py"]))
    result = graph.invoke(state, config)
    assert result["final_status"] == "failed"
    assert result["current_stage"] == "failed"
    assert any("safe relative path" in error for error in result["validation_errors"])
    assert graph.get_state(config).next == ()


def test_invalid_scope_conflict_and_empty_scope_are_rejected(tmp_path):
    for proposal in (
        _proposal(proposed_files=[]),
        _proposal(proposed_files=["backend/api.py"], excluded_files=["backend/"]),
    ):
        graph, state, config, _ = _setup(tmp_path, proposal=proposal)
        result = graph.invoke(state, config)
        assert result["final_status"] == "failed"
        assert graph.get_state(config).next == ()


def test_codex_execution_failure_stops_before_validation_and_approval(tmp_path):
    graph, state, config, _ = _setup(
        tmp_path, proposal=None, execution=_execution("failed", "Codex failed safely."),
    )
    result = graph.invoke(state, config)
    assert result["final_status"] == "failed"
    assert result["analysis_evidence"]["status"] == "failed"
    assert result["proposal"] is None
    assert graph.get_state(config).next == ()


def test_explicit_approval_resumes_with_same_checkpoint_thread(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    result, snapshot = _pause_for_approval(graph, state, config)
    proposal = TaskProposal.model_validate(result["proposal"])
    approval = new_approval(proposal, approved=True).model_dump(mode="json")

    completed = graph.invoke(Command(resume=approval), config)
    assert completed["final_status"] == "approved"
    assert completed["current_stage"] == "approved"
    assert completed["approval"]["approved"] is True
    assert graph.get_state(config).next == ()
    assert snapshot.config["configurable"]["thread_id"] == state["run_id"]


def test_explicit_rejection_ends_workflow(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    result, _ = _pause_for_approval(graph, state, config)
    rejection = new_approval(TaskProposal.model_validate(result["proposal"]), approved=False)

    completed = graph.invoke(Command(resume=rejection.model_dump(mode="json")), config)
    assert completed["final_status"] == "rejected"
    assert completed["current_stage"] == "rejected"
    assert completed["approval"]["approved"] is False


def test_missing_approval_cannot_advance(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    _, snapshot = _pause_for_approval(graph, state, config)
    assert snapshot.next == ("request_approval",)

    with pytest.raises(UnknownInterruptError) as error:
        graph.invoke(Command(resume={}), config)
    assert error.value.reason == "empty_map"

    # The empty map is rejected without consuming or advancing the approval
    # interrupt, so an explicit decision can still be supplied afterward.
    current = graph.get_state(config)
    assert current.values["final_status"] == "awaiting_approval"
    assert current.next == ("request_approval",)


def test_malformed_approval_fails_closed(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    _pause_for_approval(graph, state, config)

    failed = graph.invoke(Command(resume={"approved": "yes"}), config)
    assert failed["final_status"] == "failed"
    assert failed["current_stage"] == "failed"
    assert graph.get_state(config).next == ()


def test_incorrect_proposal_fingerprint_is_rejected(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    result, _ = _pause_for_approval(graph, state, config)
    approval = new_approval(TaskProposal.model_validate(result["proposal"]), approved=True)
    payload = approval.model_dump(mode="json")
    payload["proposal_fingerprint"] = "0" * 64

    failed = graph.invoke(Command(resume=payload), config)
    assert failed["final_status"] == "failed"
    assert "stale" in failed["errors"][0]


def test_modified_proposal_cannot_use_old_approval_fingerprint(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    result, _ = _pause_for_approval(graph, state, config)
    approval = new_approval(TaskProposal.model_validate(result["proposal"]), approved=True)
    modified = _proposal(task_description="A modified proposal.")
    graph.update_state(config, {"proposal": modified.model_dump(mode="json")})

    failed = graph.invoke(Command(resume=approval.model_dump(mode="json")), config)
    assert failed["final_status"] == "failed"
    assert "modified proposal" in failed["errors"][0]


def test_implementation_is_not_part_of_the_graph(tmp_path):
    graph, _, _, _ = _setup(tmp_path)
    node_names = set(graph.get_graph().nodes)
    assert {"analyze_task", "validate_proposal", "request_approval"}.issubset(node_names)
    assert not any("implement" in name.lower() for name in node_names)
    assert not any("worktree" in name.lower() for name in node_names)


def test_workflow_state_is_json_serializable_and_checkpoint_resumable(tmp_path):
    graph, state, config, _ = _setup(tmp_path)
    initial_json = json.dumps(state)
    assert json.loads(initial_json)["run_id"] == state["run_id"]

    result, snapshot = _pause_for_approval(graph, state, config)
    # invoke() includes LangGraph runtime interrupt metadata, which is not part
    # of the JSON-friendly application workflow state.
    json.dumps(snapshot.values)
    assert snapshot.values["final_status"] == "awaiting_approval"
    approval = new_approval(TaskProposal.model_validate(result["proposal"]), approved=True)
    resumed = graph.invoke(Command(resume=approval.model_dump(mode="json")), config)

    assert resumed["final_status"] == "approved"
    assert graph.get_state(config).values["run_id"] == state["run_id"]
