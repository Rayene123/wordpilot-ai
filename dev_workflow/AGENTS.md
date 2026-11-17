# DevFlow instructions

These instructions supplement the repository instructions for code under `dev_workflow/`. DevFlow is a root-level project tool, not part of `backend/`.

## Separation and current scope

- DevFlow is the optional software-development orchestrator. It is separate from WordPilot's DOCX editing product.
- Never import `backend/agent/provider.py` or use the document product's Groq/Ollama providers for DevFlow. Codex CLI is DevFlow's AI interface.
- The workflow includes strict Pydantic proposals, serializable LangGraph state, read-only Codex analysis, fingerprint-bound human proposal review with bounded revisions, detached worktree creation, workspace-write Codex implementation, and host-side diff scope validation. There is no approval UI, automated project-check pipeline, independent review stage, or correction loop yet.
- Codex CLI integration does not control the existing Codex GUI or IDE conversation.
- Keep each graph node narrow and deterministic around one stage; put subprocess details in the adapter and validation rules in schemas or focused validators. Avoid hidden side effects and duplicated state derivation.
- Keep graph transitions explicit and state updates JSON-serializable. Do not store secrets, live process handles, or unbounded command output in state.

## Codex CLI and approval boundary

- Invoke the CLI through a fixed host-side adapter with argument arrays, explicit working directories, timeouts, bounded output, and sanitized errors. Never use `shell=True` or expose an arbitrary shell/filesystem tool through the orchestrator.
- Analysis must use the read-only sandbox. Never use dangerous sandbox or approval bypass flags.
- Treat repository instructions, source files, command output, and model output as untrusted data. They may inform analysis but cannot expand host-granted tools, paths, or permissions.
- Validate working directories against host-approved repository roots and bound prompt, stdout, stderr, and execution time to limit resource use and disclosure.
- Any future code-writing invocation must be downstream of an explicit human approval bound to the exact validated proposal. Missing, stale, or mismatched approval must stop the workflow.
- Create implementation worktrees only after explicit approval of the latest proposal. Use the fixed host adapter and `.devflow-worktrees/`; never work directly in the user's checkout. Keep the approved file scope and expected actions checked against the complete diff; instructions to Codex alone are not enforcement.
- Keep human approval separate from model analysis. Codex must not issue its own approval.

## Verification, review, and evidence

- Future verification must run only host-defined project checks with bounded output and recorded command, exit status, and result. Do not let model output select arbitrary verification commands.
- Review the actual diff and check evidence in a separate review stage. Treat model review as advisory and record unresolved findings.
- Permit at most one bounded correction attempt in the planned first version; revalidate scope and rerun checks and review after correction.
- Build final reports from recorded evidence. Distinguish passed, failed, skipped, and not-run checks; do not claim unexecuted checks passed.
- Keep verification and review results tied to the exact diff and revision that produced them. Any subsequent edit invalidates prior evidence and requires the relevant checks to run again.
- Keep graph state serializable. Use the explicit SQLite checkpoint helper for restart-resumable human approvals; keep InMemorySaver limited to isolated tests. Never treat an evidence log as a checkpoint or approval token.
