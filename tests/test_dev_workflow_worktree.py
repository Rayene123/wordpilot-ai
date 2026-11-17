from __future__ import annotations

from pathlib import Path

from dev_workflow import worktree
from dev_workflow.schemas import TaskProposal


def _proposal():
    return TaskProposal.model_validate({
        "task_summary": "Add field.", "requirements": ["Keep existing output."],
        "acceptance_criteria": ["Field is present."],
        "implementation_steps": [{"order": 1, "description": "Update endpoint."}],
        "expected_files": [{"path": "backend/api.py", "action": "modify", "rationale": "Add field."}],
        "file_scope": {"included_files": ["backend/api.py"]},
        "technical_decisions": [{"decision": "Use current schema", "rationale": "Consistency."}],
        "testing_strategy": ["Run API tests."],
    })


def _proposal_for(path, action):
    return TaskProposal.model_validate({
        "task_summary": "Change one scoped file.",
        "requirements": ["Change the declared file only."],
        "acceptance_criteria": ["The declared file action is reflected in the diff."],
        "implementation_steps": [{"order": 1, "description": "Make the declared file change."}],
        "expected_files": [{"path": path, "action": action, "rationale": "Explicitly approved file action."}],
        "file_scope": {"included_files": [path]},
        "technical_decisions": [{"decision": "Use a one-file scope", "rationale": "Keep the change bounded."}],
        "testing_strategy": ["Validate the resulting path and action in the diff."],
    })


def test_create_worktree_uses_fixed_detached_host_git_command(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    calls = []

    def fake_git(repository, *args, timeout=30):
        calls.append((repository, args, timeout))
        if args == ("rev-parse", "--show-toplevel"):
            return str(root).encode()
        return b""

    monkeypatch.setattr(worktree, "_git", fake_git)
    run_id = "123e4567-e89b-12d3-a456-426614174000"
    result = worktree.create_worktree(root, run_id)
    assert result == str(root / ".devflow-worktrees" / run_id)
    assert calls[-1][1][:3] == ("worktree", "add", "--detach")
    assert calls[-1][1][-1] == "HEAD"


def test_diff_validation_accepts_only_expected_scoped_changes(tmp_path, monkeypatch):
    worktree_path = tmp_path / ".devflow-worktrees" / "run"
    (worktree_path / "backend").mkdir(parents=True)
    (worktree_path / "backend" / "api.py").write_text("new", encoding="utf-8")

    def fake_git(repository, *args, timeout=30):
        if args == ("rev-parse", "--show-toplevel"):
            return str(worktree_path).encode()
        if args[:2] == ("diff", "--name-status"):
            return b"M\x00backend/api.py\x00"
        if args[:2] == ("ls-files", "--others"):
            return b""
        return b"diff --git a/backend/api.py b/backend/api.py\n+new\n"

    monkeypatch.setattr(worktree, "_git", fake_git)
    report = worktree.collect_worktree_diff(worktree_path, _proposal())
    assert report.status == "valid"
    assert report.changed_files[0].path == "backend/api.py"
    assert "+new" in report.diff


def test_diff_validation_rejects_unapproved_path(tmp_path, monkeypatch):
    worktree_path = tmp_path / ".devflow-worktrees" / "run"
    worktree_path.mkdir(parents=True)

    def fake_git(repository, *args, timeout=30):
        if args == ("rev-parse", "--show-toplevel"):
            return str(worktree_path).encode()
        if args[:2] == ("diff", "--name-status"):
            return b"M\x00backend/other.py\x00"
        if args[:2] == ("ls-files", "--others"):
            return b""
        return b""

    monkeypatch.setattr(worktree, "_git", fake_git)
    report = worktree.collect_worktree_diff(worktree_path, _proposal())
    assert report.status == "invalid"
    assert any("scope" in error for error in report.errors)


def test_diff_validation_accepts_approved_create_action(tmp_path, monkeypatch):
    worktree_path = tmp_path / ".devflow-worktrees" / "run"
    new_file = worktree_path / "tests" / "devflow_demo" / "README.md"
    new_file.parent.mkdir(parents=True)
    new_file.write_text("Synthetic demo only.\n", encoding="utf-8")

    def fake_git(repository, *args, timeout=30):
        if args == ("rev-parse", "--show-toplevel"):
            return str(worktree_path).encode()
        if args[:2] == ("diff", "--name-status") or "--ignored" in args:
            return b""
        if args[:2] == ("ls-files", "--others"):
            return b"tests/devflow_demo/README.md\x00"
        return b""

    monkeypatch.setattr(worktree, "_git", fake_git)
    report = worktree.collect_worktree_diff(
        worktree_path, _proposal_for("tests/devflow_demo/README.md", "create"),
    )
    assert report.status == "valid"
    assert [(entry.path, entry.action) for entry in report.changed_files] == [
        ("tests/devflow_demo/README.md", "create")
    ]


def test_diff_validation_accepts_approved_delete_action(tmp_path, monkeypatch):
    worktree_path = tmp_path / ".devflow-worktrees" / "run"
    worktree_path.mkdir(parents=True)

    def fake_git(repository, *args, timeout=30):
        if args == ("rev-parse", "--show-toplevel"):
            return str(worktree_path).encode()
        if args[:2] == ("diff", "--name-status"):
            return b"D\x00backend/api.py\x00"
        if args[:2] == ("ls-files", "--others"):
            return b""
        return b"diff --git a/backend/api.py b/backend/api.py\n-deleted\n"

    monkeypatch.setattr(worktree, "_git", fake_git)
    report = worktree.collect_worktree_diff(
        worktree_path, _proposal_for("backend/api.py", "delete"),
    )
    assert report.status == "valid"
    assert [(entry.path, entry.action) for entry in report.changed_files] == [
        ("backend/api.py", "delete")
    ]
