from __future__ import annotations

from types import SimpleNamespace

from dev_workflow import __main__ as cli


def test_main_exits_from_interactive_menu(monkeypatch, capsys):
    answers = iter(["7"])
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    assert cli.main() == 0
    assert "7. Exit" in capsys.readouterr().out


def test_status_distinguishes_interrupted_and_failed(capsys):
    cli._show_state({
        "run_id": "run-1", "current_stage": "implemented", "final_status": "pending",
        "decisions": [{"action": "approve"}], "execution_result": {"status": "succeeded"},
        "verification_results": [], "errors": [],
    })
    output = capsys.readouterr().out
    assert "Stage: implemented | Status: pending" in output
    assert "Approval: approve" in output
    assert "Verification: not run" in output

    cli._show_state({"run_id": "run-2", "current_stage": "failed", "final_status": "failed",
                     "decisions": [], "errors": ["Codex analysis failed."]})
    output = capsys.readouterr().out
    assert "Stage: failed | Status: failed" in output
    assert "Codex analysis failed." in output


def test_status_reports_worktree_changed_files_and_check_evidence(monkeypatch, capsys):
    report = SimpleNamespace(
        overall_status="passed",
        diff_validation_status="valid",
        changed_files=[SimpleNamespace(action="create", path="average.py"),
                       SimpleNamespace(action="create", path="test_average.py")],
        verification_results=[SimpleNamespace(
            check_id="python.tests", status="passed", exit_code=0,
            command_identity=["python", "-m", "pytest", "-q"],
        )],
        failures=[],
    )
    monkeypatch.setattr(cli, "FinalReport", SimpleNamespace(model_validate=lambda _value: report))

    cli._show_state({
        "run_id": "demo-run",
        "current_stage": "passed",
        "final_status": "passed",
        "worktree_path": ".devflow-worktrees/demo-run",
        "decisions": [{"action": "approve"}],
        "execution_result": {"status": "succeeded"},
        "verification_results": [],
        "final_report": {"overall_status": "passed"},
        "errors": [],
    })

    output = capsys.readouterr().out
    assert "Worktree: .devflow-worktrees/demo-run" in output
    assert "Changed files: create: average.py, create: test_average.py" in output
    assert "python.tests: passed (exit 0); python -m pytest -q" in output
    assert "Report: passed; diff valid" in output


def test_start_task_uses_existing_workflow_api_and_surfaces_approval(monkeypatch, tmp_path, capsys):
    events = []

    class Graph:
        def stream(self, state, config, stream_mode):
            events.append((state["task_description"], config["configurable"]["thread_id"]))
            self.values = {**state, "current_stage": "awaiting_approval", "final_status": "awaiting_approval",
                           "proposal": {"task_summary": "Create a greeting script.", "requirements": ["Print hello"],
                                        "acceptance_criteria": ["Print hello exactly"],
                                        "implementation_steps": [{"order": 1, "description": "Create hello.py"}],
                                        "expected_files": [{"path": "hello.py", "action": "create", "rationale": "Demo"}],
                                        "file_scope": {"included_files": ["hello.py"], "excluded_files": []},
                                        "technical_decisions": [{"decision": "Use Python", "rationale": "Demo"}],
                                        "testing_strategy": ["Run the script"]}, "revision_count": 0}
            yield {"analyze_task": {}}

        def get_state(self, config):
            return type("Snapshot", (), {"values": self.values})()

    task = "Create a Python greeting script.\n\nAcceptance criteria:\n- Print exact output.\n"
    answers = iter([*task.split("\n"), "END"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "_review", lambda graph, state: events.append(("review", state["run_id"])))
    cli.start_task(Graph())
    assert events[0] == (task, events[0][1])
    assert events[1] == ("review", events[0][1])
    output = capsys.readouterr().out
    assert "awaiting_approval" in output
    assert "Progress: analyze task" in output


def test_read_task_description_preserves_multiline_text_and_blank_lines():
    entered = iter(["  Implement feature.  ", "", "Acceptance criteria:", "- Keep spacing.", "", "END"])

    task = cli._read_task_description(lambda _prompt: next(entered))

    assert task == "  Implement feature.  \n\nAcceptance criteria:\n- Keep spacing.\n"


def test_read_task_description_uses_only_exact_end_line_as_terminator():
    entered = iter(["Mention END in task text.", " END ", "END", "7"])

    task = cli._read_task_description(lambda _prompt: next(entered))

    assert task == "Mention END in task text.\n END "
    assert next(entered) == "7"


def test_read_task_description_discards_incomplete_input_on_eof(capsys):
    entered = iter(["Incomplete task", "first detail"])

    def read_line(_prompt):
        try:
            return next(entered)
        except StopIteration as exc:
            raise EOFError from exc

    assert cli._read_task_description(read_line) is None
    assert "incomplete task was discarded" in capsys.readouterr().out


def test_read_task_description_rejects_empty_input(capsys):
    entered = iter(["", "", "END"])

    assert cli._read_task_description(lambda _prompt: next(entered)) is None
    assert "A task description is required" in capsys.readouterr().out


def test_read_task_description_keeps_single_line_entry_working():
    entered = iter(["Add a small helper", "END"])

    assert cli._read_task_description(lambda _prompt: next(entered)) == "Add a small helper"


def _proposal_for_feedback_test():
    return {
        "task_summary": "Create greeting module.",
        "requirements": ["Create hello.py."],
        "acceptance_criteria": ["hello.py exists."],
        "implementation_steps": [{"order": 1, "description": "Create hello.py."}],
        "expected_files": [{"path": "hello.py", "action": "create", "rationale": "Demo."}],
        "file_scope": {"included_files": ["hello.py"], "excluded_files": []},
        "technical_decisions": [{"decision": "Use Python.", "rationale": "Demo."}],
        "testing_strategy": ["Inspect the file."],
    }


def test_review_passes_multiline_feedback_unchanged_to_proposal_revision(monkeypatch, capsys):
    feedback = "  Narrow the implementation.\n\nKeep this detail.  \n"
    entered = iter(["  Narrow the implementation.", "", "Keep this detail.  ", "", "END"])
    captured = {}
    monkeypatch.setattr(cli, "_ask", lambda _prompt: "3")
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(entered))
    monkeypatch.setattr(cli, "_display_proposal", lambda state: cli.TaskProposal.model_validate(state["proposal"]))
    monkeypatch.setattr(cli, "resume_pending_approval", lambda graph, run_id, decision: (
        captured.update(run_id=run_id, decision=decision) or {}
    ))
    monkeypatch.setattr(cli, "_show_state", lambda _state: None)

    cli._review(object(), {
        "run_id": "feedback-run", "revision_count": 0,
        "proposal": _proposal_for_feedback_test(),
    })

    assert captured["run_id"] == "feedback-run"
    assert captured["decision"].action == "request_changes"
    assert captured["decision"].feedback == feedback
    assert "Finish by entering END on its own line" in capsys.readouterr().out


def test_multiline_feedback_terminates_only_on_exact_end_line():
    entered = iter(["Revise the summary.", "The word END appears here.", " END ", "END", "2"])

    feedback = cli._read_multiline_input(
        "proposal-change feedback", "feedback", lambda _prompt: next(entered),
    )

    assert feedback == "Revise the summary.\nThe word END appears here.\n END "
    assert next(entered) == "2"


def test_review_does_not_submit_empty_or_incomplete_feedback(monkeypatch, capsys):
    resumed = []
    monkeypatch.setattr(cli, "_ask", lambda _prompt: "3")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "END")
    monkeypatch.setattr(cli, "_display_proposal", lambda state: cli.TaskProposal.model_validate(state["proposal"]))
    monkeypatch.setattr(cli, "resume_pending_approval", lambda *args: resumed.append(args))
    cli._review(object(), {
        "run_id": "empty-feedback-run", "revision_count": 0,
        "proposal": _proposal_for_feedback_test(),
    })
    assert resumed == []
    assert "A feedback is required" in capsys.readouterr().out

    entered = iter(["Incomplete feedback"])

    def eof_after_one_line(_prompt):
        try:
            return next(entered)
        except StopIteration as exc:
            raise EOFError from exc

    assert cli._read_multiline_input("proposal-change feedback", "feedback", eof_after_one_line) is None
    assert "incomplete feedback was discarded" in capsys.readouterr().out


def test_environment_check_reports_python_and_git(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_codex_executable", lambda: (None, "Codex CLI not found"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "ROOT", cli.Path.cwd())
    monkeypatch.setattr(cli, "run_live_codex_preflight", lambda root: type("Result", (), {
        "overall_status": "failed", "cli_status": "unavailable",
        "authentication_status": "skipped", "read_only_status": "skipped",
        "workspace_write_status": "skipped", "read_only_detail": "unavailable",
        "workspace_write_detail": "unavailable", "write_evidence_path": None,
    })())
    cli.check_environment()
    output = capsys.readouterr().out
    assert "Python:" in output
    assert "Git: not found" in output
    assert "Codex CLI: Codex CLI not found" in output


def test_main_blocks_task_start_until_live_preflight_passes(monkeypatch, capsys):
    answers = iter(["2", "7"])
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    assert cli.main() == 0
    assert "Task start blocked" in capsys.readouterr().out


def test_approval_cannot_resume_into_implementation_without_live_preflight(monkeypatch, capsys):
    monkeypatch.setattr(cli, "LIVE_PREFLIGHT_PASSED", False)
    resumed = []
    monkeypatch.setattr(cli, "resume_pending_approval", lambda *args: resumed.append(args))
    state = {
        "run_id": "run-approval",
        "revision_count": 0,
        "proposal": {
            "task_summary": "Create greeting module.",
            "requirements": ["Create hello.py."],
            "acceptance_criteria": ["hello.py exists."],
            "implementation_steps": [{"order": 1, "description": "Create hello.py."}],
            "expected_files": [{"path": "hello.py", "action": "create", "rationale": "Demo."}],
            "file_scope": {"included_files": ["hello.py"], "excluded_files": []},
            "technical_decisions": [{"decision": "Use Python.", "rationale": "Demo."}],
            "testing_strategy": ["Inspect the file."],
        },
    }
    monkeypatch.setattr(cli, "_ask", lambda _prompt: "1")

    cli._review(object(), state)

    assert resumed == []
    assert "Approval blocked" in capsys.readouterr().out
