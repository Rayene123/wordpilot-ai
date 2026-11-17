from __future__ import annotations

import sys
import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
import pytest
from pydantic import BaseModel

from backend.agent import OperationPlanner, PlanningError, PlanningResult
from backend.agent.planner import PlanningContextTooLarge
from backend.agent.provider import (
    LangChainPlanningProvider, PlanningProviderError, create_planning_provider,
)


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


def test_planner_sends_compact_ordered_context_without_duplicate_run_or_table_text():
    context = {
        "blocks": [
            {"p": "Before"},
            {"t": [[[{"p": "Cell target"}]]]},
            {"p": "After"},
        ],
    }
    provider = MockProvider({"status": "unsupported", "message": "No edit.", "operations": []})
    result = OperationPlanner(provider).plan("Find the cell target", SUMMARY, planner_context=context)
    request = provider.messages[1].content
    projected_json = request.split("Untrusted document inspection summary (JSON):\n", 1)[1]
    projected = json.loads(projected_json)

    assert result.status == "unsupported"
    assert projected == context
    assert request.count("Cell target") == 1
    assert projected["blocks"] == [{"p": "Before"}, {"t": [[[{"p": "Cell target"}]]]}, {"p": "After"}]
    assert projected["blocks"][1]["t"][0][0][0]["p"] == "Cell target"


def test_oversized_planner_context_is_rejected_before_provider_call():
    provider = MockProvider({"status": "unsupported", "message": "No edit.", "operations": []})
    oversized = {"blocks": [{"type": "paragraph", "order": 0, "text": "x" * 20_000}]}

    with pytest.raises(PlanningContextTooLarge, match="too large to plan safely"):
        OperationPlanner(provider).plan("Edit", SUMMARY, planner_context=oversized)
    assert provider.messages is None


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


@pytest.mark.parametrize(("provider", "module", "class_name", "env", "expected"), [
    ("ollama", "langchain_ollama", "ChatOllama", {}, {
        "model": "qwen2.5:7b", "base_url": "http://localhost:11434",
        "temperature": 0, "validate_model_on_init": False,
        "client_kwargs": {"timeout": 30.0},
    }),
    ("groq", "langchain_groq", "ChatGroq", {"GROQ_API_KEY": "test-groq-key", "GROQ_MODEL": "openai/gpt-oss-120b"}, {
        "model": "openai/gpt-oss-120b", "api_key": "test-groq-key", "temperature": 0,
        "timeout": 30.0, "max_retries": 0,
    }),
])
def test_provider_selection_and_model_configuration(monkeypatch, provider, module, class_name, env, expected):
    import backend.agent.provider as provider_module

    monkeypatch.setattr(provider_module, "load_dotenv", lambda **kwargs: False)
    calls = []

    class FakeChat:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "modules", {
        **sys.modules, module: SimpleNamespace(**{class_name: FakeChat}),
    })
    for key in ("WORDPILOT_LLM_PROVIDER", "WORDPILOT_LLM_MODEL", "WORDPILOT_OLLAMA_BASE_URL",
                "WORDPILOT_LLM_TIMEOUT_SECONDS", "GROQ_API_KEY", "GROQ_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", provider)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = create_planning_provider()
    assert isinstance(result, LangChainPlanningProvider)
    assert calls == [expected]


def test_ollama_explicit_model_and_base_url(monkeypatch):
    calls = []
    class FakeOllama:
        def __init__(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setattr(sys, "modules", {
        **sys.modules, "langchain_ollama": SimpleNamespace(ChatOllama=FakeOllama),
    })
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("WORDPILOT_LLM_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setenv("WORDPILOT_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    assert create_planning_provider()
    assert calls[0]["model"] == "qwen2.5:7b-instruct"
    assert calls[0]["base_url"] == "http://127.0.0.1:11434"


def test_provider_model_settings_do_not_leak(monkeypatch):
    import backend.agent.provider as provider_module

    monkeypatch.setattr(provider_module, "load_dotenv", lambda **kwargs: False)
    monkeypatch.setenv("WORDPILOT_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", "groq")
    assert provider_module.get_planning_configuration() == ("groq", "openai/gpt-oss-120b")
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", "ollama")
    assert provider_module.get_planning_configuration() == ("ollama", "qwen2.5:7b")


@pytest.mark.parametrize(("provider", "key", "value", "message"), [
    ("unknown", None, None, "Unknown WORDPILOT_LLM_PROVIDER"),
    ("groq", "GROQ_API_KEY", None, "GROQ_API_KEY is required"),
    ("remote", None, None, "Unknown WORDPILOT_LLM_PROVIDER"),
])
def test_invalid_provider_configuration_fails_without_fallback(monkeypatch, provider, key, value, message):
    import backend.agent.provider as provider_module

    monkeypatch.setattr(provider_module, "load_dotenv", lambda **kwargs: False)
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", provider)
    if key:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(PlanningProviderError, match=message):
        create_planning_provider()


def test_ollama_connection_error_does_not_fall_back_to_groq(monkeypatch):
    class BrokenOllama:
        def with_structured_output(self, schema):
            return self
        def invoke(self, messages):
            raise ConnectionError("private document contents must not leak")

    provider = LangChainPlanningProvider(BrokenOllama())
    with pytest.raises(PlanningProviderError, match="connection or response error") as error:
        provider.generate([HumanMessage(content="private document contents")], PlanningResult)
    assert "private document contents" not in str(error.value)


def test_ollama_timeout_is_normalized_without_fallback():
    class BrokenOllama:
        def with_structured_output(self, schema):
            return self
        def invoke(self, messages):
            raise TimeoutError("private request data")

    with pytest.raises(PlanningProviderError, match="timed out"):
        LangChainPlanningProvider(BrokenOllama()).generate([HumanMessage(content="x")], PlanningResult)


def test_groq_uses_json_schema_output_and_rejects_invalid_operation(monkeypatch):
    import backend.agent.provider as provider_module

    monkeypatch.setattr(provider_module, "load_dotenv", lambda **kwargs: False)
    captured = {}

    class FakeGroq:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

        def with_structured_output(self, schema, **options):
            captured["schema"] = schema
            captured["options"] = options
            return self

        def invoke(self, messages):
            return {"status": "proposed", "message": "Invalid.", "operations": [
                {"operation_type": "run_python", "code": "not allowed"},
            ]}

    monkeypatch.setattr(sys, "modules", {
        **sys.modules, "langchain_groq": SimpleNamespace(ChatGroq=FakeGroq),
    })
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("WORDPILOT_LLM_MODEL", raising=False)

    with pytest.raises(PlanningError, match="invalid planning output"):
        OperationPlanner(create_planning_provider()).plan("Do something", SUMMARY)

    assert captured["schema"] is PlanningResult
    assert captured["options"] == {"method": "json_schema", "strict": False}
    assert captured["config"]["model"] == "openai/gpt-oss-120b"
