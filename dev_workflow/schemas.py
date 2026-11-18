"""Strict, serializable schemas for DevFlow proposals and execution evidence."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

_SECRET_LIKE = re.compile(
    r"(?i)(?:\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{12,})"
)


def codex_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build Codex/OpenAI strict-output JSON Schema from a Pydantic model.

    Structured-output APIs require all object keys to be required and reject
    unspecified properties. Pydantic defaults remain the application-level
    behavior; defaults are omitted from this transport schema, and a model's
    nullable unions remain nullable in the generated property schema.
    """
    schema = deepcopy(model.model_json_schema())

    def normalize(value: Any) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if value.get("type") == "object" or isinstance(properties, dict):
                properties = properties if isinstance(properties, dict) else {}
                value["required"] = list(properties)
                value["additionalProperties"] = False
            value.pop("default", None)
            for child in value.values():
                normalize(child)
        elif isinstance(value, list):
            for child in value:
                normalize(child)

    normalize(schema)
    return schema


def _safe_relative_path(value: str, *, allow_directory: bool) -> str:
    """Normalize a safe exact path or trailing-slash directory scope.

    Glob syntax and whitespace are deliberately unsupported: directory scopes
    use a literal path ending in ``/``; all other values are literal file paths.
    """
    if not isinstance(value, str):
        raise ValueError("Path must be a safe repository-relative path.")
    candidate = value
    portable = candidate.replace("\\", "/")
    directory = allow_directory and portable.endswith("/")
    trimmed = portable.rstrip("/") if directory else portable
    windows = PureWindowsPath(candidate)
    posix = PurePosixPath(trimmed)
    parts = trimmed.split("/")
    if (
        not candidate
        or "\x00" in candidate
        or any(ord(char) < 32 for char in candidate)
        or windows.is_absolute()
        or bool(windows.drive)
        or posix.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in candidate
        or any(char in candidate for char in "*?[]")
        or any(part.casefold() in {".git", ".devflow-worktrees"} for part in parts)
        or any(
            not all(char.isalnum() or char in "._-@+" for char in part)
            for part in parts
        )
    ):
        raise ValueError("Path must be a safe repository-relative path.")
    normalized = "/".join(posix.parts)
    return normalized + ("/" if directory else "")


class ImplementationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order: StrictInt = Field(ge=1)
    description: str = Field(min_length=1)


class ExpectedFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(
        min_length=1,
        description="One literal repository-relative file path; do not use prose, whitespace, or glob syntax.",
    )
    action: Literal["create", "modify", "delete"]
    rationale: str = Field(min_length=1)

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Path must be a safe repository-relative path.")
        if value != value.strip():
            raise ValueError("Path cannot have leading or trailing whitespace.")
        return _safe_relative_path(value, allow_directory=False)


class TechnicalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class FileScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    included_files: list[str] = Field(
        min_length=1,
        description=(
            "Literal repository-relative file paths or directory prefixes ending in '/'. "
            "Whitespace, prose, globs, absolute paths, and traversal are unsupported."
        ),
    )
    excluded_files: list[str] = Field(
        default_factory=list,
        description=(
            "Optional literal repository-relative file paths or directory prefixes ending in '/'. "
            "Use an empty array when no exclusions are needed; never write explanatory prose here."
        ),
    )

    @field_validator("included_files", "excluded_files", mode="before")
    @classmethod
    def validate_paths(cls, values: Any, info) -> Any:
        if isinstance(values, list):
            if any(not isinstance(value, str) for value in values):
                raise ValueError("File scope entries must be path strings.")
            if any(value != value.strip() for value in values):
                raise ValueError("File scope paths cannot have leading or trailing whitespace.")
        if not isinstance(values, list):
            return values
        allow_directory = info.field_name in {"included_files", "excluded_files"}
        normalized = [
            _safe_relative_path(value, allow_directory=allow_directory)
            for value in values
        ]
        keys = [value.rstrip("/").casefold() for value in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError("File scope contains duplicate paths.")
        return normalized

    @model_validator(mode="after")
    def reject_direct_scope_conflicts(self) -> "FileScope":
        for included in self.included_files:
            included_path = included.rstrip("/").casefold()
            for excluded in self.excluded_files:
                excluded_path = excluded.rstrip("/").casefold()
                excluded_is_dir = excluded.endswith("/")
                if (
                    included_path == excluded_path
                    or (
                        excluded_is_dir
                        and included_path.startswith(excluded_path + "/")
                    )
                ):
                    raise ValueError("Excluded paths cannot cover an included scope entry.")
        return self

    def allows(self, path: str) -> bool:
        """Return whether a normalized changed path is within this file scope."""
        candidate = path.replace("\\", "/").casefold()
        included = any(
            candidate == item.casefold()
            or (item.endswith("/") and candidate.startswith(item.casefold()))
            for item in self.included_files
        )
        excluded = any(
            candidate == item.casefold()
            or (item.endswith("/") and candidate.startswith(item.casefold()))
            for item in self.excluded_files
        )
        return included and not excluded


class TaskProposal(BaseModel):
    """Complete implementation plan presented to a human before write access."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_summary: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    implementation_steps: list[ImplementationStep] = Field(min_length=1)
    expected_files: list[ExpectedFileChange] = Field(min_length=1)
    file_scope: FileScope
    technical_decisions: list[TechnicalDecision] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    testing_strategy: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator(
        "requirements", "acceptance_criteria", "risks", "edge_cases",
        "testing_strategy", "assumptions",
    )
    @classmethod
    def strip_and_reject_blank_items(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("List items must not be empty.")
        return cleaned

    @model_validator(mode="after")
    def validate_order_and_expected_scope(self) -> "TaskProposal":
        orders = [step.order for step in self.implementation_steps]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("Implementation steps must be ordered consecutively from 1.")
        paths = [item.path.casefold() for item in self.expected_files]
        if len(paths) != len(set(paths)):
            raise ValueError("Expected file changes must not contain duplicate paths.")
        for item in self.expected_files:
            if not self.file_scope.allows(item.path):
                raise ValueError("Every expected file must be inside the included, non-excluded file scope.")
        return self


class WorkflowDecision(BaseModel):
    """A human decision bound to the exact proposal shown by the host."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "request_changes"]
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_revision: StrictInt = Field(ge=0)
    decision_timestamp: datetime
    feedback: str | None = None

    @field_validator("decision_timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_timestamp must include a timezone.")
        return value

    @field_validator("feedback")
    @classmethod
    def strip_feedback(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def require_revision_feedback(self) -> "WorkflowDecision":
        if self.action == "request_changes" and not self.feedback:
            raise ValueError("Feedback is required when requesting proposal changes.")
        return self


class CodexExecutionResult(BaseModel):
    """Bounded command result and evidence from one Codex CLI invocation."""

    model_config = ConfigDict(extra="forbid")

    command_identity: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    last_message: str | None
    parsed_result: dict[str, Any] | None
    duration_seconds: float = Field(ge=0)
    status: Literal["succeeded", "failed", "timeout"]
    timed_out: bool
    error: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "CodexExecutionResult":
        if self.timed_out != (self.status == "timeout"):
            raise ValueError("timed_out must agree with the execution status.")
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("Successful execution must have exit code zero.")
        if self.status == "timeout" and self.exit_code is not None:
            raise ValueError("Timed out execution must not claim an exit code.")
        return self


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: Literal["create", "modify", "delete"]


class DiffReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "invalid", "empty", "not_collected"]
    changed_files: list[ChangedFile] = Field(default_factory=list)
    diff: str = ""
    errors: list[str] = Field(default_factory=list)
    worktree_path: str | None = None


class VerificationCheckResult(BaseModel):
    """Bounded evidence for one host-configured verification check."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    check_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=160)
    command_identity: list[StrictStr] = Field(min_length=1, max_length=32)
    working_directory: Literal["repository", "worktree"]
    working_directory_identity: StrictStr | None = Field(default=None, min_length=1, max_length=2_048)
    status: Literal["passed", "failed", "timed_out", "not_run"]
    exit_code: StrictInt | None = None
    duration_seconds: float = Field(ge=0, le=86_400)
    stdout: str = Field(default="", max_length=8_192)
    stderr: str = Field(default="", max_length=8_192)
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @field_validator("command_identity")
    @classmethod
    def validate_command_identity(cls, value: list[str]) -> list[str]:
        if not value or any(not isinstance(part, str) or not part or len(part) > 2_048 for part in value):
            raise ValueError("Command identity must contain non-empty bounded arguments.")
        if sum(len(part) for part in value) > 8_192:
            raise ValueError("Command identity exceeds the configured size limit.")
        if any(_SECRET_LIKE.search(part) for part in value):
            raise ValueError("Command identity cannot contain secret-like values.")
        return value

    @field_validator("stdout", "stderr")
    @classmethod
    def reject_secret_like_output(cls, value: str) -> str:
        if _SECRET_LIKE.search(value):
            raise ValueError("Verification output must be redacted before it is stored.")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "VerificationCheckResult":
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("A passed check must have exit code zero.")
        if self.status == "timed_out" and self.exit_code is not None:
            raise ValueError("A timed-out check cannot claim an exit code.")
        if self.status == "not_run" and (self.exit_code is not None or self.duration_seconds != 0):
            raise ValueError("A not-run check cannot have an exit code or execution duration.")
        return self


class FinalReport(BaseModel):
    """Serializable, evidence-based summary of one DevFlow run."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(min_length=1, max_length=80)
    overall_status: Literal["passed", "failed", "rejected", "awaiting_approval"]
    task_summary: str = Field(min_length=1, max_length=2_000)
    approved_proposal_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_revision: StrictInt = Field(ge=0)
    human_decisions: list[WorkflowDecision] = Field(default_factory=list, max_length=20)
    implementation_status: Literal["succeeded", "failed", "timed_out", "not_run"]
    changed_files: list[ChangedFile] = Field(default_factory=list, max_length=500)
    diff_validation_status: Literal["valid", "invalid", "empty", "not_collected"]
    verification_results: list[VerificationCheckResult] = Field(default_factory=list, max_length=100)
    failures: list[str] = Field(default_factory=list, max_length=100)
    remaining_risks: list[str] = Field(default_factory=list, max_length=100)
    evidence_references: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("failures", "remaining_risks", "evidence_references")
    @classmethod
    def validate_report_entries(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2_048 for value in values):
            raise ValueError("Report entries must be non-empty and bounded.")
        return values

    @field_validator("evidence_references")
    @classmethod
    def validate_evidence_references(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            windows_path = PureWindowsPath(value)
            if (
                path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
                or ".." in windows_path.parts
                or "\x00" in value
            ):
                raise ValueError("Evidence references must be safe relative identities.")
        return values

    @model_validator(mode="after")
    def validate_report_outcome(self) -> "FinalReport":
        if self.overall_status == "passed" and (
            self.implementation_status != "succeeded"
            or self.diff_validation_status != "valid"
            or not self.verification_results
            or any(result.status != "passed" for result in self.verification_results)
            or self.failures
        ):
            raise ValueError("A passing report requires successful implementation, valid diff, and passing checks.")
        if self.overall_status == "rejected" and self.implementation_status != "not_run":
            raise ValueError("A rejected report must not claim implementation ran.")
        return self


class WriteCapabilityPreflightResult(BaseModel):
    """Evidence from a sandboxed write-capability check in a disposable worktree."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed"]
    preflight_run_id: str
    worktree_path: str | None
    evidence_path: str | None
    marker_path: str
    marker_contents: str
    marker_content_verified: bool
    scope_validated: bool
    execution_result: CodexExecutionResult | None
    diff_report: DiffReport | None
    cleanup_status: Literal["removed", "failed", "not_created", "not_attempted"]
    configuration_guidance: str | None = None
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_passed_evidence(self) -> "WriteCapabilityPreflightResult":
        if self.status == "passed":
            changed = self.diff_report.changed_files if self.diff_report else []
            if not (
                self.execution_result
                and self.execution_result.status == "succeeded"
                and self.marker_content_verified
                and self.scope_validated
                and self.cleanup_status == "removed"
                and self.diff_report
                and self.diff_report.status == "valid"
                and len(changed) == 1
                and changed[0].path == self.marker_path
                and changed[0].action == "create"
            ):
                raise ValueError("A passing preflight requires verified content, exact scope, and cleanup.")
        return self


class ReadOnlyProbeResult(BaseModel):
    """The intentionally small structured observation from live read-only probing."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    inspected_path: Literal["README.md"]
    first_nonempty_line: StrictStr = Field(min_length=1, max_length=240)


class LiveCodexPreflightReport(BaseModel):
    """Host-observed Codex execution readiness; help output is not execution evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    overall_status: Literal["passed", "failed"]
    cli_status: Literal["available", "unavailable"]
    authentication_status: Literal["passed", "failed", "process_launch_denied", "skipped"]
    read_only_status: Literal[
        "passed", "process_launch_denied", "sandbox_denied", "invalid_result", "failed", "skipped",
    ]
    workspace_write_status: Literal[
        "passed", "process_launch_denied", "sandbox_denied", "failed", "skipped",
    ]
    read_only_detail: StrictStr = Field(max_length=1_000)
    workspace_write_detail: StrictStr = Field(max_length=1_000)
    write_evidence_path: StrictStr | None = Field(default=None, max_length=2_048)

    @model_validator(mode="after")
    def validate_overall_status(self) -> "LiveCodexPreflightReport":
        all_passed = (
            self.cli_status == "available"
            and self.authentication_status == "passed"
            and self.read_only_status == "passed"
            and self.workspace_write_status == "passed"
        )
        if (self.overall_status == "passed") != all_passed:
            raise ValueError("Preflight can pass only when both live operations and authentication pass.")
        return self


def proposal_fingerprint(proposal: TaskProposal, *, revision: int = 0) -> str:
    """Fingerprint the validated plan together with its workflow revision number."""
    if type(revision) is not int or revision < 0:
        raise ValueError("Proposal revision must be a non-negative integer.")
    canonical = json.dumps(
        {"proposal": proposal.model_dump(mode="json"), "revision": revision},
        sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_decision_for_proposal(
    decision: WorkflowDecision,
    proposal: TaskProposal,
    *,
    expected_revision: int | None = None,
) -> WorkflowDecision:
    """Reject any decision that does not identify this exact validated plan."""
    if expected_revision is not None and decision.proposal_revision != expected_revision:
        raise ValueError("Decision is bound to a different proposal revision.")
    if decision.proposal_fingerprint != proposal_fingerprint(
        proposal, revision=decision.proposal_revision,
    ):
        raise ValueError("Decision is bound to a different task proposal.")
    return decision


def new_workflow_decision(
    proposal: TaskProposal,
    *,
    action: Literal["approve", "reject", "request_changes"],
    feedback: str | None = None,
    proposal_revision: int = 0,
    decision_timestamp: datetime | None = None,
) -> WorkflowDecision:
    """Build a host-side decision bound to the displayed proposal."""
    return WorkflowDecision(
        action=action,
        proposal_fingerprint=proposal_fingerprint(proposal, revision=proposal_revision),
        proposal_revision=proposal_revision,
        decision_timestamp=decision_timestamp or datetime.now(timezone.utc),
        feedback=feedback,
    )
