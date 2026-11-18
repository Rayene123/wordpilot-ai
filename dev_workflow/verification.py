"""Host-defined, bounded verification for approved DevFlow worktrees."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .schemas import VerificationCheckResult
from . import worktree as worktree_tools

MAX_TIMEOUT_SECONDS = 900
MAX_EVIDENCE_BYTES = 8_192
_SECRET_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|"
    r"ACCESS[_-]?TOKEN|CLIENT[_-]?SECRET))\s*[=:]\s*[^\s,;]+",
    r"\bAuthorization\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?[A-Za-z0-9._~+/=-]+",
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"ya29\.[0-9A-Za-z._-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})",
    r"[a-z][a-z0-9+.-]*://[^:/@\s]+:[^@/\s]+@[^\s,;]+",
    r"['\"]?(?:password|passwd|pwd|token|api[_-]?key|client[_-]?secret|authorization)['\"]?"
    r"\s*[:=]\s*['\"]?[^\s'\",;}]+",
))
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIAL|COOKIE|"
    r"DATABASE_URL|DB_URL|CONNECTION_STRING|CONN_STR|PRIVATE|ACCESS)"
)


@dataclass(frozen=True)
class VerificationCheck:
    """One immutable application-configured check; paths are worktree-relative."""

    check_id: str
    display_name: str
    arguments: tuple[str, ...]
    working_directory: str
    timeout_seconds: int


# This is deliberately a short, fixed set. Proposal and implementation data never
# supplies command arguments. Applications may pass their own host-owned tuple.
DEFAULT_CHECKS = (
    VerificationCheck(
        "python.tests", "Python test suite", ("python", "-m", "pytest", "-q"),
        ".", 600,
    ),
    VerificationCheck(
        "frontend.typecheck", "Frontend typecheck", ("npm", "run", "typecheck"),
        "frontend", 300,
    ),
)


def _safe_worktree(path: str | Path) -> Path:
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("Verification requires a registered DevFlow worktree.")
    worktree = supplied.resolve(strict=True)
    if not worktree.is_dir() or worktree.parent.name != ".devflow-worktrees":
        raise ValueError("Verification requires a registered DevFlow worktree.")
    root_result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        check=False, shell=False,
    )
    if root_result.returncode:
        raise ValueError("Verification path is not a Git worktree.")
    root = Path(root_result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if root != worktree:
        raise ValueError("Verification path is not the worktree root.")
    parent = worktree.parent.parent
    listing = subprocess.run(
        ["git", "-C", str(parent), "worktree", "list", "--porcelain"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        check=False, shell=False,
    )
    registered = {
        Path(line.removeprefix(b"worktree ").decode("utf-8", errors="strict")).resolve()
        for line in listing.stdout.splitlines() if line.startswith(b"worktree ")
    }
    if listing.returncode or worktree not in registered:
        raise ValueError("Verification worktree is not registered with its repository.")
    return worktree


def _working_directory(worktree: Path, identity: str) -> Path:
    portable = identity.replace("\\", "/")
    win = PureWindowsPath(identity)
    posix = PurePosixPath(portable)
    if (
        not identity or "\x00" in identity or win.is_absolute() or win.drive
        or posix.is_absolute() or any(part in {"", ".", ".."} for part in portable.split("/"))
    ) and identity != ".":
        raise ValueError("Check working directory must be a safe worktree-relative path.")
    candidate = worktree if identity == "." else worktree.joinpath(*portable.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Check working directory does not exist inside the worktree.") from exc
    if not resolved.is_dir() or (resolved != worktree and worktree not in resolved.parents):
        raise ValueError("Check working directory is not a directory inside the worktree.")
    return resolved


def _redact(text: str, env: dict[str, str]) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    secrets = sorted(
        {value for key, value in env.items()
         if _SECRET_ENV_NAME.search(key)
         and len(value) >= 4},
        key=len, reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
        # Catch output truncated in the middle of a known secret value.
        for length in range(min(len(secret) - 1, len(text)), 3, -1):
            if text.endswith(secret[:length]):
                text = text[:-length] + "[REDACTED]"
                break
    return text


def _read_evidence(stream, env: dict[str, str]) -> tuple[str, bool]:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    data = stream.read(MAX_EVIDENCE_BYTES + 1)
    truncated = size > MAX_EVIDENCE_BYTES
    if truncated:
        data = data[:MAX_EVIDENCE_BYTES]
    text = _redact(data.decode("utf-8", errors="replace"), env)
    if len(text) > MAX_EVIDENCE_BYTES:
        text = text[:MAX_EVIDENCE_BYTES]
        truncated = True
    return text, truncated


def _result(check: VerificationCheck, status: str, *, exit_code: int | None = None,
            duration: float = 0, stdout: str = "", stderr: str = "",
            stdout_truncated: bool = False, stderr_truncated: bool = False):
    return VerificationCheckResult(
        check_id=check.check_id, display_name=check.display_name,
        command_identity=list(check.arguments), working_directory="worktree",
        working_directory_identity=check.working_directory,
        status=status, exit_code=exit_code, duration_seconds=min(duration, 86_400),
        stdout=stdout, stderr=stderr, stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _is_windows() -> bool:
    return os.name == "nt"


def _process_arguments(check: VerificationCheck) -> list[str]:
    """Resolve the fixed npm check to a directly launchable Windows process."""
    arguments = list(check.arguments)
    if not _is_windows() or check.arguments != ("npm", "run", "typecheck"):
        return arguments

    npm = shutil.which("npm")
    if not npm:
        raise FileNotFoundError("Could not resolve npm for the frontend typecheck.")
    npm_path = Path(npm).resolve()
    if npm_path.suffix.lower() == ".cmd":
        npm_cli = npm_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        node_path = npm_path.parent / "node.exe"
        if not node_path.is_file():
            node = shutil.which("node")
            node_path = Path(node).resolve() if node else node_path
        if not node_path.is_file() or not npm_cli.is_file():
            raise FileNotFoundError(
                "Could not resolve node.exe and npm-cli.js for the Windows npm launcher."
            )
        return [str(node_path), str(npm_cli), *arguments[1:]]
    if npm_path.suffix.lower() == ".exe":
        return [str(npm_path), *arguments[1:]]
    raise FileNotFoundError("The resolved Windows npm command is not directly launchable.")


def run_verification(
    worktree_path: str | Path,
) -> list[VerificationCheckResult]:
    """Run only the fixed host checks and bind them to one stable worktree diff."""
    return _run_configured_checks(worktree_path, DEFAULT_CHECKS)


def _run_configured_checks(
    worktree_path: str | Path,
    checks: tuple[VerificationCheck, ...],
) -> list[VerificationCheckResult]:
    """Private test seam; production callers use ``run_verification`` only."""
    worktree = _safe_worktree(worktree_path)
    if not checks or len(checks) > 100:
        raise ValueError("Host verification configuration must contain 1 to 100 checks.")
    if len({check.check_id for check in checks}) != len(checks):
        raise ValueError("Host verification check IDs must be unique.")
    for check in checks:
        if (not 1 <= check.timeout_seconds <= MAX_TIMEOUT_SECONDS
                or not check.arguments or any(not arg or "\x00" in arg for arg in check.arguments)):
            raise ValueError("Invalid host verification configuration.")
    before = worktree_tools.diff_identity(worktree)
    results: list[VerificationCheckResult] = []
    failed = False
    env = dict(os.environ)
    for check in checks:
        if failed:
            results.append(_result(check, "not_run"))
            continue
        start = time.monotonic()
        status, code = "failed", None
        out_text = err_text = ""
        out_truncated = err_truncated = False
        try:
            cwd = _working_directory(worktree, check.working_directory)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                try:
                    completed = subprocess.run(
                        _process_arguments(check), cwd=cwd, env=env, stdout=stdout_file,
                        stderr=stderr_file, timeout=check.timeout_seconds,
                        check=False, shell=False,
                    )
                    code = completed.returncode
                    status = "passed" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    status = "timed_out"
                out_text, out_truncated = _read_evidence(stdout_file, env)
                err_text, err_truncated = _read_evidence(stderr_file, env)
        except OSError as exc:
            status = "failed"
            detail = exc.strerror or str(exc) or type(exc).__name__
            err_text = _redact(f"Process launch failed: {detail}", env)
        except (ValueError, subprocess.SubprocessError):
            status = "failed"
        duration = time.monotonic() - start
        results.append(_result(check, status, exit_code=code, duration=duration,
                               stdout=out_text, stderr=err_text,
                               stdout_truncated=out_truncated, stderr_truncated=err_truncated))
        failed = status in {"failed", "timed_out"}
    try:
        after = worktree_tools.diff_identity(worktree)
    except (OSError, UnicodeError, RuntimeError, ValueError, subprocess.SubprocessError):
        after = ""
    if before != after:
        executed = [index for index, result in enumerate(results) if result.status != "not_run"]
        if executed:
            index = executed[-1]
            last = results[index]
            marker = "\n[Verification invalidated: worktree diff changed.]"
            combined_stdout = last.stdout + marker
            invalidated_stdout = combined_stdout[:MAX_EVIDENCE_BYTES]
            for executed_index in executed:
                item = results[executed_index]
                results[executed_index] = _result(
                    checks[executed_index], "failed", exit_code=item.exit_code,
                    duration=item.duration_seconds,
                    stdout=(invalidated_stdout if executed_index == index else item.stdout),
                    stderr=item.stderr,
                    stdout_truncated=(item.stdout_truncated or
                                      (executed_index == index and len(combined_stdout) > MAX_EVIDENCE_BYTES)),
                    stderr_truncated=item.stderr_truncated,
                )
        elif checks:
            results[0] = _result(
                checks[0], "failed",
                stdout="Verification invalidated: worktree diff changed before execution.",
            )
    return results
