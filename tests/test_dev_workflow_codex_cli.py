from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_workflow import codex_cli
from dev_workflow.schemas import TaskProposal, proposal_fingerprint


_EXEC_HELP = """Options:
--ephemeral
--ignore-user-config
-c, --config <key=value>
--sandbox <SANDBOX_MODE>
possible values: read-only, workspace-write
--json
--output-schema <FILE>
--output-last-message <FILE>
"""
_REVIEW_HELP = """Options:
--ephemeral
--ignore-user-config
--uncommitted
--json
--output-last-message <FILE>
"""


def _proposal_json():
    return json.dumps({
        "task_summary": "Add a response field.",
        "requirements": ["Keep API compatibility."],
        "acceptance_criteria": ["The field is present."],
        "implementation_steps": [{"order": 1, "description": "Update the endpoint response."}],
        "expected_files": [{"path": "backend/api.py", "action": "modify", "rationale": "Add field."}],
        "file_scope": {"included_files": ["backend/api.py"]},
        "technical_decisions": [{"decision": "Use existing response model", "rationale": "Follow project convention."}],
        "risks": ["Response shape changes."],
        "edge_cases": ["Preserve existing fields."],
        "testing_strategy": ["Run API tests."],
        "assumptions": [],
    })


def _fake_run_factory(*, returncode=0, message=None, stdout="event", stderr=""):
    captured = {"help_commands": []}

    def fake_run(command, **kwargs):
        if command[-1] == "--help":
            captured["help_commands"].append(command)
            help_text = _REVIEW_HELP if "review" in command else _EXEC_HELP
            return subprocess.CompletedProcess(command, 0, stdout=help_text, stderr="")
        captured["command"] = command
        captured["kwargs"] = kwargs
        kwargs["stdout"].write(stdout.encode())
        kwargs["stderr"].write(stderr.encode())
        output_path = Path(command[command.index("--output-last-message") + 1])
        if message is not None:
            output_path.write_text(message, encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode)

    return fake_run, captured


def _setup(monkeypatch, tmp_path, fake_run):
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setenv("DEVFLOW_WINDOWS_SANDBOX", "elevated")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: "C:/tools/codex.exe")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    return tmp_path


def test_explicit_executable_path_takes_priority(monkeypatch, tmp_path):
    executable = tmp_path / "bundled-codex.exe"
    executable.write_bytes(b"fake executable")
    monkeypatch.setenv("CODEX_CLI_PATH", str(executable))
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: "C:/tools/path-codex.exe")

    resolved, error = codex_cli.resolve_codex_executable()

    assert resolved == str(executable.resolve())
    assert error is None


def test_invalid_configured_path_uses_path_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_CLI_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: "C:/tools/path-codex.exe")

    resolved, error = codex_cli.resolve_codex_executable()

    assert resolved == "C:/tools/path-codex.exe"
    assert error is None


def test_missing_configured_and_path_executable_has_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_CLI_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: None)

    resolved, error = codex_cli.resolve_codex_executable()

    assert resolved is None
    assert "CODEX_CLI_PATH" in error
    assert "PATH" in error


def test_success_uses_read_only_cli_stdin_and_explicit_working_directory(monkeypatch, tmp_path):
    fake_run, captured = _fake_run_factory(message=_proposal_json())
    _setup(monkeypatch, tmp_path, fake_run)
    result, proposal = codex_cli.analyze_task("Add a response field", tmp_path)

    assert result.status == "succeeded"
    assert isinstance(proposal, TaskProposal)
    args = captured["command"]
    assert args[:2] == ["C:/tools/codex.exe", "exec"]
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert captured["help_commands"] == [["C:/tools/codex.exe", "exec", "--help"]]
    assert "--json" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert args[-1] == "-"
    assert args == [
        "C:/tools/codex.exe", "exec", "--ephemeral", "--ignore-user-config",
        "--sandbox", "read-only", "--json", "--output-schema",
        args[args.index("--output-schema") + 1], "--output-last-message",
        args[args.index("--output-last-message") + 1], "-",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert isinstance(kwargs["input"], bytes)
    assert "Add a response field" in kwargs["input"].decode("utf-8")
    assert "text" not in kwargs


def test_analysis_prompt_is_sent_as_exact_utf8_bytes_for_unicode_text(monkeypatch, tmp_path):
    task = "Update naïve façade; Zoë’s résumé — 你好 مرحبا"
    fake_run, captured = _fake_run_factory(message=_proposal_json())
    _setup(monkeypatch, tmp_path, fake_run)

    result, proposal = codex_cli.analyze_task(task, tmp_path)

    sent = captured["kwargs"]["input"]
    assert result.status == "succeeded"
    assert proposal is not None
    assert isinstance(sent, bytes)
    assert task.encode("utf-8") in sent
    assert sent.decode("utf-8").encode("utf-8") == sent


def test_reanalysis_includes_human_feedback_and_previous_proposal(monkeypatch, tmp_path):
    fake_run, captured = _fake_run_factory(message=_proposal_json())
    _setup(monkeypatch, tmp_path, fake_run)
    result, _ = codex_cli.analyze_task("Task", tmp_path, feedback="Narrow the scope.",
                                       previous_proposal={"task_summary": "Earlier plan"})
    assert result.status == "succeeded"
    prompt = captured["kwargs"]["input"].decode("utf-8")
    assert "Narrow the scope." in prompt
    assert "Earlier plan" in prompt
    assert "do not execute repository commands or edit files" in prompt
    assert "Implementation may occur only after explicit human approval" in prompt
    assert "Host-controlled verification may run after implementation" in prompt
    assert "Do not say that read-only analysis restrictions prohibit later host verification" in prompt


def test_implementation_uses_workspace_write_only_in_supplied_worktree(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    worktree = root / ".devflow-worktrees" / "worktree"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, captured = _fake_run_factory(message="implementation complete")
    _setup(monkeypatch, tmp_path, fake_run)
    base_run = codex_cli.subprocess.run

    def fake_git_and_codex(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=str(worktree).encode())
        return base_run(command, **kwargs)

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_git_and_codex)
    fingerprint = proposal_fingerprint(proposal)
    result = codex_cli.implement_approved_plan(proposal, fingerprint, worktree)
    args = captured["command"]
    assert result.status == "succeeded"
    expected = ["C:/tools/codex.exe", "exec", "--ephemeral", "--ignore-user-config"]
    if codex_cli.sys.platform == "win32":
        expected.extend(["--config", "windows.sandbox=elevated"])
    expected.extend([
        "--sandbox", "workspace-write", "--json", "--output-last-message",
        args[args.index("--output-last-message") + 1], "-",
    ])
    assert args == expected
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "--ask-for-approval" not in args
    assert "--approve-for-me" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert captured["kwargs"]["cwd"] == str(worktree.resolve())
    assert captured["kwargs"]["shell"] is False
    sent = captured["kwargs"]["input"]
    assert isinstance(sent, bytes)
    assert fingerprint.encode("utf-8") in sent
    assert sent.decode("utf-8").encode("utf-8") == sent
    assert "text" not in captured["kwargs"]
    assert result.command_identity[args.index("--sandbox") + 1] == "workspace-write"
    assert captured["help_commands"] == [["C:/tools/codex.exe", "exec", "--help"]]


@pytest.mark.parametrize("implementation", ["mxc", "elevated", "unelevated"])
def test_windows_implementation_passes_validated_sandbox_config(
    monkeypatch, tmp_path, implementation,
):
    monkeypatch.setattr(codex_cli.sys, "platform", "win32")
    root = tmp_path / "repo"
    worktree = root / ".devflow-worktrees" / "worktree"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, captured = _fake_run_factory(message="implementation complete")
    _setup(monkeypatch, tmp_path, fake_run)
    monkeypatch.setenv("DEVFLOW_WINDOWS_SANDBOX", implementation)
    base_run = codex_cli.subprocess.run

    def fake_git_and_codex(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=str(worktree).encode())
        return base_run(command, **kwargs)

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_git_and_codex)
    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )
    args = captured["command"]
    assert result.status == "succeeded"
    assert args[args.index("--config") + 1] == f"windows.sandbox={implementation}"
    assert "--ignore-user-config" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert captured["kwargs"]["cwd"] == str(worktree.resolve())
    assert captured["kwargs"]["shell"] is False
    assert not {
        "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust", "--yolo", "--full-auto",
    }.intersection(args)
    assert "danger-full-access" not in args


@pytest.mark.parametrize("implementation", [None, "", " Elevated", "ELEVATED", "danger-full-access"])
def test_windows_implementation_rejects_missing_or_invalid_sandbox_setting_before_subprocess(
    monkeypatch, tmp_path, implementation,
):
    monkeypatch.setattr(codex_cli.sys, "platform", "win32")
    calls = []

    def unexpected_subprocess(command, **kwargs):
        calls.append(command)
        raise AssertionError("Subprocess must not run for invalid Windows sandbox configuration.")

    _setup(monkeypatch, tmp_path, unexpected_subprocess)
    if implementation is None:
        monkeypatch.delenv("DEVFLOW_WINDOWS_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("DEVFLOW_WINDOWS_SANDBOX", implementation)
    worktree = tmp_path / "repo" / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))

    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )

    assert result.status == "failed"
    assert result.exit_code is None
    assert "DEVFLOW_WINDOWS_SANDBOX" in result.error
    assert calls == []


def test_non_windows_implementation_does_not_require_or_pass_windows_sandbox_setting(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    root = tmp_path / "repo"
    worktree = root / ".devflow-worktrees" / "worktree"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, captured = _fake_run_factory(message="implementation complete")
    _setup(monkeypatch, tmp_path, fake_run)
    monkeypatch.delenv("DEVFLOW_WINDOWS_SANDBOX", raising=False)
    base_run = codex_cli.subprocess.run

    def fake_git_and_codex(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=str(worktree).encode())
        return base_run(command, **kwargs)

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_git_and_codex)
    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )

    assert result.status == "succeeded"
    assert "--config" not in captured["command"]
    assert "--ignore-user-config" in captured["command"]
    assert captured["command"][captured["command"].index("--sandbox") + 1] == "workspace-write"
    assert captured["kwargs"]["cwd"] == str(worktree.resolve())


@pytest.mark.parametrize("output", [
    '{"type":"item.completed","item":{"type":"agent_message","text":"The workspace is mounted read-only."}}',
    '{"type":"item.completed","item":{"type":"command_execution","error":"Rejected: blocked by policy"}}',
    "Codex sandbox rejected the write request.",
    "Permission denied while creating the approved file.",
    "PowerShell command rejected by environment policy.",
    "Windows sandbox implementation is unavailable.",
    "windows.sandbox implementation is not allowed by managed policy.",
    '{"error":"environment policy blocked command execution","exit_code":0}',
])
def test_explicit_execution_failures_are_detected_despite_zero_exit(output):
    assert codex_cli._explicit_execution_failure(output) is not None


def test_implementation_marks_zero_exit_policy_rejection_failed(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    worktree = root / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, _ = _fake_run_factory(
        returncode=0,
        message="This session has read-only file access and cannot implement the approved change.",
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"No files changed."}}',
        stderr="PowerShell command rejected by policy.",
    )
    _setup(monkeypatch, tmp_path, fake_run)
    monkeypatch.setattr(codex_cli, "_validate_worktree_root", lambda _: True)
    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )
    assert result.exit_code == 0
    assert result.status == "failed"
    assert "sandbox or execution-policy refusal" in result.error


def test_ordinary_nonzero_implementation_failure_has_distinct_diagnostic(monkeypatch, tmp_path):
    worktree = tmp_path / "repo" / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, _ = _fake_run_factory(
        returncode=7, message="Implementation command failed.", stderr="ordinary failure",
    )
    _setup(monkeypatch, tmp_path, fake_run)
    monkeypatch.setattr(codex_cli, "_validate_worktree_root", lambda _: True)

    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )

    assert result.status == "failed"
    assert result.error == "Codex implementation exited with a nonzero status."


def test_implementation_timeout_remains_distinct_from_policy_refusal(monkeypatch, tmp_path):
    worktree = tmp_path / "repo" / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))

    def timeout(command, **kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout=_EXEC_HELP, stderr="")
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=b"PowerShell command rejected by policy",
        )

    _setup(monkeypatch, tmp_path, timeout)
    monkeypatch.setattr(codex_cli, "_validate_worktree_root", lambda _: True)

    result = codex_cli.implement_approved_plan(
        proposal, proposal_fingerprint(proposal), worktree,
    )

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.error == "Codex implementation exceeded the configured timeout."


def test_unsupported_workspace_write_capability_fails_closed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, lambda *args, **kwargs: None)
    worktree = tmp_path / "repo" / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fingerprint = proposal_fingerprint(proposal)
    monkeypatch.setattr(codex_cli, "_validate_worktree_root", lambda _: True)

    def unsupported_help(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="--output-last-message --json", stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", unsupported_help)
    result = codex_cli.implement_approved_plan(proposal, fingerprint, worktree)
    assert result.status == "failed"
    assert "lacks required sandboxed implementation options" in result.error


def test_review_uses_review_specific_supported_argument_list(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    worktree = root / ".devflow-worktrees" / "run"
    worktree.mkdir(parents=True)
    proposal = TaskProposal.model_validate(json.loads(_proposal_json()))
    fake_run, captured = _fake_run_factory(message="No findings.")
    _setup(monkeypatch, tmp_path, fake_run)
    base_run = codex_cli.subprocess.run

    def fake_git_and_cli(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout=str(worktree).encode())
        return base_run(command, **kwargs)

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_git_and_cli)
    result = codex_cli.review_worktree(proposal, proposal_fingerprint(proposal), worktree)
    args = captured["command"]
    assert result.status == "succeeded"
    assert args == [
        "C:/tools/codex.exe", "exec", "review", "--ephemeral", "--ignore-user-config",
        "--uncommitted", "--json", "--output-last-message",
        args[args.index("--output-last-message") + 1], "-",
    ]
    assert "--sandbox" not in args
    sent = captured["kwargs"]["input"]
    assert isinstance(sent, bytes)
    assert sent.decode("utf-8").encode("utf-8") == sent
    assert "text" not in captured["kwargs"]
    assert captured["help_commands"] == [["C:/tools/codex.exe", "exec", "review", "--help"]]


def test_missing_executable_returns_safe_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: None)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "not found" in result.error


def test_timeout_is_reported_without_exception_details(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        if args[0][-1] == "--help":
            return subprocess.CompletedProcess(args[0], 0, stdout=_EXEC_HELP, stderr="")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial", stderr=b"secret=hidden")

    _setup(monkeypatch, tmp_path, timeout)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "timeout"
    assert result.timed_out is True
    assert proposal is None
    assert "secret" not in result.error


def test_nonzero_exit_has_failure_status(monkeypatch, tmp_path):
    fake_run, _ = _fake_run_factory(returncode=7, message=_proposal_json())
    _setup(monkeypatch, tmp_path, fake_run)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert result.exit_code == 7
    assert proposal is None
    assert "nonzero" in result.error


@pytest.mark.parametrize("message", ["not json", '{"task_description":"partial"}'])
def test_invalid_or_schema_incomplete_result_fails(monkeypatch, tmp_path, message):
    fake_run, _ = _fake_run_factory(message=message)
    _setup(monkeypatch, tmp_path, fake_run)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "invalid structured" in result.error


def test_missing_last_message_fails(monkeypatch, tmp_path):
    fake_run, _ = _fake_run_factory(message=None)
    _setup(monkeypatch, tmp_path, fake_run)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "did not create" in result.error


def test_captured_output_is_bounded(monkeypatch, tmp_path):
    fake_run, _ = _fake_run_factory(
        message=_proposal_json(), stdout="x" * (codex_cli.MAX_CAPTURE_BYTES * 3),
        stderr="y" * (codex_cli.MAX_CAPTURE_BYTES * 3),
    )
    _setup(monkeypatch, tmp_path, fake_run)
    result, _ = codex_cli.analyze_task("Task", tmp_path)
    assert len(result.stdout) <= codex_cli.MAX_CAPTURE_BYTES
    assert len(result.stderr) <= codex_cli.MAX_CAPTURE_BYTES
    assert "[output truncated]" in result.stdout


def test_captured_credentials_are_redacted(monkeypatch, tmp_path):
    fake_run, _ = _fake_run_factory(
        message=_proposal_json(),
        stderr="GROQ_API_KEY=groq-secret Bearer bearer-secret sk-12345678901234567890",
    )
    _setup(monkeypatch, tmp_path, fake_run)
    result, _ = codex_cli.analyze_task("Task", tmp_path)
    assert "groq-secret" not in result.stderr
    assert "bearer-secret" not in result.stderr
    assert "12345678901234567890" not in result.stderr
    assert result.stderr.count("[REDACTED]") == 3


def test_unexpected_process_error_does_not_leak_secret(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        if args[0][-1] == "--help":
            return subprocess.CompletedProcess(args[0], 0, stdout=_EXEC_HELP, stderr="")
        raise OSError("failed with API_KEY=secret-value")

    _setup(monkeypatch, tmp_path, fail)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "secret-value" not in str(result.model_dump())
