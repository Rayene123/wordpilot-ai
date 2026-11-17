# DevFlow instructions

These instructions supplement the repository and backend instructions for code under `backend/dev_workflow/`.

## Separation and current scope

- DevFlow is the optional software-development orchestrator. It is separate from WordPilot's DOCX editing product.
- Never import `backend/agent/provider.py` or use the document product's Groq/Ollama providers for DevFlow. Codex CLI is DevFlow's AI interface.
- The current code includes Pydantic schemas, serializable workflow state, a read-only Codex CLI analysis adapter, and a LangGraph analysis/validation/approval flow. Run its focused tests before making claims about runtime behavior. An approval UI, worktree manager, implementation stage, verification pipeline, review stage, and repair loop are not part of the current workflow.
- Codex CLI integration does not control the existing Codex GUI or IDE conversation.

## Codex CLI and approval boundary

- Invoke the CLI through a fixed host-side adapter with argument arrays, explicit working directories, timeouts, bounded output, and sanitized errors. Never use `shell=True` or expose an arbitrary shell/filesystem tool through the orchestrator.
- Analysis must use the read-only sandbox. Never use dangerous sandbox or approval bypass flags.
- Any future code-writing invocation must be downstream of an explicit human approval bound to the exact validated proposal. Missing, stale, or mismatched approval must stop the workflow.
- Before any future implementation stage, isolate work in a dedicated Git worktree. Keep the proposal's approved file scope and check the resulting diff against it; instructions to Codex alone are not enforcement.
- Keep human approval separate from model analysis. Codex must not issue its own approval.

## Verification, review, and evidence

- Any future implementation stage must run only host-defined project checks with bounded output and recorded command, exit status, and result. Do not let model output select arbitrary verification commands.
- Review the actual diff and check evidence in a separate review stage. Treat model review as advisory and record unresolved findings.
- Permit at most one bounded correction attempt in the planned first version; revalidate scope and rerun checks and review after correction.
- Build final reports from recorded evidence. Distinguish passed, failed, skipped, and not-run checks; do not claim unexecuted checks passed.
- Keep state serializable and avoid adding checkpoint stores, API services, or other architecture until explicitly required.
