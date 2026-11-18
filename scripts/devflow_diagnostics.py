"""Read-only DevFlow host prerequisite diagnostic; safe to run before installation."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IMPORTS = {
    "python-docx": "docx",
    "pytest": "pytest",
    "langgraph": "langgraph",
    "langgraph-checkpoint-sqlite": "langgraph.checkpoint.sqlite",
    "langchain-core": "langchain_core",
    "langchain-ollama": "langchain_ollama",
    "langchain-groq": "langchain_groq",
    "python-dotenv": "dotenv",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "python-multipart": "multipart",
    "httpx": "httpx",
    "pydantic": "pydantic",
}
IMPLEMENTATION_OPTIONS = (
    "--ephemeral", "--ignore-user-config", "--sandbox", "--json",
    "--output-last-message",
)


def _resolve_codex(environ: dict[str, str], which: Any) -> tuple[str | None, str | None]:
    configured = environ.get("CODEX_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve()), None
        configured_error = "CODEX_CLI_PATH is not an existing file."
    else:
        configured_error = None
    found = which("codex")
    if found:
        return found, None
    return None, configured_error or "Codex CLI is not on PATH."


def _run(command: list[str], *, cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _status(completed: subprocess.CompletedProcess[str] | None) -> dict[str, Any]:
    if completed is None:
        return {"status": "failed", "detail": "Command could not be run or timed out."}
    output = (completed.stdout or completed.stderr or "").strip()
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "detail": output[:1000],
    }


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def run_diagnostics(*, root: Path = REPOSITORY_ROOT, environ: dict[str, str] | None = None,
                    which: Any = shutil.which) -> dict[str, Any]:
    """Check dependencies and host capabilities without running an agent task."""
    env = os.environ if environ is None else environ
    dependencies = {
        distribution: "available" if _module_available(module) else "missing"
        for distribution, module in REQUIRED_IMPORTS.items()
    }
    git = which("git")
    git_check = _status(_run([git or "git", "--version"], cwd=root)) if git else {
        "status": "failed", "detail": "Git was not found on PATH."
    }

    codex, resolve_error = _resolve_codex(env, which)
    codex_result: dict[str, Any] = {"executable": codex}
    if codex is None:
        codex_result.update({"status": "failed", "detail": resolve_error})
        version = {"status": "failed", "detail": resolve_error}
        analysis = implementation = authentication = {"status": "skipped", "detail": "Codex CLI unavailable."}
    else:
        version_run = _run([codex, "--version"], cwd=root)
        version = _status(version_run)
        help_run = _run([codex, "exec", "--help"], cwd=root)
        help_text = "" if help_run is None else f"{help_run.stdout or ''}\n{help_run.stderr or ''}"
        base_ok = help_run is not None and help_run.returncode == 0
        analysis_options = (
            *IMPLEMENTATION_OPTIONS[:-1], "--output-schema", "--output-last-message",
        )
        analysis_ok = base_ok and all(option in help_text for option in analysis_options) \
            and "read-only" in help_text
        implementation_ok = base_ok and all(option in help_text for option in IMPLEMENTATION_OPTIONS) \
            and "workspace-write" in help_text
        analysis = {"status": "passed" if analysis_ok else "failed",
                    "detail": "exec --help advertises read-only sandbox and structured output."}
        implementation = {"status": "passed" if implementation_ok else "failed",
                          "detail": "exec --help advertises workspace-write sandbox and output options."}
        authentication = _status(_run([codex, "login", "status"], cwd=root))
        codex_result["status"] = "available"

    checks = {
        "python": {
            "status": "passed" if (3, 12) <= sys.version_info < (3, 13) else "failed",
            "version": sys.version.split()[0], "required": ">=3.12,<3.13",
        },
        "python_dependencies": {
            "status": "passed" if all(value == "available" for value in dependencies.values()) else "failed",
            "packages": dependencies,
        },
        "git": git_check,
        "codex_cli": codex_result,
        "codex_version": version,
        "codex_authentication": authentication,
        "read_only_analysis_capability": analysis,
        "workspace_write_capability": implementation,
    }
    # Authentication is an external prerequisite for live model work; report it,
    # but do not make missing Codex/auth break WordPilot's document-editing app.
    app_required = ("python", "python_dependencies")
    checks["document_app_status"] = (
        "passed" if all(checks[name]["status"] == "passed" for name in app_required) else "failed"
    )
    devflow_required = (
        "python", "python_dependencies", "git", "codex_version",
        "codex_authentication", "read_only_analysis_capability",
        "workspace_write_capability",
    )
    checks["devflow_status"] = (
        "passed" if all(checks[name]["status"] == "passed" for name in devflow_required) else "failed"
    )
    checks["repository_root"] = str(root.resolve())
    checks["note"] = "No implementation task was run. Workspace-write support is only inferred from CLI help; run the mandatory live preflight before implementation."
    return checks


def main() -> int:
    result = run_diagnostics()
    print(json.dumps(result, indent=2))
    return 0 if result["devflow_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
