from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.dev_workflow import codex_cli
from backend.dev_workflow.schemas import TaskProposal


def _proposal_json():
    return json.dumps({
        "task_description": "Add a response field.",
        "requirements": ["Keep API compatibility."],
        "acceptance_criteria": ["The field is present."],
        "implementation_plan": ["Update the endpoint response."],
        "risks": ["Response shape changes."],
        "proposed_files": ["backend/api.py"],
        "excluded_files": ["frontend/"],
        "assumptions": [],
    })


def _fake_run_factory(*, returncode=0, message=None, stdout="event", stderr=""):
    captured = {}

    def fake_run(command, **kwargs):
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
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: "C:/tools/codex.exe")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    return tmp_path


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
    assert "--json" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert args[-1] == "-"
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert "Add a response field" in kwargs["input"]
    assert kwargs["text"] is True


def test_missing_executable_returns_safe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _: None)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "not found" in result.error


def test_timeout_is_reported_without_exception_details(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
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
        raise OSError("failed with API_KEY=secret-value")

    _setup(monkeypatch, tmp_path, fail)
    result, proposal = codex_cli.analyze_task("Task", tmp_path)
    assert result.status == "failed"
    assert proposal is None
    assert "secret-value" not in str(result.model_dump())
