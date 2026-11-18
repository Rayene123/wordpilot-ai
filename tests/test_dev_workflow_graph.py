from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import UnknownInterruptError
from langgraph.types import Command

from dev_workflow.graph import build_dev_workflow, new_workflow_state, workflow_config
from dev_workflow.schemas import (
    CodexExecutionResult, DiffReport, TaskProposal, VerificationCheckResult,
    new_workflow_decision, proposal_fingerprint,
)
from dev_workflow.worktree import create_worktree


def _proposal(summary="Add an API health detail.", **updates):
    return TaskProposal.model_validate({
        "task_summary": summary,
        "requirements": ["Keep current health fields."],
        "acceptance_criteria": ["Health response contains the detail."],
        "implementation_steps": [{"order": 1, "description": "Update endpoint and test."}],
        "expected_files": [
            {"path": "backend/api.py", "action": "modify", "rationale": "Add field."},
            {"path": "tests/test_api.py", "action": "modify", "rationale": "Cover field."},
        ],
        "file_scope": {"included_files": ["backend/api.py", "tests/test_api.py"]},
        "technical_decisions": [{"decision": "Keep endpoint sync", "rationale": "Existing convention."}],
        "risks": ["Response consumers may rely on exact shape."],
        "edge_cases": ["Existing health fields remain present."],
        "testing_strategy": ["Run the focused API tests."],
        "assumptions": [],
        **updates,
    })


def _execution(status="succeeded", error=None):
    return CodexExecutionResult(
        command_identity=["codex", "exec", "--sandbox", "read-only"],
        exit_code=0 if status == "succeeded" else None if status == "timeout" else 1, stdout="analysis", stderr="",
        last_message="proposal", parsed_result=None, duration_seconds=0.1,
        status=status, timed_out=status == "timeout", error=error,
    )


def _passing_checks(worktree):
    return [VerificationCheckResult(
        check_id=check_id, display_name=check_id,
        command_identity=["python", "-m", "pytest", "-q"] if check_id == "python.tests" else ["npm", "run", "typecheck"],
        working_directory="worktree",
        working_directory_identity="." if check_id == "python.tests" else "frontend",
        status="passed", exit_code=0, duration_seconds=0.1,
    ) for check_id in ("python.tests", "frontend.typecheck")]


def _setup(tmp_path, *, proposals=None, execution=None, implementation_status="succeeded",
           diff_status="valid", max_revisions=2, verification_runner=_passing_checks,
           worktree_factory=None):
    calls, implementations, worktrees, diffs = [], [], [], []
    proposals = proposals or [_proposal()]
    for candidate in proposals:
        try:
            typed = TaskProposal.model_validate(candidate)
        except Exception:
            continue
        for expected in typed.expected_files:
            if expected.action in {"modify", "delete"}:
                path = tmp_path / expected.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("existing fixture content", encoding="utf-8")

    (tmp_path / ".gitignore").write_text(".devflow-worktrees/\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, shell=False)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "devflow-test@example.invalid"], check=True, shell=False)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "DevFlow Test"], check=True, shell=False)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, shell=False)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--quiet", "-m", "base"], check=True, shell=False)

    def analyzer(task, repository, *, feedback=None, previous_proposal=None):
        calls.append((task, repository, feedback, previous_proposal))
        proposal = proposals[min(len(calls) - 1, len(proposals) - 1)]
        return execution or _execution(), proposal

    def implementer(proposal, fingerprint, worktree, *, proposal_revision=0):
        implementations.append((proposal, fingerprint, worktree, proposal_revision))
        return _execution(implementation_status)

    def worktree_creator(repository, run_id):
        value = (worktree_factory or create_worktree)(repository, run_id)
        worktrees.append(value)
        return value

    def diff_collector(worktree, proposal):
        diffs.append((worktree, proposal))
        return DiffReport(status=diff_status, changed_files=[
            {"path": "backend/api.py", "action": "modify"},
            {"path": "tests/test_api.py", "action": "modify"},
        ] if diff_status != "not_collected" else [],
            errors=["Scope validation failed."] if diff_status == "invalid" else [],
            diff="+ health detail", worktree_path=worktree)

    saver = InMemorySaver()
    graph = build_dev_workflow(analyzer=analyzer, implementer=implementer,
                               worktree_creator=worktree_creator, diff_collector=diff_collector,
                               verification_runner=verification_runner,
                               checkpointer=saver, max_plan_revisions=max_revisions)
    state = new_workflow_state("Add an explicit API health detail", tmp_path, max_plan_revisions=max_revisions)
    return graph, state, workflow_config(state["run_id"]), calls, implementations, worktrees, diffs


def _pause(graph, state, config):
    result = graph.invoke(state, config)
    return result, graph.get_state(config)


def _request(snapshot):
    return snapshot.tasks[0].interrupts[0].value


def _decision(result, action="approve", feedback=None):
    return new_workflow_decision(TaskProposal.model_validate(result["proposal"]), action=action,
                                 feedback=feedback,
                                 proposal_revision=result.get("revision_count", 0)).model_dump(mode="json")


def test_full_proposal_is_returned_as_structured_interrupt_state(tmp_path):
    graph, state, config, calls, implementations, *_ = _setup(tmp_path)
    result, snapshot = _pause(graph, state, config)
    assert result["final_status"] == "awaiting_approval"
    assert result["proposal"]["task_summary"] == "Add an API health detail."
    assert result["proposal_fingerprint"]
    request = _request(snapshot)
    assert request["proposal"] == result["proposal"]
    assert request["proposal_fingerprint"] == proposal_fingerprint(TaskProposal.model_validate(request["proposal"]))
    assert {"requirements", "acceptance_criteria", "implementation_steps", "expected_files",
            "file_scope", "technical_decisions", "risks", "edge_cases", "testing_strategy"} <= set(request["proposal"])
    assert calls and not implementations


def test_approval_is_required_before_isolated_implementation(tmp_path):
    graph, state, config, _, implementations, worktrees, diffs = _setup(tmp_path)
    result, _ = _pause(graph, state, config)
    assert implementations == worktrees == diffs == []
    done = graph.invoke(Command(resume=_decision(result)), config)
    assert done["final_status"] == "passed"
    assert done["final_report"]["overall_status"] == "passed"
    assert done["diff_report"]["status"] == "valid"
    assert len(implementations) == len(worktrees) == len(diffs) == 1


def test_preflight_is_not_a_required_implementation_gate(tmp_path):
    graph, state, config, _, implementations, worktrees, diffs = _setup(tmp_path)
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "passed"
    assert implementations and worktrees and diffs
    assert done["final_report"]["overall_status"] == "passed"


@pytest.mark.parametrize("invalid_path", ["repository", "outside"])
def test_implementation_rejects_non_worktree_paths_before_implementer(tmp_path, invalid_path):
    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    selected = tmp_path if invalid_path == "repository" else outside
    graph, state, config, _, implementations, *_ = _setup(
        tmp_path, worktree_factory=lambda repository, run_id: str(selected),
    )
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["overall_status"] == "failed"
    assert implementations == []


def test_valid_registered_worktree_reaches_implementer(tmp_path):
    graph, state, config, _, implementations, worktrees, *_ = _setup(tmp_path)
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "passed"
    assert implementations and worktrees
    assert Path(worktrees[0]).parent == tmp_path / ".devflow-worktrees"


def test_implementation_failure_is_reported_without_verification(tmp_path):
    calls = []
    graph, state, config, *_ = _setup(
        tmp_path, implementation_status="failed",
        verification_runner=lambda worktree: calls.append(worktree) or _passing_checks(worktree),
    )
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["implementation_status"] == "failed"
    assert calls == []


def test_invalid_diff_is_reported_without_verification(tmp_path):
    calls = []
    graph, state, config, *_ = _setup(
        tmp_path, diff_status="invalid",
        verification_runner=lambda worktree: calls.append(worktree) or _passing_checks(worktree),
    )
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["diff_validation_status"] == "invalid"
    assert calls == []


@pytest.mark.parametrize("check_status", ["failed", "timed_out"])
def test_verification_failure_and_timeout_are_reported(tmp_path, check_status):
    def runner(worktree):
        return [VerificationCheckResult(
            check_id=check_id, display_name=check_id,
            command_identity=["python", "-m", "pytest", "-q"] if check_id == "python.tests" else ["npm", "run", "typecheck"],
            working_directory="worktree",
            working_directory_identity="." if check_id == "python.tests" else "frontend",
            status=check_status,
            exit_code=1 if check_status == "failed" else None,
            duration_seconds=1,
        ) for check_id in ("python.tests", "frontend.typecheck")]

    graph, state, config, *_ = _setup(tmp_path, verification_runner=runner)
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["overall_status"] == "failed"
    assert {item["status"] for item in done["verification_results"]} == {check_status}


def test_missing_verification_evidence_cannot_pass(tmp_path):
    graph, state, config, *_ = _setup(tmp_path, verification_runner=lambda worktree: [])
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["verification_results"] == []
    assert any("missing" in item for item in done["final_report"]["failures"])


@pytest.mark.parametrize("mismatch", ["command", "working_directory", "working_directory_identity", "missing", "unexpected"])
def test_mismatched_host_verification_evidence_cannot_pass(tmp_path, mismatch):
    results = _passing_checks("unused")
    if mismatch == "command":
        results[0] = results[0].model_copy(update={"command_identity": ["other-command"]})
    elif mismatch == "working_directory":
        results[0] = results[0].model_copy(update={"working_directory": "repository"})
    elif mismatch == "working_directory_identity":
        results[0] = results[0].model_copy(update={"working_directory_identity": "frontend"})
    elif mismatch == "missing":
        results.pop()
    else:
        results[0] = results[0].model_copy(update={"check_id": "unexpected.check"})
    graph, state, config, *_ = _setup(tmp_path, verification_runner=lambda worktree: results)
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["overall_status"] == "failed"


def test_worktree_change_during_verification_cannot_pass(tmp_path):
    def mutate_during_verification(worktree):
        (Path(worktree) / "verification-side-effect.txt").write_text("changed", encoding="utf-8")
        return _passing_checks(worktree)

    graph, state, config, *_ = _setup(tmp_path, verification_runner=mutate_during_verification)
    proposal_result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(proposal_result)), config)
    assert done["final_status"] == "failed"
    assert done["final_report"]["overall_status"] == "failed"


def test_requested_changes_reanalyze_and_invalidate_previous_approval(tmp_path):
    revised = _proposal("Add an API health detail with a test.", risks=["Check response compatibility."])
    graph, state, config, calls, implementations, *_ = _setup(tmp_path, proposals=[_proposal(), revised])
    first, _ = _pause(graph, state, config)
    first_fingerprint = first["proposal_fingerprint"]
    changed = graph.invoke(Command(resume=_decision(first, "request_changes", "Add a response compatibility test.")), config)
    assert changed["final_status"] == "awaiting_approval"
    assert len(calls) == 2
    assert calls[1][2] == "Add a response compatibility test."
    assert calls[1][3] == first["proposal"]
    assert changed["proposal_fingerprint"] != first_fingerprint
    assert changed["revision_count"] == 1
    assert len(changed["proposal_history"]) == 1
    assert changed["proposal_history"][0]["proposal_fingerprint"] == first_fingerprint
    assert changed["decisions"][0]["action"] == "request_changes"
    assert changed["decisions"][0]["proposal_fingerprint"] == first_fingerprint
    assert not implementations
    completed = graph.invoke(Command(resume=_decision(changed)), config)
    assert completed["final_status"] == "passed"
    assert completed["decisions"][-1]["proposal_revision"] == 1
    assert completed["decisions"][-1]["proposal_fingerprint"] == changed["proposal_fingerprint"]


def test_previous_proposal_approval_cannot_authorize_revised_proposal(tmp_path):
    revised = _proposal()
    graph, state, config, _, implementations, *_ = _setup(tmp_path, proposals=[_proposal(), revised])
    first, _ = _pause(graph, state, config)
    old_approval = _decision(first)
    second = graph.invoke(Command(resume=_decision(first, "request_changes", "Revise scope.")), config)
    assert second["final_status"] == "awaiting_approval"
    assert second["proposal_fingerprint"] != first["proposal_fingerprint"]
    assert second["revision_count"] == 1
    failed = graph.invoke(Command(resume=old_approval), config)
    assert failed["final_status"] == "failed"
    assert not implementations


def test_rejection_terminates_without_implementation(tmp_path):
    graph, state, config, _, implementations, *_ = _setup(tmp_path)
    result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(result, "reject")), config)
    assert done["final_status"] == "rejected"
    assert done["final_report"]["overall_status"] == "rejected"
    assert not implementations


@pytest.mark.parametrize("payload", [{}, {"action": "approve"}, {"action": "approve", "proposal_fingerprint": "bad"}])
def test_missing_or_malformed_decision_fails_closed(tmp_path, payload):
    graph, state, config, _, implementations, *_ = _setup(tmp_path)
    _pause(graph, state, config)
    if payload == {}:
        with pytest.raises(UnknownInterruptError):
            graph.invoke(Command(resume=payload), config)
        assert graph.get_state(config).values["final_status"] == "awaiting_approval"
    else:
        result = graph.invoke(Command(resume=payload), config)
        assert result["final_status"] == "failed"
    assert not implementations


def test_revision_limit_fails_closed(tmp_path):
    graph, state, config, calls, implementations, *_ = _setup(tmp_path, max_revisions=0)
    result, _ = _pause(graph, state, config)
    done = graph.invoke(Command(resume=_decision(result, "request_changes", "Revise.")), config)
    assert done["final_status"] == "failed"
    assert len(calls) == 1
    assert not implementations


def test_invalid_or_unsafe_proposal_never_reaches_human_gate(tmp_path):
    bad = _proposal().model_dump(mode="json")
    bad["expected_files"][0]["path"] = "../outside.py"
    bad["file_scope"]["included_files"] = ["../outside.py"]
    graph, state, config, *_ = _setup(tmp_path, proposals=[bad])
    done = graph.invoke(state, config)
    assert done["final_status"] == "failed"
    assert graph.get_state(config).next == ()


def test_prose_in_file_scope_is_rejected_before_human_review(tmp_path):
    bad = _proposal().model_dump(mode="json")
    bad["file_scope"]["excluded_files"] = [
        "All other repository files, including fixture code and WordPilot application code"
    ]
    graph, state, config, *_ = _setup(tmp_path, proposals=[bad])
    result = graph.invoke(state, config)
    assert result["final_status"] == "failed"
    assert graph.get_state(config).next == ()


def test_repository_validation_rejects_missing_modify_target(tmp_path):
    graph, state, config, *_ = _setup(tmp_path)
    (tmp_path / "backend" / "api.py").unlink()
    result = graph.invoke(state, config)
    assert result["final_status"] == "failed"
    assert graph.get_state(config).next == ()


def test_documentation_only_create_proposal_reaches_review_without_writes(tmp_path):
    proposal = TaskProposal.model_validate({
        "task_summary": "Document the safe demo fixture boundary.",
        "requirements": ["Create only tests/devflow_demo/README.md."],
        "acceptance_criteria": ["The README describes its purpose and safe synthetic-only use."],
        "implementation_steps": [{"order": 1, "description": "Create the README."}],
        "expected_files": [{
            "path": "tests/devflow_demo/README.md", "action": "create",
            "rationale": "This is the only requested artifact.",
        }],
        "file_scope": {"included_files": ["tests/devflow_demo/README.md"], "excluded_files": []},
        "technical_decisions": [{"decision": "Use one Markdown file", "rationale": "The task is documentation-only."}],
        "testing_strategy": ["Inspect the README content and validate the complete diff against the exact created path."],
    })
    graph, state, config, _, implementations, worktrees, diffs = _setup(tmp_path, proposals=[proposal])
    result = graph.invoke(state, config)
    assert result["final_status"] == "awaiting_approval"
    assert result["proposal"]["file_scope"] == {
        "included_files": ["tests/devflow_demo/README.md"], "excluded_files": []
    }
    assert not implementations and not worktrees and not diffs


def test_existing_file_delete_action_reaches_review(tmp_path):
    proposal = _proposal(
        expected_files=[{
            "path": "backend/api.py", "action": "delete",
            "rationale": "The task explicitly removes this scoped file.",
        }],
        file_scope={"included_files": ["backend/api.py"]},
    )
    graph, state, config, *_ = _setup(tmp_path, proposals=[proposal])
    result = graph.invoke(state, config)
    assert result["final_status"] == "awaiting_approval"


def test_state_and_checkpoint_resume_are_json_serializable(tmp_path):
    graph, state, config, _, implementations, *_ = _setup(tmp_path)
    assert json.loads(json.dumps(state))["run_id"] == state["run_id"]
    result, snapshot = _pause(graph, state, config)
    json.dumps(snapshot.values)
    done = graph.invoke(Command(resume=_decision(result)), config)
    json.dumps(done)
    assert done["final_status"] == "passed"
    assert done["final_report"]["overall_status"] == "passed"
    assert graph.get_state(config).values["run_id"] == state["run_id"]
    assert implementations
