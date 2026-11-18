from __future__ import annotations

import subprocess
import sys
import os
import pytest

from dev_workflow import verification
from dev_workflow.verification import VerificationCheck, run_verification


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "approved-worktree"
    root.mkdir()
    (root / "frontend").mkdir()
    monkeypatch.setattr(verification, "_safe_worktree", lambda path: root)
    monkeypatch.setattr(verification.worktree_tools, "diff_identity", lambda path: "stable-diff")
    return root


def _check(arguments, *, cwd=".", timeout=2, name="Check", check_id="test.check"):
    return VerificationCheck(check_id, name, tuple(arguments), cwd, timeout)


def _python(code):
    return (sys.executable, "-c", code)


def _run(tmp_path, monkeypatch, checks):
    monkeypatch.setattr(verification, "DEFAULT_CHECKS", tuple(checks))
    return run_verification(tmp_path)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, shell=False,
    )


def _registered_worktree(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "devflow-test@example.invalid")
    _git(repository, "config", "user.name", "DevFlow Test")
    (repository / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "base.txt")
    _git(repository, "commit", "-q", "-m", "base")
    target = repository / ".devflow-worktrees" / "run"
    _git(repository, "worktree", "add", "--detach", str(target), "HEAD")
    return repository, target


def test_successful_check_records_bounded_serializable_result(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = _run(tmp_path, monkeypatch, [_check(_python("print('ok')"))])
    assert results[0].status == "passed"
    assert results[0].exit_code == 0
    assert "ok" in results[0].stdout
    dumped = results[0].model_dump(mode="json")
    assert all(not hasattr(value, "fileno") for value in dumped.values())
    assert len(str(dumped)) < 20_000


def test_nonzero_exit_code_fails(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = _run(tmp_path, monkeypatch, [_check(_python("raise SystemExit(7)"))])[0]
    assert result.status == "failed" and result.exit_code == 7


def test_timeout_is_recorded_and_stops_following_checks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = _run(tmp_path, monkeypatch, [
        _check(_python("import time; time.sleep(5)"), timeout=1),
        _check(_python("print('should not run')"), check_id="test.next"),
    ])
    assert [item.status for item in results] == ["timed_out", "not_run"]
    assert results[0].exit_code is None
    assert results[1].duration_seconds == 0


def test_missing_executable_fails_and_does_not_continue(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    results = _run(tmp_path, monkeypatch, [
        _check(("definitely-no-such-devflow-executable",)),
        _check(_python("print('not run')"), check_id="test.next"),
    ])
    assert [item.status for item in results] == ["failed", "not_run"]


def test_invalid_and_escaping_working_directories_fail_safely(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    process_calls = []

    def unexpected_process(*args, **kwargs):
        process_calls.append((args, kwargs))
        raise AssertionError("Invalid working directories must be rejected before process launch.")

    monkeypatch.setattr(verification.subprocess, "run", unexpected_process)
    for cwd in ("missing", "../outside"):
        result = _run(tmp_path, monkeypatch, [_check(_python("pass"), cwd=cwd)])[0]
        assert result.status == "failed"
        assert result.stdout == result.stderr == ""
    assert process_calls == []


def test_stdout_and_stderr_are_bounded_with_truncation_flags(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    code = "import sys; print('o'*12000); print('e'*12000, file=sys.stderr)"
    result = _run(tmp_path, monkeypatch, [_check(_python(code))])[0]
    assert result.status == "passed"
    assert len(result.stdout.encode()) <= verification.MAX_EVIDENCE_BYTES
    assert len(result.stderr.encode()) <= verification.MAX_EVIDENCE_BYTES
    assert result.stdout_truncated and result.stderr_truncated


def test_command_uses_argument_array_shell_false_and_timeout(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    real_run = subprocess.run
    seen = []

    def recording_run(args, **kwargs):
        if isinstance(args, list) and args[:1] == [sys.executable]:
            seen.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(verification.subprocess, "run", recording_run)
    argv = _python("print('ok')")
    result = _run(tmp_path, monkeypatch, [_check(argv)])[0]
    assert result.status == "passed"
    assert seen[0][0] == list(argv)
    assert seen[0][1]["shell"] is False
    assert seen[0][1]["timeout"] == 2


def test_windows_npm_cmd_uses_node_directly_and_keeps_logical_identity(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    node_dir = tmp_path / "nodejs"
    npm_cmd = node_dir / "npm.cmd"
    node = node_dir / "node.exe"
    npm_cli = node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cmd.parent.mkdir(parents=True)
    node.write_bytes(b"node placeholder")
    npm_cmd.write_text("@echo off\n", encoding="utf-8")
    npm_cli.parent.mkdir(parents=True)
    npm_cli.write_text("// npm placeholder\n", encoding="utf-8")
    monkeypatch.setattr(verification, "_is_windows", lambda: True)
    monkeypatch.setattr(verification.shutil, "which", lambda command: str(npm_cmd) if command == "npm" else None)
    captured = {}

    def successful_process(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        kwargs["stdout"].write(b"typecheck passed\n")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(verification.subprocess, "run", successful_process)
    check = VerificationCheck(
        "frontend.typecheck", "Frontend typecheck", ("npm", "run", "typecheck"),
        "frontend", 300,
    )
    result = _run(tmp_path, monkeypatch, [check])[0]

    assert result.status == "passed"
    assert result.command_identity == ["npm", "run", "typecheck"]
    assert captured["arguments"] == [str(node.resolve()), str(npm_cli.resolve()), "run", "typecheck"]
    assert captured["kwargs"]["shell"] is False
    assert str(captured["kwargs"]["cwd"]) == str((root / "frontend").resolve())
    assert captured["kwargs"]["timeout"] == 300
    assert result.stdout == "typecheck passed"


def test_windows_npm_resolution_failure_is_reported_clearly(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(verification, "_is_windows", lambda: True)
    monkeypatch.setattr(verification.shutil, "which", lambda command: None)
    check = VerificationCheck(
        "frontend.typecheck", "Frontend typecheck", ("npm", "run", "typecheck"),
        "frontend", 300,
    )

    result = _run(tmp_path, monkeypatch, [check])[0]

    assert result.status == "failed"
    assert result.exit_code is None
    assert result.command_identity == ["npm", "run", "typecheck"]
    assert "Process launch failed" in result.stderr
    assert "Could not resolve npm" in result.stderr


def test_diff_change_invalidates_results(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    identities = iter(("before", "after"))
    monkeypatch.setattr(verification.worktree_tools, "diff_identity", lambda path: next(identities))

    def mutate_then_run(args, **kwargs):
        (root / "changed.py").write_text("changed", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(verification.subprocess, "run", mutate_then_run)
    result = _run(tmp_path, monkeypatch, [_check(("host-check",))])[0]
    assert result.status == "failed"
    assert "invalidated" in result.stdout


def test_public_runner_rejects_command_injection_parameter():
    with pytest.raises(TypeError):
        run_verification("unused", checks=[_check(("arbitrary-command",))])


def test_secret_redaction_covers_database_url_and_common_tokens():
    database_url = "postgres://dbuser:fake-db-password@db.invalid:5432/app"
    fake_token = "ghp_" + "a" * 32
    env = {
        "DATABASE_URL": database_url,
        "SERVICE_API_KEY": "fake-api-key-value",
        "PATH": "not-a-secret",
    }
    output = (
        f"connecting {database_url} password=fake-password "
        'and {"password":"fake-json-password"} '
        f"Authorization: Bearer fake-bearer-token {fake_token} "
        "AKIA1234567890ABCDEF"
    )
    redacted = verification._redact(output, env)
    for secret in (
        database_url, "fake-db-password", "fake-password", "fake-json-password", "fake-api-key-value",
        "fake-bearer-token", fake_token, "AKIA1234567890ABCDEF",
    ):
        assert secret not in redacted


def test_verification_result_stores_only_redacted_database_secret(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    secret = "postgres://user:fake-secret@db.invalid/app"
    monkeypatch.setenv("DATABASE_URL", secret)
    result = _run(
        tmp_path, monkeypatch,
        [_check(_python("import os; print(os.environ['DATABASE_URL'])"))],
    )[0]
    assert result.status == "passed"
    assert secret not in result.stdout
    assert "fake-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout


def test_non_timeout_process_error_fails(monkeypatch, tmp_path):
    _setup(tmp_path, monkeypatch)

    def denied(*args, **kwargs):
        raise PermissionError("launch denied")

    monkeypatch.setattr(verification.subprocess, "run", denied)
    result = _run(tmp_path, monkeypatch, [_check(("blocked-program",))])[0]
    assert result.status == "failed"
    assert result.exit_code is None
    assert "Process launch failed" in result.stderr


def test_symlinked_working_directories_are_contained_or_rejected(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    internal = root / "inside"
    internal.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "internal-link").symlink_to(internal, target_is_directory=True)
        (root / "external-link").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    assert verification._working_directory(root, "internal-link") == internal.resolve()
    with pytest.raises(ValueError):
        verification._working_directory(root, "external-link")


def test_safe_worktree_rejects_main_repository_and_accepts_registered_worktree(tmp_path):
    repository, implementation = _registered_worktree(tmp_path)
    with pytest.raises(ValueError):
        verification._safe_worktree(repository)
    assert verification._safe_worktree(implementation) == implementation.resolve()


def test_ignored_untracked_file_change_invalidates_verification(tmp_path, monkeypatch):
    _, implementation = _registered_worktree(tmp_path)
    monkeypatch.setattr(verification, "DEFAULT_CHECKS", (
        _check(_python("from pathlib import Path; Path('leak.ignored').write_text('fake')")),
    ))
    result = run_verification(implementation)[0]
    assert result.status == "failed"
    assert "invalidated" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Executable mode changes are not portable on Windows")
def test_untracked_executable_mode_change_invalidates_verification(tmp_path, monkeypatch):
    _, implementation = _registered_worktree(tmp_path)
    script = implementation / "new-script"
    script.write_text("echo safe\n", encoding="utf-8")
    script.chmod(0o644)
    monkeypatch.setattr(verification, "DEFAULT_CHECKS", (
        _check(_python("import os; os.chmod('new-script', 0o755)")),
    ))
    result = run_verification(implementation)[0]
    assert result.status == "failed"
    assert "invalidated" in result.stdout


def test_diff_identity_fails_when_snapshot_changes_during_capture(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    root.mkdir()
    snapshots = iter(("first", "second"))
    monkeypatch.setattr(verification.worktree_tools, "_diff_snapshot", lambda path: next(snapshots))
    with pytest.raises(RuntimeError, match="changed while diff identity"):
        verification.worktree_tools.diff_identity(root)


def test_results_never_store_process_handles_or_unbounded_logs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = _run(tmp_path, monkeypatch, [_check(_python("print('x'*500000)"))])[0]
    payload = result.model_dump(mode="json")
    assert set(payload) == {
        "check_id", "display_name", "command_identity", "working_directory", "working_directory_identity", "status",
        "exit_code", "duration_seconds", "stdout", "stderr", "stdout_truncated", "stderr_truncated",
    }
    assert len(payload["stdout"]) <= 8_192
    assert payload["stdout_truncated"] is True
