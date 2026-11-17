"""Strict, serializable schemas for the development workflow foundation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class TaskProposal(BaseModel):
    """Structured task analysis produced by a read-only Codex CLI call."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_description: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    implementation_plan: list[str] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    proposed_files: list[str] = Field(default_factory=list)
    excluded_files: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator(
        "requirements", "acceptance_criteria", "implementation_plan",
        "risks", "proposed_files", "excluded_files", "assumptions",
    )
    @classmethod
    def strip_and_reject_blank_items(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("List items must not be empty.")
        return cleaned


class CodexExecutionResult(BaseModel):
    """Bounded result and evidence from one Codex CLI analysis invocation."""

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


class WorkflowApproval(BaseModel):
    """A human decision bound to one exact proposal fingerprint."""

    model_config = ConfigDict(extra="forbid")

    approved: StrictBool
    proposal_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_timestamp: datetime

    @field_validator("decision_timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_timestamp must include a timezone.")
        return value


def proposal_fingerprint(proposal: TaskProposal) -> str:
    """Return a stable SHA-256 fingerprint of a validated proposal."""
    canonical = json.dumps(
        proposal.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_approval_for_proposal(
    approval: WorkflowApproval,
    proposal: TaskProposal,
) -> WorkflowApproval:
    """Reject an approval unless it names the exact proposal being considered."""
    if approval.proposal_fingerprint != proposal_fingerprint(proposal):
        raise ValueError("Approval is bound to a different task proposal.")
    return approval


def new_approval(
    proposal: TaskProposal,
    *,
    approved: bool,
    decision_timestamp: datetime | None = None,
) -> WorkflowApproval:
    """Build an explicit, proposal-bound approval record for a human decision."""
    return WorkflowApproval(
        approved=approved,
        proposal_fingerprint=proposal_fingerprint(proposal),
        decision_timestamp=decision_timestamp or datetime.now(timezone.utc),
    )
