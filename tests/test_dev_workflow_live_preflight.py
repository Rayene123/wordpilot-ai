from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dev_workflow import codex_cli
from dev_workflow import preflight
from dev_workflow.schemas import (
    CodexExecutionResult,
    DiffReport,
    LiveCodexPreflightReport,
    WriteCapabilityPreflightResult,
)


def _execution(*, status="succeeded", parsed=None, error=None):
    return CodexExecutionResult(
        command_identity=["codex", "exec", "--sandbox", "read-only"],
        exit_code=0 if status == "succeeded" else 1,
        stdout="", stderr="", last_message=None, parsed_result=parsed,
        duration_seconds=0.1, status=status, timed_out=False, error=error,
    )


def _write_result(*, status="passed", error=None):
    marker = "devflow_preflight_marker.txt"
    execution = CodexExecutionResult(
        command_identity=["codex", "exec", "--sandbox", "workspace-write"],
        exit_code=0 if status == "passed" else 1, stdout="", stderr="",
        last_message=None, parsed_result=None, duration_seconds=0.1,
        status="succeeded" if status == "passed" else "failed",
        timed_out=False, error=error,
    )
    diff = DiffReport(
        status="valid" if status == "passed" else "invalid",
        changed_files=[{"path": marker, "action": "create"}] if status == "passed" else [],
        diff="", worktree_path="/repo/.devflow-worktrees/run",
    )
    return WriteCapabilityPreflightResult(
        status=status, preflight_run_id="run", worktree_path="/repo/.devflow-worktrees/run",
        evidence_path="/tmp/evidence.json", marker_path=marker, marker_contents="marker\n",
        marker_content_verified=status == "passed", scope_validated=status == "passed",
        execution_result=execution, diff_report=diff,
        cleanup_status="removed", errors=[] if status == "passed" else [error or "failed"],
    )


def _setup(monkeypatch, tmp_path, *, read_result=None, write_result=None, auth=("passed", "authenticated")):
    (tmp_path / "README.md").write_text("# Small preflight fixture\n" + "context\n" * 20, encoding="utf-8")
    monkeypatch.setattr(preflight, "resolve_codex_executable", lambda: ("/tools/codex", None))
    write_calls = []

    def read_probe(_root, *, executable):
        assert executable == "/tools/codex"
        return read_result or _execution(parsed={
            "inspected_path": "README.md", "first_nonempty_line": "# Small preflight fixture",
        })

    def write_probe(root):
        write_calls.append(root)
        return write_result or _write_result()

    report = preflight.run_live_codex_preflight(
        tmp_path,
        authentication_check=lambda executable, root: auth,
        read_only_probe=read_probe,
        write_probe=write_probe,
    )
    return report, write_calls


def test_live_preflight_requires_matching_structured_read_only_result_and_write_probe(monkeypatch, tmp_path):
    original = "# Small preflight fixture\n" + "context\n" * 20
    (tmp_path / "README.md").write_text(original, encoding="utf-8")
    report, write_calls = _setup(monkeypatch, tmp_path)

    assert report.overall_status == "passed"
    assert report.cli_status == "available"
    assert report.authentication_status == "passed"
    assert report.read_only_status == "passed"
    assert report.workspace_write_status == "passed"
    assert len(write_calls) == 1
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == original


def test_live_preflight_distinguishes_cli_unavailable_and_authentication_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "resolve_codex_executable", lambda: (None, "Codex CLI missing"))
    unavailable = preflight.run_live_codex_preflight(tmp_path)
    assert unavailable.cli_status == "unavailable"
    assert unavailable.authentication_status == "skipped"
    assert unavailable.read_only_status == unavailable.workspace_write_status == "skipped"

    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "resolve_codex_executable", lambda: ("/tools/codex", None))
    report, write_calls = _setup(monkeypatch, tmp_path, auth=("failed", "Please run codex login."))
    assert report.authentication_status == "failed"
    assert report.read_only_status == report.workspace_write_status == "skipped"
    assert "codex login" in report.workspace_write_detail
    assert write_calls == []

    report, write_calls = _setup(monkeypatch, tmp_path, auth=("process_launch_denied", "Permission denied."))
    assert report.authentication_status == "process_launch_denied"
    assert write_calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Codex CLI could not be started.", "process_launch_denied"),
        ("Codex reported a sandbox or execution-policy refusal.", "sandbox_denied"),
    ],
)
def test_live_preflight_classifies_read_only_execution_failures(monkeypatch, tmp_path, error, expected):
    blocked = _execution(status="failed", error=error)
    report, _ = _setup(monkeypatch, tmp_path, read_result=blocked)
    assert report.read_only_status == expected
    assert report.overall_status == "failed"


def test_live_preflight_rejects_invalid_structured_or_unmatched_read_only_result(monkeypatch, tmp_path):
    invalid = _execution(status="succeeded", parsed={"unexpected": "shape"})
    report, _ = _setup(monkeypatch, tmp_path, read_result=invalid)
    assert report.read_only_status == "invalid_result"

    mismatched = _execution(status="succeeded", parsed={
        "inspected_path": "README.md", "first_nonempty_line": "hallucinated context",
    })
    report, _ = _setup(monkeypatch, tmp_path, read_result=mismatched)
    assert report.read_only_status == "invalid_result"


def test_live_preflight_classifies_workspace_write_policy_and_launch_failures(monkeypatch, tmp_path):
    policy_result = _write_result(status="failed", error="Codex reported a sandbox refusal.")
    report, _ = _setup(monkeypatch, tmp_path, write_result=policy_result)
    assert report.workspace_write_status == "sandbox_denied"

    launch_result = _write_result(status="failed", error="Codex CLI could not be started.")
    report, _ = _setup(monkeypatch, tmp_path, write_result=launch_result)
    assert report.workspace_write_status == "process_launch_denied"


def test_live_preflight_report_cannot_claim_pass_without_both_live_operations():
    with pytest.raises(ValueError, match="both live operations"):
        LiveCodexPreflightReport(
            overall_status="passed", cli_status="available", authentication_status="passed",
            read_only_status="passed", workspace_write_status="failed",
            read_only_detail="read ok", workspace_write_detail="write failed",
        )


def test_read_only_adapter_launches_with_read_only_sandbox_and_strict_bounded_result(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    readme = tmp_path / "README.md"
    readme.write_text("# Fixture\n" + "extra context\n" * 30, encoding="utf-8")
    original = readme.read_bytes()
    captured = []
    monkeypatch.setattr(codex_cli, "resolve_codex_executable", lambda _exe: ("/tools/codex", None))

    def run(command, **kwargs):
        captured.append((command, kwargs))
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout=(
                "--ephemeral --ignore-user-config --sandbox read-only --json "
                "--output-schema --output-last-message"
            ), stderr="")
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(json.dumps({
            "inspected_path": "README.md", "first_nonempty_line": "# Fixture",
        }), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    result = codex_cli.run_read_only_preflight(tmp_path)

    assert result.status == "succeeded"
    assert result.parsed_result == {"inspected_path": "README.md", "first_nonempty_line": "# Fixture"}
    command, kwargs = captured[-1]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert b"first 12 lines" in kwargs["input"]
    assert readme.read_bytes() == original


def test_read_only_adapter_reports_process_launch_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    monkeypatch.setattr(codex_cli, "resolve_codex_executable", lambda _exe: ("/tools/codex", None))

    def run(command, **kwargs):
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, stdout=(
                "--ephemeral --ignore-user-config --sandbox read-only --json "
                "--output-schema --output-last-message"
            ), stderr="")
        raise PermissionError("execution policy denied process launch")

    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    result = codex_cli.run_read_only_preflight(tmp_path)
    assert result.status == "failed"
    assert result.error == "Codex CLI could not be started."


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, "passed"), (1, "failed")],
)
def test_authentication_check_uses_fixed_non_shell_command(monkeypatch, tmp_path, returncode, expected):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        if returncode:
            kwargs["stderr"].write(b"Please run codex login.\n")
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    status, detail = codex_cli.check_codex_authentication("/tools/codex", tmp_path)
    assert status == expected
    assert captured["command"] == ["/tools/codex", "login", "status"]
    assert captured["kwargs"]["shell"] is False
    if returncode:
        assert "codex login" in detail
    else:
        assert "authenticated" in detail


def test_authentication_check_distinguishes_process_launch_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_cli.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))
    status, _detail = codex_cli.check_codex_authentication("/tools/codex", tmp_path)
    assert status == "process_launch_denied"
