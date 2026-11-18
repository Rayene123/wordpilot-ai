"""Interactive terminal interface for the DevFlow workflow."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import build_dev_workflow, new_workflow_state, open_sqlite_checkpointer
from . import workflow_config
from .checkpointing import ApprovalResumeError, resume_pending_approval
from .codex_cli import resolve_codex_executable
from .preflight import run_live_codex_preflight
from .schemas import FinalReport, TaskProposal, new_workflow_decision
from .verification import DEFAULT_CHECKS, run_verification

ROOT = Path.cwd().resolve()
DATABASE = ROOT / ".devflow" / "checkpoints.sqlite3"
LIVE_PREFLIGHT_PASSED = False


def _say(text: str = "") -> None:
    print(text, flush=True)


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def _read_task_description(input_line: Callable[[str], str] | None = None) -> str | None:
    """Collect complete task text until END, preserving blank and edge lines."""
    reader = input_line or input
    _say("Enter the full task description. Finish by entering END on its own line.")
    lines: list[str] = []
    while True:
        try:
            line = reader("Task> " if not lines else "")
        except EOFError:
            _say("Input ended before END; the incomplete task was discarded.")
            return None
        if line == "END":
            break
        lines.append(line)
    task = "\n".join(lines)
    if not task.strip():
        _say("A task description is required.")
        return None
    return task


def _graph(saver: Any):
    return build_dev_workflow(checkpointer=saver)


def _runs(graph: Any) -> list[tuple[str, dict[str, Any], tuple[str, ...]]]:
    found: dict[str, tuple[str, dict[str, Any], tuple[str, ...]]] = {}
    try:
        for checkpoint in graph.checkpointer.list(None):
            configurable = checkpoint.config.get("configurable", {})
            run_id = configurable.get("thread_id")
            if not isinstance(run_id, str) or run_id in found:
                continue
            snapshot = graph.get_state(workflow_config(run_id))
            if snapshot.values:
                found[run_id] = (run_id, dict(snapshot.values), tuple(snapshot.next))
    except Exception as exc:
        raise RuntimeError("Could not read workflow checkpoints.") from exc
    return sorted(found.values(), key=lambda item: str(item[1].get("task_description", "")).casefold())


def check_environment() -> bool:
    global LIVE_PREFLIGHT_PASSED
    LIVE_PREFLIGHT_PASSED = False
    _say("\nEnvironment check")
    _say(f"Python: {platform.python_version()} {'OK' if sys.version_info >= (3, 12) else 'install Python 3.12 or newer'}")
    git = shutil.which("git")
    _say(f"Git: {'available' if git else 'not found (install Git and add it to PATH)'}")
    codex, reason = resolve_codex_executable()
    if not codex:
        _say(f"Codex CLI: {reason}")
    else:
        _say(f"Codex CLI: {codex}")
    missing = [name for name in ("langgraph", "langgraph.checkpoint.sqlite", "pydantic") if importlib.util.find_spec(name) is None]
    _say("Python dependencies: " + ("missing " + ", ".join(missing) if missing else "available"))
    _say(f"Project config: {'found requirements.txt' if (ROOT / 'requirements.txt').is_file() else 'requirements.txt not found'}")
    if os.name == "nt":
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-ExecutionPolicy -Scope CurrentUser"], capture_output=True, text=True, timeout=10, shell=False)
            _say("PowerShell execution policy: " + (result.stdout.strip() or "could not determine"))
            _say("If activation is blocked, use .venv\\Scripts\\python.exe directly.")
        except (OSError, subprocess.TimeoutExpired):
            _say("PowerShell execution policy: could not determine")
    else:
        _say("Execution policy: not applicable to this shell")
    if os.name == "nt" and not os.environ.get("DEVFLOW_WINDOWS_SANDBOX"):
        _say("Implementation prerequisite: set DEVFLOW_WINDOWS_SANDBOX to an allowed backend (mxc, elevated, or unelevated).")
    _say("\nLive Codex execution preflight")
    live = run_live_codex_preflight(ROOT)
    _say(f"Overall: {live.overall_status}")
    _say(f"Codex CLI: {live.cli_status}")
    _say(f"Authentication: {live.authentication_status}")
    _say(f"Read-only execution: {live.read_only_status} — {live.read_only_detail}")
    _say(f"Workspace-write execution: {live.workspace_write_status} — {live.workspace_write_detail}")
    if live.write_evidence_path:
        _say(f"Write preflight evidence: {live.write_evidence_path}")
    _say("DevFlow task start is enabled only after all live preflight checks pass.")
    LIVE_PREFLIGHT_PASSED = live.overall_status == "passed"
    return LIVE_PREFLIGHT_PASSED


def _display_proposal(state: dict[str, Any]) -> TaskProposal:
    proposal = TaskProposal.model_validate(state.get("proposal"))
    _say("\nProposal")
    _say(proposal.task_summary)
    _say("Requirements:")
    for item in proposal.requirements:
        _say(f"  • {item}")
    _say("Files in scope:")
    for item in proposal.expected_files:
        _say(f"  • {item.action}: {item.path} — {item.rationale}")
    _say("Included: " + ", ".join(proposal.file_scope.included_files))
    _say("Excluded: " + (", ".join(proposal.file_scope.excluded_files) or "none specified"))
    _say("Acceptance criteria:")
    for item in proposal.acceptance_criteria:
        _say(f"  • {item}")
    if proposal.risks:
        _say("Risks: " + "; ".join(proposal.risks))
    return proposal


def _review(graph: Any, state: dict[str, Any]) -> None:
    proposal = _display_proposal(state)
    _say("\n1. Approve and implement in isolated worktree\n2. Reject\n3. Request proposal changes")
    choice = _ask("Decision: ")
    action = {"1": "approve", "2": "reject", "3": "request_changes"}.get(choice)
    if action is None:
        _say("No decision recorded. The approval remains pending.")
        return
    feedback = _ask("What should change? ") if action == "request_changes" else None
    if action == "request_changes" and not feedback:
        _say("Please provide change feedback. No decision recorded.")
        return
    decision = new_workflow_decision(proposal, action=action, feedback=feedback,
                                     proposal_revision=state.get("revision_count", 0))
    if action == "approve" and not LIVE_PREFLIGHT_PASSED:
        _say("Approval blocked: run option 1 and pass the live Codex preflight before implementation.")
        return
    result = resume_pending_approval(graph, state["run_id"], decision)
    _show_state(result)


def start_task(graph: Any) -> None:
    task = _read_task_description()
    if task is None:
        return
    state = new_workflow_state(task, ROOT)
    _say(f"Starting workflow {state['run_id']}…")
    try:
        config = workflow_config(state["run_id"])
        for update in graph.stream(state, config, stream_mode="updates"):
            for node in update:
                _say(f"Progress: {node.replace('_', ' ')}")
        result = dict(graph.get_state(config).values)
        _say(f"Stage: {result.get('current_stage', 'unknown')}")
        if result.get("current_stage") == "awaiting_approval":
            _review(graph, result)
        else:
            _show_state(result)
    except KeyboardInterrupt:
        _say("\nStopped at the last saved checkpoint. Choose Resume from the menu to continue.")


def _show_state(state: dict[str, Any], *, interrupted: bool = False) -> None:
    _say(f"Run ID: {state.get('run_id', 'unknown')}")
    _say(f"Stage: {state.get('current_stage', 'unknown')} | Status: {state.get('final_status', 'unknown')}")
    if interrupted:
        _say("Execution: interrupted at a saved workflow stage; resumable")
    _say(f"Approval: {(state.get('decisions') or [{}])[-1].get('action', 'pending')}")
    if state.get("worktree_path"):
        _say(f"Worktree: {state['worktree_path']}")
    execution = state.get("execution_result") or {}
    _say(f"Implementation: {execution.get('status', 'not run')}")
    checks = state.get("verification_results") or []
    _say("Verification: " + (", ".join(f"{item['check_id']}={item['status']}" for item in checks) if checks else "not run"))
    report = state.get("final_report")
    if report:
        final = FinalReport.model_validate(report)
        _say(f"Report: {final.overall_status}; diff {final.diff_validation_status}")
        _say("Changed files: " + (", ".join(
            f"{item.action}: {item.path}" for item in final.changed_files
        ) or "none"))
        if final.verification_results:
            _say("Verification results:")
            for check in final.verification_results:
                command = " ".join(check.command_identity)
                _say(f"  • {check.check_id}: {check.status} (exit {check.exit_code}); {command}")
        else:
            _say("Verification results: none")
        for failure in final.failures:
            _say(f"  ! {failure}")
    for error in state.get("errors", []):
        _say(f"  ! {error}")


def _select_run(runs: list[tuple[str, dict[str, Any], tuple[str, ...]]]) -> tuple[str, dict[str, Any], tuple[str, ...]] | None:
    if not runs:
        _say("No saved workflow runs found.")
        return None
    for index, (run_id, state, next_nodes) in enumerate(runs, 1):
        _say(f"{index}. {state.get('task_description', '(no description)')} — {state.get('current_stage', 'unknown')} — {run_id}")
    selection = _ask("Select a run number (or Enter to cancel): ")
    if not selection:
        return None
    try:
        return runs[int(selection) - 1]
    except (ValueError, IndexError):
        _say("That selection is not in the list.")
        return None


def resume_run(graph: Any) -> None:
    run = _select_run(_runs(graph))
    if run is None:
        return
    run_id, state, next_nodes = run
    if state.get("final_status") == "awaiting_approval":
        _review(graph, state)
    elif next_nodes:
        if not LIVE_PREFLIGHT_PASSED:
            _say("Resume blocked: run option 1 and pass the live Codex preflight before continuing implementation.")
            return
        try:
            _show_state(graph.invoke(None, workflow_config(run_id)))
        except KeyboardInterrupt:
            _say("\nStopped at the last saved checkpoint. Resume again when ready.")
    else:
        _show_state(state)


def inspect_status(graph: Any) -> None:
    run = _select_run(_runs(graph))
    if run:
        _show_state(run[1], interrupted=bool(run[2]) and run[1].get("final_status") != "awaiting_approval")


def run_checks(graph: Any) -> None:
    candidates = [run for run in _runs(graph) if run[1].get("worktree_path")]
    run = _select_run(candidates)
    if not run:
        return
    worktree = run[1]["worktree_path"]
    _say("Configured checks:")
    for check in DEFAULT_CHECKS:
        _say(f"  • {check.display_name}: {' '.join(check.arguments)} (in {check.working_directory}, timeout {check.timeout_seconds}s)")
    if _ask("Run these checks now in the registered worktree? [y/N] ").casefold() != "y":
        _say("Verification cancelled.")
        return
    try:
        for result in run_verification(worktree):
            _say(f"{result.display_name}: {result.status} (exit {result.exit_code}, {result.duration_seconds:.1f}s)")
    except (OSError, ValueError, RuntimeError):
        _say("Verification could not run safely. Check the worktree registration and dependencies.")


def main() -> int:
    _say("DevFlow — interactive development workflow")
    live_preflight_passed = False
    while True:
        try:
            _say("\n1. Check environment\n2. Start a new task\n3. Review pending approval\n4. Resume an existing workflow\n5. Inspect workflow status and report\n6. Run available verification\n7. Exit")
            choice = _ask("Choose an option: ")
            if choice == "7":
                return 0
            if choice == "1":
                live_preflight_passed = False
                live_preflight_passed = check_environment()
                continue
            if choice == "2" and not live_preflight_passed:
                _say("Task start blocked: choose option 1 and complete a successful live Codex preflight first.")
                continue
            with open_sqlite_checkpointer(DATABASE) as saver:
                graph = _graph(saver)
                if choice == "2":
                    start_task(graph)
                elif choice in {"3", "4"}:
                    pending = [item for item in _runs(graph) if item[1].get("final_status") == "awaiting_approval"]
                    run = _select_run(pending if choice == "3" else _runs(graph))
                    if run:
                        if run[1].get("final_status") == "awaiting_approval":
                            _review(graph, run[1])
                        elif choice == "4":
                            if run[2] and not live_preflight_passed:
                                _say("Resume blocked: run option 1 and pass the live Codex preflight first.")
                            else:
                                _show_state(graph.invoke(None, workflow_config(run[0])) if run[2] else run[1])
                        else:
                            _say("That run has no pending approval.")
                elif choice == "5":
                    inspect_status(graph)
                elif choice == "6":
                    run_checks(graph)
                else:
                    _say("Choose a number from 1 to 7.")
        except KeyboardInterrupt:
            _say("\nNo decision was recorded. Any saved checkpoint is safe to resume.")
        except (ApprovalResumeError, RuntimeError, ValueError) as exc:
            _say(f"DevFlow could not complete that action: {exc}")
        except (OSError, ImportError):
            _say("DevFlow could not open its checkpoint store. Check the project folder and installed dependencies.")


if __name__ == "__main__":
    raise SystemExit(main())
