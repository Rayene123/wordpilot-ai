# Backend instructions

These instructions supplement the repository-level `AGENTS.md` for files under `backend/`.

## Python and API conventions

- Target Python 3.11+ and the versions declared in `requirements.txt` (Pydantic 2, FastAPI, and LangGraph 1.x).
- Use type hints, small functions, and Pydantic models for structured external or workflow data. Reject unexpected fields for security-sensitive payloads where compatible with existing interfaces.
- Keep modules cohesive and dependencies one-way where practical: routes coordinate, domain modules implement behavior, and shared helpers stay small and domain-neutral. Avoid duplicate validation or parallel implementations of an existing operation.
- Keep FastAPI route handlers thin. Put document and workflow behavior in the existing backend modules and preserve response compatibility unless the task explicitly changes it.
- Keep filesystem paths and output directories selected and validated by host code.
- Handle expected failures explicitly at API boundaries; do not return stack traces, provider credentials, local paths, or document text in client-facing errors or logs.
- Bound external work with timeouts and output limits. Avoid blocking the async event loop with synchronous network or long-running process work.

## Document editing and integrity

- Use the existing `backend/docx_editor/` operations and inspection APIs. Do not introduce a second DOCX editing path without a specific requirement.
- Never overwrite the source DOCX in an approved API workflow. Keep generated output separate and verify it with `backend/docx_editor/integrity.py` where applicable.
- Preserve paragraph/run formatting, tables, images, and package components outside the intended edit. Be precise about verifier coverage: a passing check does not certify unsupported OOXML features.
- Respect documented limits for tracked changes, headers/footers, hyperlinks, content controls, fields, shapes, and other unsupported document structures. Do not claim unsupported content is preserved or edited safely.

## Agents, providers, and workflow state

- Keep document-editing planning and execution in `backend/agent/`; keep DOCX operations in `backend/docx_editor/`.
- Groq and Ollama are providers for WordPilot's document-editing product and are configured through `backend/agent/provider.py`. Do not silently change provider selection or add automatic fallback.
- Keep workflow state serializable. Do not put open DOCX objects, subprocesses, callbacks, or file handles in state.
- Treat provider responses and model-generated plans as untrusted. Validate them against existing schemas before they can affect operations, state transitions, or user-visible claims.
- Keep approval tied to the exact operation or proposal being approved; reject missing or stale approval rather than inferring consent.
- DevFlow is maintained in the repository-root `dev_workflow/` package and is outside the backend. Follow `dev_workflow/AGENTS.md` when changing it; keep it separate from the document-editing provider and approval flow.

## Backend verification

- Run focused tests with `python -m pytest tests/<relevant_test_file>.py -q`; run the full backend suite with `python -m pytest -q` when appropriate.
- API tests use FastAPI's test client and synthetic DOCX fixtures in `tests/conftest.py`. Planner/provider tests use fakes and should not require live model credentials.
- Report any unavailable Python runtime, external service, or check explicitly. Do not infer a pass from a mocked test or static inspection.
