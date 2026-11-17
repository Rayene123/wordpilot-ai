from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dev_workflow.checkpointing import (
    ApprovalResumeError,
    CheckpointStoreError,
    open_sqlite_checkpointer,
    resume_pending_approval,
)
from dev_workflow.graph import build_dev_workflow, new_workflow_state, workflow_config
from dev_workflow.schemas import (
    CodexExecutionResult,
    DiffReport,
    TaskProposal,
    new_workflow_decision,
)


def _proposal(summary: str) -> TaskProposal:
    return TaskProposal.model_validate({
        "task_summary": summary,
        "requirements": ["Preserve the current API response."],
        "acceptance_criteria": ["Health endpoint remains compatible."],
        "implementation_steps": [{"order": 1, "description": "Update the endpoint."}],
        "expected_files": [{
            "path": "backend/api.py", "action": "modify", "rationale": "Documented test target."
        }],
        "file_scope": {"included_files": ["backend/api.py"], "excluded_files": []},
        "technical_decisions": [{
            "decision": "Retain the existing API shape", "rationale": "Compatibility is required."
        }],
        "testing_strategy": ["Run the focused API tests."],
    })


def _execution() -> CodexExecutionResult:
    return CodexExecutionResult(
        command_identity=["test-analyzer"], exit_code=0, stdout="", stderr="",
        last_message=None, parsed_result=None, duration_seconds=0,
        status="succeeded", timed_out=False,
    )


def _callbacks(tmp_path, first: TaskProposal, revised: TaskProposal):
    target = tmp_path / "backend" / "api.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing", encoding="utf-8")
    implementations = []

    def analyzer(task, repository, *, feedback=None, previous_proposal=None):
        return _execution(), revised if feedback else first

    def implementer(proposal, fingerprint, worktree, *, proposal_revision=0):
        implementations.append((proposal, fingerprint, proposal_revision))
        return _execution()

    def diff_collector(worktree, proposal):
        return DiffReport(
            status="valid", changed_files=[{"path": "backend/api.py", "action": "modify"}],
            diff="+ change", worktree_path=str(worktree),
        )

    def worktree_creator(repository, run_id):
        return str(tmp_path / ".devflow-worktrees" / run_id)

    return analyzer, implementer, worktree_creator, diff_collector, implementations


def _reach_revision_one_pause(tmp_path, database):
    first = _proposal("Initial API proposal.")
    revised = _proposal("Revised API proposal.")
    callbacks = _callbacks(tmp_path, first, revised)
    analyzer, implementer, worktree_creator, diff_collector, implementations = callbacks
    state = new_workflow_state("Keep the API compatible.", tmp_path, max_plan_revisions=1)
    config = workflow_config(state["run_id"])
    with open_sqlite_checkpointer(database) as saver:
        graph = build_dev_workflow(
            analyzer=analyzer, implementer=implementer, worktree_creator=worktree_creator,
            diff_collector=diff_collector, checkpointer=saver, max_plan_revisions=1,
        )
        first_result = graph.invoke(state, config)
        assert first_result["final_status"] == "awaiting_approval"
        revision = new_workflow_decision(
            first, action="request_changes", feedback="Clarify the plan.", proposal_revision=0,
        )
        second_result = graph.invoke(Command(resume=revision.model_dump(mode="json")), config)
        assert second_result["final_status"] == "awaiting_approval"
        assert second_result["revision_count"] == 1
    return state["run_id"], first, revised, callbacks, implementations


def test_sqlite_checkpoint_survives_closed_saver_and_resumes_same_interrupt(tmp_path):
    database = tmp_path / ".devflow" / "checkpoints.sqlite3"
    run_id, first, revised, callbacks, implementations = _reach_revision_one_pause(tmp_path, database)
    analyzer, implementer, worktree_creator, diff_collector, _ = callbacks

    with open_sqlite_checkpointer(database) as saver:
        graph = build_dev_workflow(
            analyzer=analyzer, implementer=implementer, worktree_creator=worktree_creator,
            diff_collector=diff_collector, checkpointer=saver, max_plan_revisions=1,
        )
        config = workflow_config(run_id)
        snapshot = graph.get_state(config)
        assert snapshot.values["run_id"] == run_id
        assert snapshot.values["current_stage"] == "awaiting_approval"
        assert snapshot.values["revision_count"] == 1
        assert snapshot.values["proposal"] == revised.model_dump(mode="json")
        assert len(snapshot.values["proposal_history"]) == 1
        assert snapshot.values["proposal_history"][0]["proposal"] == first.model_dump(mode="json")
        assert snapshot.values["decisions"][0]["action"] == "request_changes"
        assert snapshot.next == ("request_approval",)

        approval = new_workflow_decision(
            revised, action="approve", proposal_revision=1,
        ).model_dump(mode="json")
        result = resume_pending_approval(graph, run_id, approval)

    assert result["final_status"] == "implemented"
    assert result["decisions"][-1]["proposal_revision"] == 1
    assert len(implementations) == 1
    assert implementations[0][1] == result["proposal_fingerprint"]
    assert implementations[0][2] == 1


@pytest.mark.parametrize("decision_kind", ["stale", "malformed"])
def test_invalid_or_stale_resume_keeps_sqlite_approval_interrupt_pending(tmp_path, decision_kind):
    database = tmp_path / "checkpoints.sqlite3"
    run_id, first, revised, callbacks, implementations = _reach_revision_one_pause(tmp_path, database)
    analyzer, implementer, worktree_creator, diff_collector, _ = callbacks

    if decision_kind == "stale":
        decision = new_workflow_decision(first, action="approve", proposal_revision=0).model_dump(mode="json")
    else:
        decision = {"action": "approve", "proposal_fingerprint": "invalid"}

    with open_sqlite_checkpointer(database) as saver:
        graph = build_dev_workflow(
            analyzer=analyzer, implementer=implementer, worktree_creator=worktree_creator,
            diff_collector=diff_collector, checkpointer=saver, max_plan_revisions=1,
        )
        config = workflow_config(run_id)
        with pytest.raises(ApprovalResumeError):
            resume_pending_approval(graph, run_id, decision)
        snapshot = graph.get_state(config)
        assert snapshot.values["final_status"] == "awaiting_approval"
        assert snapshot.next == ("request_approval",)

        valid = new_workflow_decision(revised, action="reject", proposal_revision=1)
        result = resume_pending_approval(graph, run_id, valid)

    assert result["final_status"] == "rejected"
    assert not implementations


def test_missing_checkpoint_is_rejected_without_creating_a_run(tmp_path):
    database = tmp_path / "empty.sqlite3"
    with open_sqlite_checkpointer(database) as saver:
        graph = build_dev_workflow(checkpointer=saver)
        decision = {"action": "approve"}
        with pytest.raises(ApprovalResumeError, match="No saved workflow"):
            resume_pending_approval(graph, "missing-run", decision)


def test_corrupt_sqlite_checkpoint_is_reported_without_fallback(tmp_path):
    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(CheckpointStoreError, match="unavailable, corrupt, or incompatible"):
        with open_sqlite_checkpointer(database):
            pytest.fail("Corrupt checkpoint database must not be opened.")


def test_incompatible_saved_revision_is_rejected_before_interrupt_resume(tmp_path):
    database = tmp_path / "checkpoints.sqlite3"
    run_id, _, revised, callbacks, implementations = _reach_revision_one_pause(tmp_path, database)
    analyzer, implementer, worktree_creator, diff_collector, _ = callbacks

    with open_sqlite_checkpointer(database) as saver:
        graph = build_dev_workflow(
            analyzer=analyzer, implementer=implementer, worktree_creator=worktree_creator,
            diff_collector=diff_collector, checkpointer=saver, max_plan_revisions=1,
        )
        config = workflow_config(run_id)
        # Simulate an incompatible persisted state shape from an older workflow.
        graph.update_state(config, {"revision_count": "invalid"})
        approval = new_workflow_decision(
            revised, action="approve", proposal_revision=1,
        ).model_dump(mode="json")
        with pytest.raises(ApprovalResumeError, match="interrupt is missing or ambiguous"):
            resume_pending_approval(graph, run_id, approval)

    assert not implementations


def test_graph_requires_an_explicit_checkpointer():
    with pytest.raises(ValueError, match="explicit checkpointer"):
        build_dev_workflow()


def test_in_memory_saver_remains_available_for_explicit_isolated_use():
    graph = build_dev_workflow(checkpointer=InMemorySaver())
    assert graph is not None
