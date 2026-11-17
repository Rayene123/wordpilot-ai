"""Run real Groq planner cases on synthetic DOCX files only.

Set GROQ_API_KEY, then run:
  $env:WORDPILOT_LLM_PROVIDER='groq'
  $env:GROQ_MODEL='openai/gpt-oss-120b'
  python -u scripts/groq_planner_integration.py

This script calls plan_document only. It never executes proposed operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from docx import Document
from dotenv import load_dotenv
from langchain_core.messages import BaseMessage
from pydantic import TypeAdapter
from pydantic import BaseModel
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent.orchestration import plan_document  # noqa: E402
from backend.agent import orchestration  # noqa: E402
from backend.agent.provider import PlanningProviderError, create_planning_provider  # noqa: E402
from backend.agent.schemas import DocumentOperation  # noqa: E402

OPERATIONS = TypeAdapter(list[DocumentOperation])
CASES = [
    ("simple replacement", "Replace 'Draft' with 'Final'.", "awaiting_approval", [
        {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
    ]),
    ("paragraph insertion", "Insert the paragraph 'Launch review scheduled.' after 'Milestones'.", "awaiting_approval", [
        {"operation_type": "insert", "anchor_paragraph_text": "Milestones", "inserted_text": "Launch review scheduled.",
         "position": "after", "paragraph_style": None, "location": None},
    ]),
    ("paragraph removal", "Remove the paragraph 'Remove this note'.", "awaiting_approval", [
        {"operation_type": "remove", "target_text": "Remove this note", "location": None},
    ]),
    ("multiple instructions", "Replace 'Draft' with 'Final' and remove the paragraph 'Remove this note'.", "awaiting_approval", [
        {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
        {"operation_type": "remove", "target_text": "Remove this note", "location": None},
    ]),
    ("missing target", "Replace 'LegacyMarkerAbsent' with 'CurrentMarker'.", "needs_clarification", []),
    ("ambiguous instruction", "Change the status to Approved.", "needs_clarification", []),
]
PARAGRAPHS = ["Project status: Draft", "Milestones", "Remove this note", "Status: Pending"]


def _create_docx(path: Path) -> None:
    document = Document()
    for paragraph in PARAGRAPHS:
        document.add_paragraph(paragraph)
    document.save(path)


def _validate(operations: Any) -> tuple[bool, str | None]:
    try:
        OPERATIONS.validate_python(operations)
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class CapturingCloudProvider:
    """Use a real cloud model while retaining its raw structured response."""

    def __init__(self, model: Any, structured_output_options: dict[str, Any]):
        self.model = model
        self.structured_output_options = structured_output_options
        self.raw_responses: list[Any] = []

    def generate(self, messages: list[BaseMessage], schema: type[BaseModel]) -> Any:
        try:
            options = {**self.structured_output_options, "include_raw": True}
            response = self.model.with_structured_output(schema, **options).invoke(messages)
        except Exception as exc:
            kind = "timed out" if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() else "connection or response error"
            raise PlanningProviderError(f"Structured model request failed ({kind}).") from None
        self.raw_responses.append(response)
        if response.get("parsing_error"):
            return response.get("parsed")
        return response.get("parsed")


def _expected_normalized(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = OPERATIONS.validate_python(operations)
    return [item.model_dump(mode="json") for item in validated]


def run() -> list[dict[str, Any]]:
    load_dotenv(override=False)
    if not os.getenv("GROQ_API_KEY"):
        raise SystemExit("GROQ_API_KEY is not set. Add it to the local .env or shell; its value is never printed.")
    provider_name = "groq"
    os.environ["WORDPILOT_LLM_PROVIDER"] = provider_name
    os.environ["GROQ_MODEL"] = os.getenv("WORDPILOT_GROQ_BENCHMARK_MODEL", "openai/gpt-oss-120b")
    os.environ["WORDPILOT_LLM_TIMEOUT_SECONDS"] = os.getenv("WORDPILOT_BENCHMARK_TIMEOUT_SECONDS", "120")
    configured_provider = create_planning_provider()
    provider = CapturingCloudProvider(
        configured_provider._model,
        configured_provider._structured_output_options,
    )
    results = []
    with tempfile.TemporaryDirectory(prefix="wordpilot-groq-integration-") as temp_dir:
        root = Path(temp_dir)
        for index, (name, instruction, expected_status, expected_operations) in enumerate(CASES, start=1):
            source = root / f"synthetic-{index}.docx"
            _create_docx(source)
            docx_before = set(root.glob("*.docx"))
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            started = time.perf_counter()
            original_execute = orchestration.execute_approved_proposal
            execution_calls = 0

            def blocked_execution(*args: Any, **kwargs: Any) -> Any:
                nonlocal execution_calls
                execution_calls += 1
                raise AssertionError(f"{provider_name} benchmark must not execute proposals.")

            with patch.object(orchestration, "execute_approved_proposal", blocked_execution):
                proposal = plan_document(source, instruction, provider=provider)
            orchestration.execute_approved_proposal = original_execute
            elapsed = time.perf_counter() - started
            operations = proposal.get("proposed_operations", [])
            valid, validation_error = _validate(operations)
            expected = _expected_normalized(expected_operations)
            actual = _expected_normalized(operations) if valid else []
            expected_types = [operation["operation_type"] for operation in expected]
            actual_types = [operation.get("operation_type") for operation in operations if isinstance(operation, dict)]
            actual_status = proposal.get("planning_status")
            type_correct = actual_types == expected_types
            target_correct = actual == expected
            clarification_correct = (
                actual_status == "needs_clarification" and not operations
                if expected_status == "needs_clarification"
                else actual_status != "needs_clarification"
            )
            after = hashlib.sha256(source.read_bytes()).hexdigest()
            outputs = set(root.glob("*.docx")) - docx_before
            raw_response = provider.raw_responses[-1] if provider.raw_responses else None
            result = {
                "case": name,
                "expected_status": expected_status,
                "latency_seconds": round(elapsed, 3),
                "planner_status": actual_status,
                "operations": operations,
                "operation_types_correct": type_correct,
                "exact_targets_and_values_correct": target_correct,
                "clarification_correct": clarification_correct,
                "fabricated_or_wrong_operations": actual_status == "awaiting_approval" and not target_correct,
                "operations_pydantic_valid": valid,
                "structured_response_parsed": bool(
                    raw_response and raw_response.get("parsed") is not None and not raw_response.get("parsing_error")
                ),
                "operation_validation_error": validation_error,
                "clarification_requested": proposal.get("planning_status") == "needs_clarification",
                "clarification": proposal.get("clarification_request"),
                "errors": proposal.get("errors", []),
                "source_sha256_unchanged": before == after,
                "output_docx_created": bool(outputs),
                "execution_called": execution_calls != 0,
            }
            if not (type_correct and target_correct and clarification_correct and valid):
                raw_message = raw_response.get("raw") if isinstance(raw_response, dict) else None
                result["raw_response"] = getattr(raw_message, "content", None)
                parsed = raw_response.get("parsed") if isinstance(raw_response, dict) else None
                result["parsed_response"] = parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not result["source_sha256_unchanged"] or result["output_docx_created"]:
                raise RuntimeError("Planning safety check failed for a synthetic DOCX.")
    mean_latency = sum(item["latency_seconds"] for item in results) / len(results)
    print(json.dumps({
        "summary": {"provider": provider_name, "model": os.environ["GROQ_MODEL"],
                    "average_latency_seconds": round(mean_latency, 3), "cases": len(results),
                    "correct_planning_cases": sum(
                        item["planner_status"] == item["expected_status"] and
                        item["exact_targets_and_values_correct"] and item["clarification_correct"] and
                        item["operations_pydantic_valid"] for item in results
                    ),
                    "valid_structured_responses": sum(item["structured_response_parsed"] for item in results),
                    "correct_clarification_decisions": sum(
                        item["clarification_correct"] for item in results
                        if item["expected_status"] == "needs_clarification"
                    ),
                    "source_documents_unchanged": all(item["source_sha256_unchanged"] for item in results),
                    "execution_called": any(item["execution_called"] for item in results)},
    }, ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    run()
