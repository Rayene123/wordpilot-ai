from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from dev_workflow import preflight
from dev_workflow import codex_cli
from dev_workflow.schemas import CodexExecutionResult, DiffReport


RUN_ID = "12345678-1234-5678-1234-567812345678"


def test_preflight_and_implementation_share_the_same_adapter():
    assert preflight.implement_approved_plan is codex_cli.implement_approved_plan


def _execution(status="succeeded", *, stdout="", stderr="", message=None):
    return CodexExecutionResult(
        command_identity=["codex", "exec", "--sandbox", "workspace-write"],
        exit_code=0, stdout=stdout, stderr=stderr, last_message=message,
        parsed_result=None, duration_seconds=0.1, status=status, timed_out=False,
        error=None if status == "succeeded" else "Codex command execution was blocked by policy.",
    )


def _setup(monkeypatch, tmp_path, *, execution=None, content_ok=True, marker_contents=None,
           changed_files=None, diff_content_ok=True):
    repository = tmp_path / "repo"
    repository.mkdir()
    worktrees = []
    cleanup = []
    monkeypatch.setattr(preflight.uuid, "uuid4", lambda: UUID(RUN_ID))

    def create(repo, run_id):
        path = Path(repo) / ".devflow-worktrees" / run_id
        path.mkdir(parents=True)
        worktrees.append(path)
        return str(path)

    def implement(proposal, fingerprint, worktree, *, proposal_revision=0):
        marker = Path(worktree) / proposal.expected_files[0].path
        expected = f"WORDPILOT_DEVFLOW_WRITE_OK:{RUN_ID}\n"
        payload = marker_contents if marker_contents is not None else (expected if content_ok else "incorrect\n")
        marker.write_bytes(payload.encode("utf-8"))
        return execution or _execution()

    def collect(worktree, proposal):
        marker = Path(worktree) / proposal.expected_files[0].path
        actual_contents = marker.read_bytes().decode("utf-8")
        patch = "".join(difflib.unified_diff(
            [], actual_contents.splitlines(keepends=True), fromfile="/dev/null",
            tofile=f"b/{proposal.expected_files[0].path}",
        ))
        if not diff_content_ok:
            patch += "unexpected payload\n"
        return DiffReport(
            status="valid" if changed_files is None else "invalid",
            changed_files=changed_files or [{"path": proposal.expected_files[0].path, "action": "create"}],
            diff=patch, worktree_path=str(worktree),
        )

    def remove(repo, worktree, run_id):
        evidence = tmp_path / "evidence" / f"wordpilot-devflow-preflight-{run_id}.json"
        cleanup.append((Path(worktree), evidence.exists()))
        shutil.rmtree(worktree)

    monkeypatch.setattr(preflight, "remove_worktree", remove)
    return repository, create, implement, collect, cleanup, worktrees


def test_preflight_passes_only_after_marker_diff_and_evidence_are_verified(monkeypatch, tmp_path):
    repository, create, implement, collect, cleanup, worktrees = _setup(monkeypatch, tmp_path)
    result = preflight.run_write_capability_preflight(
        repository, implementer=implement, worktree_creator=create, diff_collector=collect,
        evidence_directory=tmp_path / "evidence",
    )

    assert result.status == "passed"
    assert result.marker_content_verified is True
    assert result.scope_validated is True
    assert result.cleanup_status == "removed"
    assert result.evidence_path and Path(result.evidence_path).is_file()
    assert cleanup == [(worktrees[0], True)]
    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    command_identity = evidence["execution_result"]["command_identity"]
    assert command_identity[command_identity.index("--sandbox") + 1] == "workspace-write"
    assert evidence["marker_contents"] == f"WORDPILOT_DEVFLOW_WRITE_OK:{RUN_ID}\n"


def test_preflight_fails_on_policy_denial_even_when_exit_code_is_zero(monkeypatch, tmp_path):
    blocked = _execution(
        "failed",
        stderr="CreateProcess rejected: blocked by policy",
        message="Workspace is mounted read-only.",
    )
    repository, create, implement, collect, cleanup, worktrees = _setup(
        monkeypatch, tmp_path, execution=blocked,
    )
    result = preflight.run_write_capability_preflight(
        repository, implementer=implement, worktree_creator=create, diff_collector=collect,
        evidence_directory=tmp_path / "evidence",
    )

    assert result.status == "failed"
    assert result.execution_result.exit_code == 0
    assert result.marker_content_verified is True
    assert result.scope_validated is True
    assert result.cleanup_status == "removed"
    assert cleanup == [(worktrees[0], True)]
    assert "windows.sandbox" in result.configuration_guidance


@pytest.mark.parametrize(
    ("marker_contents", "expected_status"),
    [
        (f"WORDPILOT_DEVFLOW_WRITE_OK:{RUN_ID}\n", "passed"),
        (f"WORDPILOT_DEVFLOW_WRITE_OK:{RUN_ID}\r\n", "passed"),
        ("incorrect\n", "failed"),
        (f"WORDPILOT_DEVFLOW_WRITE_OK:{RUN_ID}\nextra content\n", "failed"),
    ],
    ids=["lf", "crlf", "incorrect", "extra-content"],
)
def test_preflight_validates_exact_marker_contents_across_windows_line_endings(
    monkeypatch, tmp_path, marker_contents, expected_status,
):
    repository, create, implement, collect, cleanup, worktrees = _setup(
        monkeypatch, tmp_path, marker_contents=marker_contents,
    )
    result = preflight.run_write_capability_preflight(
        repository, implementer=implement, worktree_creator=create, diff_collector=collect,
        evidence_directory=tmp_path / "evidence",
    )

    assert result.status == expected_status
    assert result.marker_content_verified is (expected_status == "passed")
    assert result.cleanup_status == "removed"
    assert cleanup == [(worktrees[0], True)]


@pytest.mark.parametrize("case", ["unexpected_path", "unexpected_diff_content"])
def test_preflight_rejects_wrong_marker_or_unexpected_diff(monkeypatch, tmp_path, case):
    changed = ([{"path": "other.txt", "action": "create"}]
               if case == "unexpected_path" else None)
    repository, create, implement, collect, cleanup, worktrees = _setup(
        monkeypatch, tmp_path,
        changed_files=changed,
        diff_content_ok=case != "unexpected_diff_content",
    )
    result = preflight.run_write_capability_preflight(
        repository, implementer=implement, worktree_creator=create, diff_collector=collect,
        evidence_directory=tmp_path / "evidence",
    )

    assert result.status == "failed"
    assert result.cleanup_status == "removed"
    assert cleanup == [(worktrees[0], True)]


def test_preflight_does_not_require_git_status_from_codex(monkeypatch, tmp_path):
    ownership_error = "fatal: detected dubious ownership in repository"
    repository, create, implement, collect, cleanup, worktrees = _setup(
        monkeypatch, tmp_path, execution=_execution(stderr=ownership_error),
    )
    captured = {}

    def implement_without_git_status(proposal, fingerprint, worktree, *, proposal_revision=0):
        captured["proposal"] = proposal
        return implement(proposal, fingerprint, worktree, proposal_revision=proposal_revision)

    result = preflight.run_write_capability_preflight(
        repository, implementer=implement_without_git_status,
        worktree_creator=create, diff_collector=collect,
        evidence_directory=tmp_path / "evidence",
    )

    assert result.status == "passed"
    assert result.scope_validated is True
    assert "git status" in " ".join(captured["proposal"].requirements)
    assert "Do not run" in " ".join(captured["proposal"].requirements)
    assert result.cleanup_status == "removed"
    assert cleanup == [(worktrees[0], True)]


def test_preflight_retains_worktree_if_diagnostic_evidence_cannot_be_saved(monkeypatch, tmp_path):
    repository, create, implement, collect, cleanup, worktrees = _setup(monkeypatch, tmp_path)
    evidence_file = tmp_path / "evidence-is-a-file"
    evidence_file.write_text("do not overwrite", encoding="utf-8")
    result = preflight.run_write_capability_preflight(
        repository, implementer=implement, worktree_creator=create, diff_collector=collect,
        evidence_directory=evidence_file,
    )

    assert result.status == "failed"
    assert result.evidence_path is None
    assert result.cleanup_status == "not_attempted"
    assert worktrees[0].is_dir()
    assert cleanup == []
