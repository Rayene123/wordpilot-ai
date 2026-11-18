"""Sandboxed write-capability preflight for DevFlow's Codex implementation adapter."""

from __future__ import annotations

import difflib
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from .codex_cli import (
    check_codex_authentication,
    implement_approved_plan,
    resolve_codex_executable,
    run_read_only_preflight,
)
from .schemas import (
    CodexExecutionResult,
    DiffReport,
    ExpectedFileChange,
    FileScope,
    ImplementationStep,
    LiveCodexPreflightReport,
    ReadOnlyProbeResult,
    TaskProposal,
    TechnicalDecision,
    WriteCapabilityPreflightResult,
    proposal_fingerprint,
)
from .worktree import collect_worktree_diff, create_worktree, remove_worktree


def _preflight_proposal(marker_path: str, marker_contents: str) -> TaskProposal:
    marker_without_final_lf = marker_contents[:-1] if marker_contents.endswith("\n") else marker_contents
    powershell_marker_writer = (
        f"[System.IO.File]::WriteAllText('{marker_path}', "
        f"'{marker_without_final_lf}' + \"`n\", "
        "[System.Text.UTF8Encoding]::new($false))"
    )
    return TaskProposal(
        task_summary=(
            "Check that Codex can write inside an isolated DevFlow worktree by creating one marker. "
            "Do not run Git commands; the host verifies the resulting diff."
        ),
        requirements=[
            f"Create only {marker_path} with the exact approved contents.",
            "Do not run git status or any other Git command; host-side diff validation is authoritative.",
        ],
        acceptance_criteria=[f"{marker_path} contains exactly {marker_contents!r}."],
        implementation_steps=[ImplementationStep(
            order=1,
            description=(
                f"Create {marker_path} with exactly {marker_contents!r}; make no other changes. "
                "On Windows PowerShell, use this exact writer to preserve the trailing LF and write "
                f"UTF-8 without a BOM: {powershell_marker_writer}. "
                "Do not use Set-Content or Add-Content for this marker. "
                "Do not run git status or any other Git command; the host validates the diff."
            ),
        )],
        expected_files=[ExpectedFileChange(
            path=marker_path,
            action="create",
            rationale="A disposable marker verifies the configured workspace-write capability.",
        )],
        file_scope=FileScope(included_files=[marker_path], excluded_files=[]),
        technical_decisions=[TechnicalDecision(
            decision="Use one uniquely named plain-text marker.",
            rationale="The host can verify the exact bytes and complete Git diff.",
        )],
        testing_strategy=[
            "The host checks exact marker contents and validates the complete diff; no Git command is needed from Codex."
        ],
    )


def _expected_marker_diff(marker_path: str, marker_contents: str) -> str:
    return "".join(difflib.unified_diff(
        [], marker_contents.splitlines(keepends=True),
        fromfile="/dev/null", tofile=f"b/{marker_path}",
    ))


def _classify_execution_error(error: str | None, *, invalid_result: bool = False) -> str:
    if invalid_result:
        return "invalid_result"
    lowered = (error or "").casefold()
    if "invalid structured" in lowered or "did not return the structured" in lowered:
        return "invalid_result"
    if "could not be started" in lowered or "process launch" in lowered:
        return "process_launch_denied"
    if "sandbox" in lowered or "execution-policy" in lowered or "policy refusal" in lowered:
        return "sandbox_denied"
    return "failed"


def _first_readme_line(repository: Path) -> str | None:
    path = repository / "README.md"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for _ in range(12):
                line = stream.readline(242)
                if not line:
                    break
                if line.strip():
                    value = line.strip()
                    return value if len(value) <= 240 else None
    except (OSError, UnicodeError):
        return None
    return None


def run_live_codex_preflight(
    repository_path: str | Path,
    *,
    authentication_check: Callable[..., tuple[str, str]] = check_codex_authentication,
    read_only_probe: Callable[..., CodexExecutionResult] = run_read_only_preflight,
    write_probe: Callable[..., WriteCapabilityPreflightResult] | None = None,
) -> LiveCodexPreflightReport:
    """Prove live CLI auth, bounded read-only inspection, and isolated write access."""
    repository = Path(repository_path).expanduser().resolve()
    executable, resolution_error = resolve_codex_executable()
    if executable is None:
        return LiveCodexPreflightReport(
            overall_status="failed", cli_status="unavailable", authentication_status="skipped",
            read_only_status="skipped", workspace_write_status="skipped",
            read_only_detail=resolution_error or "Codex CLI is unavailable.",
            workspace_write_detail="Skipped because Codex CLI is unavailable.",
        )

    authentication_status, authentication_detail = authentication_check(executable, repository)
    if authentication_status != "passed":
        return LiveCodexPreflightReport(
            overall_status="failed", cli_status="available",
            authentication_status=authentication_status,
            read_only_status="skipped", workspace_write_status="skipped",
            read_only_detail="Skipped because Codex authentication failed.",
            workspace_write_detail=(authentication_detail or "Codex authentication failed.")[:1_000],
        )

    try:
        observed_line = _first_readme_line(repository)
        read_result = CodexExecutionResult.model_validate(read_only_probe(repository, executable=executable))
        if read_result.status == "succeeded" and read_result.parsed_result is not None:
            try:
                observation = ReadOnlyProbeResult.model_validate(read_result.parsed_result)
                if observed_line is None or observation.first_nonempty_line != observed_line:
                    raise ValueError("The bounded repository observation did not match host content.")
                read_status = "passed"
                read_detail = "Codex inspected the first non-empty line of README.md within its first 12 lines; structured result matched host content."
            except (TypeError, ValueError):
                read_status = "invalid_result"
                read_detail = "Codex read-only operation returned a mismatched or invalid structured observation."
        else:
            read_status = _classify_execution_error(read_result.error)
            read_detail = read_result.error or "Codex read-only operation failed."
    except Exception as exc:
        read_status = "failed"
        read_detail = f"Read-only preflight could not complete safely ({type(exc).__name__})."

    try:
        runner = write_probe or run_write_capability_preflight
        write_result = WriteCapabilityPreflightResult.model_validate(runner(repository))
    except Exception as exc:
        write_result = None
        write_error = f"Workspace-write preflight could not complete safely ({type(exc).__name__})."
    else:
        write_error = ""
    if write_result and write_result.status == "passed":
        write_status = "passed"
        write_detail = "Codex created the exact marker in a disposable worktree; host diff validation passed and the worktree was removed."
    else:
        execution_error = write_result.execution_result.error if write_result and write_result.execution_result else None
        write_status = _classify_execution_error(execution_error)
        write_detail = execution_error or ("; ".join(write_result.errors) if write_result else "") or write_error or "Workspace-write preflight failed safe validation or cleanup."

    overall = "passed" if read_status == "passed" and write_status == "passed" else "failed"
    return LiveCodexPreflightReport(
        overall_status=overall, cli_status="available", authentication_status="passed",
        read_only_status=read_status, workspace_write_status=write_status,
        read_only_detail=read_detail[:1_000], workspace_write_detail=write_detail[:1_000],
        write_evidence_path=write_result.evidence_path if write_result else None,
    )


def run_write_capability_preflight(
    repository_path: str | Path,
    *,
    implementer: Callable[..., CodexExecutionResult] = implement_approved_plan,
    worktree_creator: Callable[..., str] = create_worktree,
    diff_collector: Callable[..., DiffReport] = collect_worktree_diff,
    evidence_directory: str | Path | None = None,
) -> WriteCapabilityPreflightResult:
    """Probe workspace-write in a generated worktree, save evidence, then remove it.

    The configured implementation adapter is reused so the executable, flags,
    inherited environment, and ``cwd`` handling match real task implementation.
    Any failed or unverifiable result fails closed. Sandbox settings are not
    modified, and the disposable worktree is removed only after evidence is saved.
    """
    repository = Path(repository_path).expanduser().resolve()
    preflight_run_id = str(uuid.uuid4())
    marker_path = f"devflow_preflight_{preflight_run_id.replace('-', '')}.txt"
    marker_contents = f"WORDPILOT_DEVFLOW_WRITE_OK:{preflight_run_id}\n"
    proposal = _preflight_proposal(marker_path, marker_contents)
    fingerprint = proposal_fingerprint(proposal, revision=0)
    directory = Path(evidence_directory) if evidence_directory is not None else Path(tempfile.gettempdir())
    evidence_path = directory / f"wordpilot-devflow-preflight-{preflight_run_id}.json"

    worktree_path: str | None = None
    created_worktree = False
    execution: CodexExecutionResult | None = None
    diff: DiffReport | None = None
    marker_verified = False
    scope_validated = False
    cleanup_status = "not_created"
    errors: list[str] = []

    try:
        worktree_path = str(worktree_creator(repository, preflight_run_id))
        expected_worktree = (repository / ".devflow-worktrees" / preflight_run_id).resolve()
        actual_worktree = Path(worktree_path).resolve()
        if actual_worktree != expected_worktree or not actual_worktree.is_dir():
            raise ValueError("Worktree creator returned an unexpected path.")
        created_worktree = True
        marker = actual_worktree / marker_path
        if marker.exists() or marker.is_symlink():
            raise ValueError("Preflight marker path already exists.")

        raw_execution = implementer(
            proposal, fingerprint, str(actual_worktree), proposal_revision=0,
        )
        execution = CodexExecutionResult.model_validate(raw_execution)
        if execution.status != "succeeded":
            errors.append(execution.error or "Codex reported an execution failure.")

        if marker.is_file() and not marker.is_symlink():
            try:
                actual_marker = marker.read_bytes().replace(b"\r\n", b"\n")
                marker_verified = actual_marker == marker_contents.encode("utf-8")
            except OSError:
                marker_verified = False
        if not marker_verified:
            errors.append("Host did not find the exact preflight marker contents.")

        try:
            diff = DiffReport.model_validate(diff_collector(str(actual_worktree), proposal))
            scope_validated = (
                diff.status == "valid"
                and len(diff.changed_files) == 1
                and diff.changed_files[0].path == marker_path
                and diff.changed_files[0].action == "create"
                and diff.diff.replace("\r\n", "\n") == _expected_marker_diff(marker_path, marker_contents)
            )
        except Exception:
            diff = DiffReport(
                status="not_collected", errors=["Host diff collection failed."],
                worktree_path=str(actual_worktree),
            )
        if not scope_validated:
            errors.append("Preflight diff did not contain exactly the approved marker creation.")
    except Exception as exc:
        # Keep process, policy, and path details out of the workflow report.
        errors.append(f"Preflight could not complete safely ({type(exc).__name__}).")

    base_status = bool(execution and execution.status == "succeeded" and marker_verified and scope_validated and not errors)
    record: dict[str, Any] = {
        "preflight_run_id": preflight_run_id,
        "repository_path": str(repository),
        "worktree_path": worktree_path,
        "marker_path": marker_path,
        "marker_contents": marker_contents,
        "marker_content_verified": marker_verified,
        "scope_validated": scope_validated,
        "execution_result": execution.model_dump(mode="json") if execution else None,
        "diff_report": diff.model_dump(mode="json") if diff else None,
        "errors": list(errors),
        "cleanup_status": "pending",
        "status": "ready_for_cleanup" if base_status else "failed",
    }
    evidence_saved = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence_saved = True
    except OSError:
        errors.append("Preflight evidence could not be saved; cleanup will still be attempted.")
        cleanup_status = "not_attempted" if created_worktree else "not_created"

    if created_worktree and worktree_path is not None:
        try:
            remove_worktree(repository, worktree_path, preflight_run_id)
            cleanup_status = "removed"
        except Exception:
            cleanup_status = "failed"
            errors.append("Disposable preflight worktree cleanup failed; it was retained for inspection.")
    elif evidence_saved:
        cleanup_status = "not_created"

    status = "passed" if base_status and cleanup_status == "removed" and not errors else "failed"
    guidance = None
    if status == "failed":
        guidance = (
            "On Windows, set DEVFLOW_WINDOWS_SANDBOX to mxc, elevated, or unelevated, then confirm "
            "the selected windows.sandbox backend is available and permitted by host or managed "
            "policy. DevFlow did not change sandbox configuration or retry with another backend."
        )
    result = WriteCapabilityPreflightResult(
        status=status,
        preflight_run_id=preflight_run_id,
        worktree_path=worktree_path,
        evidence_path=str(evidence_path) if evidence_saved else None,
        marker_path=marker_path,
        marker_contents=marker_contents,
        marker_content_verified=marker_verified,
        scope_validated=scope_validated,
        execution_result=execution,
        diff_report=diff,
        cleanup_status=cleanup_status,  # type: ignore[arg-type]
        configuration_guidance=guidance,
        errors=errors,
    )
    if evidence_saved:
        record.update(result.model_dump(mode="json"))
        try:
            evidence_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # The initial evidence was saved before cleanup; keep its location and report the update failure.
            result.status = "failed"
            result.configuration_guidance = guidance
            result.errors.append("Final cleanup status could not be written to the evidence file.")
    return result
