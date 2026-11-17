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
            # Provider exception text can contain request fragments. Keep the
            # user-facing error useful without exposing prompts or document text.
            kind = "timed out" if isinstance(exc, (TimeoutError,)) or "timeout" in type(exc).__name__.lower() else "connection or response error"
            raise PlanningProviderError(f"Structured model request failed ({kind}).") from None


def create_planning_provider() -> LangChainPlanningProvider:
    """Create the explicitly selected LangChain planning model.

    Ollama is the privacy-preserving default. OpenAI is used only when selected
    explicitly through WORDPILOT_LLM_PROVIDER=openai.
    """
    provider_name = os.getenv("WORDPILOT_LLM_PROVIDER", "ollama").strip().lower()
    try:
        timeout = float(os.getenv("WORDPILOT_LLM_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if provider_name == "ollama":
            from langchain_ollama import ChatOllama

            model_name = os.getenv("WORDPILOT_LLM_MODEL", "qwen2.5:7b")
            return LangChainPlanningProvider(ChatOllama(
                model=model_name,
                base_url=os.getenv("WORDPILOT_OLLAMA_BASE_URL", "http://localhost:11434"),
                temperature=0,
                validate_model_on_init=False,
                client_kwargs={"timeout": timeout},
            ))
        if provider_name == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise PlanningProviderError("OPENAI_API_KEY is required when WORDPILOT_LLM_PROVIDER=openai.")
            from langchain_openai import ChatOpenAI

            model_name = os.getenv("WORDPILOT_LLM_MODEL", "gpt-4o-mini")
            return LangChainPlanningProvider(ChatOpenAI(
                model=model_name,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            ))
        raise PlanningProviderError(
            "Unknown WORDPILOT_LLM_PROVIDER. Choose 'ollama' or 'openai'."
        )
    except PlanningProviderError:
        raise
    except Exception as exc:
        raise PlanningProviderError("Could not configure the selected planning model.") from None
