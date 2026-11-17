"""Small LangChain provider adapter for structured planning output."""

from __future__ import annotations

import os
from typing import Any, Protocol

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


class StructuredModel(Protocol):
    def with_structured_output(self, schema: type[BaseModel]) -> Any:
        """Return a model wrapper that asks the provider for this Pydantic shape."""
        ...


class PlanningProviderError(RuntimeError):
    """Provider configuration, transport, or response failure."""


class LangChainPlanningProvider:
    """Invoke a LangChain chat model with a Pydantic structured-output schema."""

    def __init__(self, model: StructuredModel):
        """Keep the configured chat model; no network request happens here."""
        self._model = model

    def generate(self, messages: list[BaseMessage], schema: type[BaseModel]) -> Any:
        """Send messages through structured output and normalize provider errors.

        The result is returned as supplied by LangChain. The planner performs
        local Pydantic validation before treating it as an operation proposal.
        """
        try:
            return self._model.with_structured_output(schema).invoke(messages)
        except Exception as exc:
            raise PlanningProviderError(f"Structured model request failed: {exc}") from exc


def create_planning_provider() -> LangChainPlanningProvider:
    """Create the configured OpenAI-compatible LangChain model.

    Configuration: OPENAI_API_KEY, WORDPILOT_LLM_MODEL, and
    WORDPILOT_LLM_TIMEOUT_SECONDS. The API key is read only from the environment.
    A positive request timeout is required, and LangChain retries are disabled so
    a failed planning call returns control to the caller immediately.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise PlanningProviderError("OPENAI_API_KEY is required to create the planning provider.")
    model_name = os.getenv("WORDPILOT_LLM_MODEL", "gpt-4o-mini")
    try:
        timeout = float(os.getenv("WORDPILOT_LLM_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        from langchain_openai import ChatOpenAI

        return LangChainPlanningProvider(ChatOpenAI(
            model=model_name,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        ))
    except Exception as exc:
        raise PlanningProviderError(f"Could not configure planning model: {exc}") from exc
