from __future__ import annotations

from langchain_core.messages import HumanMessage
import pytest
from pydantic import BaseModel

from backend.agent import OperationPlanner, PlanningError, PlanningResult
from backend.agent.provider import LangChainPlanningProvider, PlanningProviderError


SUMMARY = {
    "paragraphs": [
        {"text": "Project status: Draft", "style": "Normal"},
        {"text": "Milestones", "style": "Heading 1"},
        {"text": "Remove this note", "style": "Normal"},
    ],
    "tables": [], "paragraph_count": 3, "table_count": 0, "inline_image_count": 0,
}


class MockProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.messages = None
        self.schema = None

    def generate(self, messages, schema):
        self.messages, self.schema = messages, schema
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(("instruction", "response", "expected"), [
    (
        "Change Draft to Final", {"status": "proposed", "message": "Ready.", "operations": [
            {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
        ]}, "replace",
    ),
    (
        "Add a paragraph after Milestones", {"status": "proposed", "message": "Ready.", "operations": [
            {"operation_type": "insert", "anchor_paragraph_text": "Milestones", "inserted_text": "New detail",
             "position": "after", "paragraph_style": None, "location": None},
        ]}, "insert",
    ),
    (
        "Remove the note", {"status": "proposed", "message": "Ready.", "operations": [
            {"operation_type": "remove", "target_text": "Remove this note", "location": None},
        ]}, "remove",
    ),
    (
        "Update the status and remove the note", {"status": "proposed", "message": "Ready.", "operations": [
            {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
            {"operation_type": "remove", "target_text": "Remove this note", "location": None},
        ]}, "multiple",
    ),
])
def test_planner_returns_valid_operations(instruction, response, expected):
    provider = MockProvider(response)
    result = OperationPlanner(provider).plan(instruction, SUMMARY)
    assert result.status == "proposed"
    if expected == "multiple":
        assert len(result.operations) == 2
    else:
        assert result.operations[0]["operation_type"] == expected
    assert provider.schema is PlanningResult
    assert isinstance(provider.messages[0].content, str)
    assert "untrusted" in provider.messages[0].content.lower()


@pytest.mark.parametrize("response", [
    {"status": "clarification_required", "message": "Which Draft?", "operations": []},
    {"status": "unsupported", "message": "Formatting is unsupported.", "operations": []},
])
def test_planner_reports_clarification_or_unsupported(response):
    result = OperationPlanner(MockProvider(response)).plan("Make a change", SUMMARY)
    assert result.status in {"clarification_required", "unsupported"}
    assert result.operations == []


def test_repeated_target_is_converted_to_host_clarification():
    duplicated = {**SUMMARY, "paragraphs": SUMMARY["paragraphs"] + [
        {"text": "Project status: Draft", "style": "Normal"},
    ]}
    provider = MockProvider({"status": "proposed", "message": "Ready.", "operations": [
        {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
    ]})
    result = OperationPlanner(provider).plan("Change Draft to Final", duplicated)
    assert result.status == "clarification_required"
    assert result.operations == []
    assert "more than once" in result.message


@pytest.mark.parametrize("response", [
    {"status": "proposed", "message": "bad", "operations": [{"operation_type": "run_python", "code": "x"}]},
    {"status": "proposed", "message": "bad", "operations": [{"operation_type": "replace", "target_text": "Draft"}]},
    {"status": "finished", "message": "bad", "operations": []},
    {"status": "proposed", "message": "bad", "operations": []},
    "not structured output",
])
def test_invalid_structured_or_malformed_response_is_rejected(response):
    with pytest.raises(PlanningError, match="invalid planning output"):
        OperationPlanner(MockProvider(response)).plan("Edit", SUMMARY)


@pytest.mark.parametrize("error", [
    PlanningProviderError("timed out"), RuntimeError("network unavailable"),
])
def test_provider_failure_is_reported(error):
    with pytest.raises(PlanningError, match="failed|timed out"):
        OperationPlanner(MockProvider(error=error)).plan("Edit", SUMMARY)


def test_langchain_provider_uses_structured_output_and_wraps_failures():
    class Chat:
        def with_structured_output(self, schema):
            assert schema is PlanningResult
            return self

        def invoke(self, messages):
            assert isinstance(messages[0], HumanMessage)
            return {"status": "unsupported", "message": "No.", "operations": []}

    assert LangChainPlanningProvider(Chat()).generate([HumanMessage(content="x")], PlanningResult)["status"] == "unsupported"

    class Broken:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            raise TimeoutError("request timed out")

    with pytest.raises(PlanningProviderError, match="timed out"):
        LangChainPlanningProvider(Broken()).generate([HumanMessage(content="x")], PlanningResult)
