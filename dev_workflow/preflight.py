"""Sandboxed write-capability preflight for DevFlow's Codex implementation adapter."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from .codex_cli import implement_approved_plan
from .schemas import (
    CodexExecutionResult,
    DiffReport,
    ExpectedFileChange,
    FileScope,
    ImplementationStep,
    TaskProposal,
    TechnicalDecision,
    WriteCapabilityPreflightResult,
    proposal_fingerprint,
)
from .worktree import collect_worktree_diff, create_worktree, remove_worktree


def _preflight_proposal(marker_path: str, marker_contents: str) -> TaskProposal:
    return TaskProposal(
        task_summary="Check that Codex can write inside an isolated DevFlow worktree.",
        requirements=[f"Create only {marker_path} with the exact approved contents."],
        acceptance_criteria=[f"{marker_path} contains exactly {marker_contents!r}."],
        implementation_steps=[ImplementationStep(
            order=1,
            description=f"Create {marker_path} with exactly {marker_contents!r}; make no other changes.",
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
        testing_strategy=["The host checks exact marker contents and validates the complete diff."],
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
                marker_verified = marker.read_bytes() == marker_contents.encode("utf-8")
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
        errors.append("Preflight evidence could not be saved; the disposable worktree was retained.")
        cleanup_status = "not_attempted" if created_worktree else "not_created"

    if evidence_saved and created_worktree and worktree_path is not None:
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
            "On Windows, investigate the host-controlled Codex windows.sandbox backend selection "
            "and managed execution policy. DevFlow did not change sandbox configuration or retry "
            "with weaker permissions."
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
