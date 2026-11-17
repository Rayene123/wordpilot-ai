"""Host-controlled Git worktree creation and approved-scope diff validation."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from .schemas import ChangedFile, DiffReport, TaskProposal

MAX_DIFF_BYTES = 256_000
MAX_FILE_BYTES = 128_000


def _git(repository: Path, *args: str, timeout: int = 30) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False,
    )
    if completed.returncode:
        raise RuntimeError("Host Git operation failed.")
    return completed.stdout


def create_worktree(repository_path: str | Path, run_id: str) -> str:
    """Create a detached worktree under a host-selected ignored directory."""
    repository = Path(repository_path).resolve()
    if not run_id or any(c not in "0123456789abcdef-" for c in run_id.lower()):
        raise ValueError("Invalid workflow run ID.")
    root = Path(_git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    worktree_parent = root / ".devflow-worktrees"
    if worktree_parent.is_symlink():
        raise ValueError("DevFlow worktree directory cannot be a symlink.")
    worktree_parent.mkdir(parents=True, exist_ok=True)
    if worktree_parent.resolve() != root / ".devflow-worktrees":
        raise ValueError("DevFlow worktree directory must remain inside the repository.")
    target = worktree_parent / run_id
    if target.exists():
        raise ValueError("Worktree target already exists.")
    _git(root, "worktree", "add", "--detach", str(target), "HEAD", timeout=120)
    return str(target)


def remove_worktree(repository_path: str | Path, worktree_path: str | Path, run_id: str) -> None:
    """Remove one precisely identified worktree after its evidence is saved."""
    repository = Path(repository_path).resolve()
    root = Path(_git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    supplied = Path(worktree_path)
    if supplied.is_symlink():
        raise ValueError("Refusing to remove a symlink as a worktree.")
    target = supplied.resolve()
    parent = root / ".devflow-worktrees"
    if parent.is_symlink() or target == root or target.parent != parent.resolve():
        raise ValueError("Refusing to remove a path outside the DevFlow worktree directory.")
    if target.name != run_id or not target.is_dir():
        raise ValueError("Worktree path does not match the generated run ID.")
    actual_root = Path(_git(target, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual_root != target:
        raise ValueError("Refusing to remove a path that is not a Git worktree root.")
    registered_paths = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git(root, "worktree", "list", "--porcelain").decode().splitlines()
        if line.startswith("worktree ")
    }
    if target not in registered_paths:
        raise ValueError("Worktree is not registered with the host repository.")
    _git(root, "worktree", "remove", "--force", str(target), timeout=120)


def collect_worktree_diff(worktree_path: str | Path, proposal: TaskProposal) -> DiffReport:
    """Collect tracked and untracked changes and validate exact approved scope."""
    worktree = Path(worktree_path).resolve()
    try:
        if worktree.parent.name != ".devflow-worktrees":
            raise ValueError("Diff collection requires a host-created DevFlow worktree.")
        git_root = Path(_git(worktree, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if git_root != worktree:
            raise ValueError("Diff collection path is not the worktree root.")
        raw = _git(worktree, "diff", "--name-status", "-z", "--no-renames", "HEAD")
        tokens = raw.decode("utf-8", errors="strict").split("\x00")
        tracked: dict[str, str] = {}
        index = 0
        while index + 1 < len(tokens):
            status, path = tokens[index], tokens[index + 1]
            index += 2
            if not path:
                continue
            action = {"A": "create", "M": "modify", "D": "delete"}.get(status[:1])
            if action is None:
                raise ValueError("Unsupported Git change type.")
            tracked[path.replace("\\", "/")] = action
        untracked_raw = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        for encoded_path in untracked_raw.split(b"\x00"):
            if not encoded_path:
                continue
            path = encoded_path.decode("utf-8", errors="strict").replace("\\", "/")
            tracked[path] = "create"
        ignored_raw = _git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
        ignored_paths = [p.decode("utf-8", errors="strict").replace("\\", "/")
                         for p in ignored_raw.split(b"\x00") if p]
        for path in ignored_paths:
            tracked.setdefault(path, "create")
        files = [ChangedFile(path=path, action=action) for path, action in sorted(tracked.items())]
        expected = {entry.path.casefold(): entry.action for entry in proposal.expected_files}
        errors: list[str] = []
        for item in files:
            if item.path in ignored_paths:
                errors.append(f"Ignored untracked file cannot be included in diff evidence: {item.path}")
            if not proposal.file_scope.allows(item.path):
                errors.append(f"Changed file is outside approved file scope: {item.path}")
            if expected.get(item.path.casefold()) != item.action:
                errors.append(f"Changed file does not match approved expected action: {item.path}")
            candidate = (worktree / item.path).resolve()
            if item.action != "delete" and (worktree not in candidate.parents or (worktree / item.path).is_symlink()):
                errors.append(f"Changed path is unsafe: {item.path}")
        changed_by_key = {path.casefold(): action for path, action in tracked.items()}
        for path, action in expected.items():
            if changed_by_key.get(path) != action:
                errors.append(f"Expected file change is missing or has a different action: {path}")
        patch = bytearray(_git(worktree, "diff", "--no-ext-diff", "--no-color", "--binary", "HEAD"))
        for item in files:
            if item.action != "create":
                continue
            path = worktree / item.path
            if path.is_symlink() or not path.is_file():
                raise ValueError("New file is not a regular file.")
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("New file exceeds the diff capture limit.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Binary untracked files are not supported.") from exc
            lines = list(difflib.unified_diff([], text.splitlines(keepends=True),
                                              fromfile="/dev/null", tofile=f"b/{item.path}"))
            patch.extend("".join(lines).encode("utf-8"))
            if len(patch) > MAX_DIFF_BYTES:
                raise ValueError("Diff exceeds the configured evidence limit.")
        if len(patch) > MAX_DIFF_BYTES:
            raise ValueError("Diff exceeds the configured evidence limit.")
        diff_text = bytes(patch).decode("utf-8", errors="replace")
        if not files:
            return DiffReport(status="empty", changed_files=[], diff=diff_text,
                              errors=["No files changed."], worktree_path=str(worktree))
        if errors:
            return DiffReport(status="invalid", changed_files=files, diff=diff_text,
                              errors=errors, worktree_path=str(worktree))
        return DiffReport(status="valid", changed_files=files, diff=diff_text, worktree_path=str(worktree))
    except (OSError, UnicodeError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return DiffReport(status="invalid", errors=["Diff could not be safely collected or validated."], worktree_path=str(worktree))
