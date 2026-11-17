"""Small LangChain provider adapter for structured planning output."""

from __future__ import annotations

import os
from typing import Any, Protocol

from langchain_core.messages import BaseMessage
from dotenv import load_dotenv
from pydantic import BaseModel


class StructuredModel(Protocol):
    def with_structured_output(self, schema: type[BaseModel], **options: Any) -> Any:
        """Return a model wrapper that asks the provider for this Pydantic shape."""
        ...


class PlanningProviderError(RuntimeError):
    """Provider configuration, transport, or response failure."""


def get_planning_configuration() -> tuple[str, str]:
    """Return the selected provider and its provider-specific model setting."""
    load_dotenv(override=False)
    provider_name = os.getenv("WORDPILOT_LLM_PROVIDER", "ollama").strip().lower()
    if provider_name == "groq":
        return provider_name, os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    if provider_name == "ollama":
        return provider_name, os.getenv("WORDPILOT_LLM_MODEL", "qwen2.5:7b")
    return provider_name, "unknown"


class LangChainPlanningProvider:
    """Invoke a LangChain chat model with a Pydantic structured-output schema."""

    def __init__(self, model: StructuredModel, *, structured_output_options: dict[str, Any] | None = None):
        """Keep the configured chat model; no network request happens here."""
        self._model = model
        self._structured_output_options = structured_output_options or {}

    def generate(self, messages: list[BaseMessage], schema: type[BaseModel]) -> Any:
        """Send messages through structured output and normalize provider errors.

        The result is returned as supplied by LangChain. The planner performs
        local Pydantic validation before treating it as an operation proposal.
        """
        try:
            return self._model.with_structured_output(
                schema, **self._structured_output_options,
            ).invoke(messages)
        except Exception as exc:
            # Provider exception text can contain request fragments. Keep the
            # user-facing error useful without exposing prompts or document text.
            kind = "timed out" if isinstance(exc, (TimeoutError,)) or "timeout" in type(exc).__name__.lower() else "connection or response error"
            raise PlanningProviderError(f"Structured model request failed ({kind}).") from None


def create_planning_provider() -> LangChainPlanningProvider:
    """Create the explicitly selected LangChain planning model.

    Ollama is the local default. Groq is used only when explicitly selected
    through WORDPILOT_LLM_PROVIDER=groq.
    """
    # Keep existing process environment values authoritative while supporting
    # project-local .env configuration for development and benchmark runs.
    provider_name, model_name = get_planning_configuration()
    try:
        timeout = float(os.getenv("WORDPILOT_LLM_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if provider_name == "ollama":
            from langchain_ollama import ChatOllama

            return LangChainPlanningProvider(ChatOllama(
                model=model_name,
                base_url=os.getenv("WORDPILOT_OLLAMA_BASE_URL", "http://localhost:11434"),
                temperature=0,
                validate_model_on_init=False,
                client_kwargs={"timeout": timeout},
            ))
        if provider_name == "groq":
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise PlanningProviderError("GROQ_API_KEY is required when WORDPILOT_LLM_PROVIDER=groq.")
            from langchain_groq import ChatGroq

            return LangChainPlanningProvider(
                ChatGroq(
                    model=model_name,
                    api_key=api_key,
                    temperature=0,
                    timeout=timeout,
                    max_retries=0,
                ),
                structured_output_options={"method": "json_schema", "strict": False},
            )
        raise PlanningProviderError(
            "Unknown WORDPILOT_LLM_PROVIDER. Choose 'ollama' or 'groq'."
        )
    except PlanningProviderError:
        raise
    except Exception as exc:
        raise PlanningProviderError("Could not configure the selected planning model.") from None
