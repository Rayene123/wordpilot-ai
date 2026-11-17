"""Run real local Ollama integration cases for WordPilot's planner.

This script is intentionally outside the ordinary pytest collection. It makes
real requests to the configured local Ollama server and never executes edits.
Run with: python scripts/ollama_planner_integration.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from docx import Document
from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Pin this integration run to the local provider. There is deliberately no
# provider fallback path in the planner adapter.
os.environ["WORDPILOT_LLM_PROVIDER"] = "ollama"
os.environ["WORDPILOT_LLM_MODEL"] = "qwen2.5:7b"
os.environ["WORDPILOT_OLLAMA_BASE_URL"] = "http://localhost:11434"
os.environ.setdefault("WORDPILOT_LLM_TIMEOUT_SECONDS", "120")

from backend.agent.orchestration import plan_document
from backend.agent.schemas import DocumentOperation

_OPERATIONS = TypeAdapter(list[DocumentOperation])

_CASES = [
    ("simple replacement", "Replace 'Draft' with 'Final'.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
    ("paragraph insertion", "Insert the paragraph 'Launch review scheduled.' after 'Milestones'.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
    ("paragraph removal", "Remove the paragraph 'Remove this note'.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
    ("multiple instructions", "Replace 'Draft' with 'Final' and remove the paragraph 'Remove this note'.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
    ("missing target", "Replace 'LegacyMarkerAbsent' with 'CurrentMarker'.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
    ("ambiguous instruction", "Change the status to Approved.", [
        "Project status: Draft", "Milestones", "Remove this note", "Status: Pending",
    ]),
]


def _make_synthetic_docx(path: Path, paragraphs: list[str]) -> None:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(path)


def _validate_operations(operations: Any) -> tuple[bool, str | None]:
    try:
        _OPERATIONS.validate_python(operations)
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED_SYNTHETIC_TEXT]")
        return value
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def run(cases: list[tuple[str, str, list[str]]] | None = None) -> list[dict[str, Any]]:
    if os.environ.get("WORDPILOT_LLM_PROVIDER") != "ollama":
        raise RuntimeError("Integration run requires the Ollama provider.")
    if os.environ.get("WORDPILOT_LLM_MODEL") != "qwen2.5:7b":
        raise RuntimeError("Integration run requires qwen2.5:7b.")

    results = []
    with tempfile.TemporaryDirectory(prefix="wordpilot-ollama-integration-") as temp_dir:
        root = Path(temp_dir)
        for index, (name, instruction, paragraphs) in enumerate(cases or _CASES, start=1):
            source = root / f"synthetic-{index}.docx"
            _make_synthetic_docx(source, paragraphs)
            original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            started = time.perf_counter()
            proposal = plan_document(source, instruction)
            latency = time.perf_counter() - started
            operations = proposal.get("proposed_operations", [])
            schema_valid, validation_error = _validate_operations(operations)
            after_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            result = {
                "case": name,
                "latency_seconds": round(latency, 3),
                "planner_status": proposal.get("planning_status"),
                "planner_message": proposal.get("clarification_request") or proposal.get("final_response"),
                "proposed_operations": operations,
                "pydantic_validation_passed": schema_valid,
                "pydantic_validation_error": validation_error,
                "clarification_requested": proposal.get("planning_status") == "needs_clarification",
                "clarification_request": proposal.get("clarification_request"),
                "original_document_unchanged": original_digest == after_digest,
                "errors": proposal.get("errors", []),
            }
            results.append(result)
            tokens = [token for text in paragraphs + [instruction]
                      for token in re.findall(r"[\w'-]+", text) if len(token) >= 3]
            secrets = list(dict.fromkeys(paragraphs + [instruction] + tokens))
            print(json.dumps(_redact(result, secrets), ensure_ascii=False))
            if not result["original_document_unchanged"]:
                raise RuntimeError(f"Planner modified synthetic source document for case: {name}")
    return results


if __name__ == "__main__":
    run()
