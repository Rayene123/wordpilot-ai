"""Durable SQLite checkpoints and guarded human-approval resumption."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import ValidationError

from .graph import workflow_config
from .schemas import TaskProposal, WorkflowDecision, proposal_fingerprint, validate_decision_for_proposal


class CheckpointStoreError(RuntimeError):
    """A requested SQLite checkpoint store could not be opened safely."""


class ApprovalResumeError(ValueError):
    """A saved workflow is not a valid pending approval for this decision."""


@contextmanager
def open_sqlite_checkpointer(checkpoint_path: str | Path) -> Iterator[SqliteSaver]:
    """Open a durable SQLite saver at an explicit host-selected database path.

    Checkpoint deserialization is restricted to LangGraph's safe msgpack types.
    A missing database is initialized; a corrupt or unusable one raises an error
    and is never silently replaced with an in-memory saver.
    """
    raw_path = str(checkpoint_path)
    if not raw_path.strip() or raw_path == ":memory:":
        raise ValueError("A persistent checkpoint database file path is required.")
    path = Path(checkpoint_path).expanduser().resolve()
    if path.exists() and not path.is_file():
        raise CheckpointStoreError("Checkpoint database path is not a regular file.")

    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout = 10000")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity != ("ok",):
            raise CheckpointStoreError("Checkpoint database failed SQLite integrity validation.")
        saver = SqliteSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=None, pickle_fallback=False),
        )
        saver.setup()
    except CheckpointStoreError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        if connection is not None:
            connection.close()
        raise CheckpointStoreError(
            "Checkpoint database is unavailable, corrupt, or incompatible."
        ) from exc

    try:
        yield saver
    finally:
        connection.close()


def resume_pending_approval(
    graph: Any,
    run_id: str,
    decision_value: Mapping[str, Any] | WorkflowDecision,
) -> dict[str, Any]:
    """Resume only a valid saved approval interrupt for the current proposal.

    Invalid decisions are rejected before LangGraph consumes the interrupt, so
    the human can still submit a valid decision after correcting the payload.
    """
    config = workflow_config(run_id)
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:
        raise ApprovalResumeError(
            "Workflow checkpoint is missing, corrupt, or incompatible."
        ) from exc

    values = snapshot.values
    if not isinstance(values, Mapping) or not values or values.get("run_id") != run_id:
        raise ApprovalResumeError("No saved workflow exists for this run ID.")
    if (
        values.get("final_status") != "awaiting_approval"
        or values.get("current_stage") != "awaiting_approval"
        or tuple(snapshot.next) != ("request_approval",)
    ):
        raise ApprovalResumeError("Workflow has no pending approval interrupt.")

    pending = [
        interrupt
        for task in snapshot.tasks
        for interrupt in task.interrupts
    ]
    if len(pending) != 1:
        raise ApprovalResumeError("Pending approval interrupt is missing or ambiguous.")

    try:
        proposal = TaskProposal.model_validate(values.get("proposal"))
        revision = values.get("revision_count")
        fingerprint = values.get("proposal_fingerprint")
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid proposal revision.")
        expected_fingerprint = proposal_fingerprint(proposal, revision=revision)
        if fingerprint != expected_fingerprint:
            raise ValueError("Saved proposal fingerprint does not match its contents.")

        interrupt_value = pending[0].value
        if not isinstance(interrupt_value, Mapping) or any((
            interrupt_value.get("kind") != "implementation_proposal_review",
            interrupt_value.get("run_id") != run_id,
            interrupt_value.get("proposal_fingerprint") != fingerprint,
            interrupt_value.get("revision_count") != revision,
            interrupt_value.get("proposal") != proposal.model_dump(mode="json"),
        )):
            raise ValueError("Saved interrupt does not match the current proposal.")

        decision = WorkflowDecision.model_validate(decision_value)
        validate_decision_for_proposal(
            decision, proposal, expected_revision=revision,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise ApprovalResumeError(
            "Decision or saved approval checkpoint is invalid, stale, or mismatched."
        ) from exc

    try:
        return graph.invoke(Command(resume=decision.model_dump(mode="json")), config)
    except Exception as exc:
        raise ApprovalResumeError("LangGraph could not resume the validated approval interrupt.") from exc
