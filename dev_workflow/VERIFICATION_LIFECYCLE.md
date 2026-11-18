# DevFlow Verification Lifecycle

## 1. Purpose

DevFlow is optional development-time tooling that orchestrates Codex CLI for software development tasks. It coordinates task analysis, human approval, isolated implementation, diff validation, verification, and reporting. It is not part of WordPilot's delivered runtime and does not process WordPilot end-user instructions.

It is separate from the WordPilot application runtime and does not use the application's AI provider.

## 2. Lifecycle Overview

The implemented workflow follows these stages:

1. Task analysis
2. Proposal validation
3. Human approval
4. Implementation in an isolated Git worktree
5. Diff collection and scope validation
6. Host-configured verification
7. Final evidence-based reporting

The workflow is checkpointed and can pause for human approval.

## 3. Host Setup and CLI

The repository declares Python `>=3.12,<3.13` in `pyproject.toml`; install the Python dependencies from the repository root with `python -m pip install -r requirements.txt`. DevFlow also requires Git on `PATH` and a Codex CLI installed and authenticated independently. `CODEX_CLI_PATH` may point to the executable; otherwise DevFlow resolves `codex` through `PATH`. DevFlow does not install Codex or provide credentials.

On Windows, launch from the repository root with `py -3.12 -m dev_workflow` after dependencies are installed. Before implementation, set `DEVFLOW_WINDOWS_SANDBOX` to a backend accepted by the implementation: `mxc`, `elevated`, or `unelevated`. Codex and managed policy determine whether that backend is available. The previous live Windows execution used CMD, Python 3.12.10, and `elevated`; it does not verify the other values or every Windows configuration. The project has not established Linux or macOS live execution through that evidence.

The CLI menu is:

| Option | Action |
|---|---|
| 1 | Check environment, including the mandatory live Codex preflight. |
| 2 | Start a new task; blocked until option 1 passes in this CLI session. Enter the task description, then type `END` alone on a new line to submit it. |
| 3 | Review a pending approval. |
| 4 | Resume an existing workflow. |
| 5 | Inspect workflow status and report. |
| 6 | Run available verification for a saved validated worktree diff. |
| 7 | Exit. |

The live preflight checks Codex authentication, read-only execution, and workspace-write execution in a disposable worktree, including host marker/diff validation and cleanup. The CLI also reports Python, Git, required dependency, and project-config availability. Resolve failures and pass option 1 before starting or continuing implementation. The exact previously validated Windows command sequence is in the [CTO demo guide](../docs/DEVFLOW_CTO_DEMO.md).

## 4. Task Analysis

The analysis stage sends the task description, repository path, and any revision feedback to the configured Codex CLI adapter.

The adapter is responsible for invoking Codex with controlled arguments, a configured working directory, execution timeouts, and bounded output.

Analysis uses the read-only sandbox. The returned proposal is validated against the strict `TaskProposal` schema before it can proceed.

Repository inspection guidance uses Python standard-library facilities such as `pathlib`, `os.walk`, and bounded file reads and searches. It does not depend on `rg`, Unix utilities, or PowerShell commands. Inspection excludes version-control metadata, DevFlow worktrees, virtual environments, caches, dependencies, and generated build output. Expected file actions are checked against the repository before a proposal proceeds.

A successful process exit alone is not sufficient evidence of successful analysis. Execution-policy or sandbox refusal evidence must cause analysis to fail closed, even if structured proposal output is returned.

The analysis stage does not authorize repository modifications.

## 5. Proposal Validation

The proposal is validated using strict schemas. Unexpected fields and invalid values are rejected.

Proposal paths are checked against repository-relative path rules. The workflow rejects unsafe paths, traversal, and symlink-based escapes.

Expected file actions are checked against the current repository state:
- `create`: the target must not already exist.
- `modify` or `delete`: the target must already exist.

The workflow calculates a proposal fingerprint using canonical proposal data and its revision number.

The fingerprint binds subsequent human decisions to the exact proposal revision.

## 6. Human Approval and Revisions

Implementation requires an explicit human approval decision.

The approval interrupt contains the run ID, proposal, proposal fingerprint, and revision information.

Supported decisions are:
- `approve`: authorize implementation of the exact validated proposal.
- `reject`: reject the proposal.
- `request_changes`: return the task to analysis with revision feedback.

Revision attempts are bounded by the workflow configuration. The default maximum is two revisions, and the configured maximum is limited to five.

A decision must match the current proposal fingerprint and revision. Stale or mismatched decisions are rejected.

The workflow does not implement an approval UI; approval is supplied through its host-facing API.

## 7. Checkpointing and Resume

DevFlow requires an explicit checkpointer.

The SQLite checkpoint helper:
- Requires a persistent database file path.
- Creates the parent directory when necessary.
- Checks database integrity.
- Uses LangGraph's JSON-plus serializer with restricted message-pack types and no pickle fallback.
- Raises an error for an unusable or corrupt database instead of silently replacing it with an in-memory saver.

To resume a pending approval, DevFlow verifies:
- The saved run ID.
- The pending approval stage and interrupt.
- The saved proposal and revision.
- The proposal fingerprint.
- The interrupt contents.
- The submitted decision.

Only a decision matching the saved proposal can resume the workflow.

## 8. Isolated Implementation

After approval, DevFlow creates a detached Git worktree under:

`.devflow-worktrees/<run_id>`

The worktree directory is host-selected. Existing targets and symlink-based directory escapes are rejected.

Before implementation and subsequent operations, the worktree is checked to ensure that it:
- Is inside the expected DevFlow worktree directory.
- Is a Git worktree root.
- Is registered with the original repository.

The implementation adapter receives the approved proposal, its fingerprint, and the isolated worktree path.

The user's original checkout is not the implementation workspace.

## 9. Diff Collection and Scope Validation

After implementation, DevFlow collects changes from the isolated worktree.

Diff collection accounts for:
- Tracked changes.
- Untracked files.
- Ignored untracked files.

Each changed file is checked against the approved file scope and expected action.

The workflow rejects:
- Changes outside the approved scope.
- Unexpected file actions.
- Missing expected changes.
- Unsafe paths and symlinks.
- Ignored untracked files.
- Unsupported change types.
- Diff evidence exceeding configured size limits.

The result is represented as a `DiffReport` with a status such as `valid`, `invalid`, or `empty`.

A valid implementation must produce a non-empty diff matching the approved proposal.

## 10. Verification

Verification is performed by host-configured checks, not commands selected by the model.

The public verification entry point selects from fixed host checks using only the changed paths in the host-validated `DiffReport`. Proposal text and model output cannot select or skip commands.

Each check has:
- A fixed check ID.
- A display name.
- A fixed argument list.
- A worktree-relative working directory.
- A timeout.

Processes are launched with `shell=False`. Output is bounded and sensitive values are redacted.

On Windows, the configured npm typecheck is resolved to a directly launchable Node.js/npm CLI process when the npm launcher is a `.cmd` file.

The host selects checks by changed paths:
- Python files require `python.tests`.
- Files under `frontend/` require `frontend.typecheck`.
- Changes in both areas require both checks.
- Documentation-only changes under `docs/` or documentation file types require no unrelated test suite.
- Other source or configuration changes retain the full configured suite.

The Python check runs `python -m pytest -q -p no:cacheprovider` with `PYTHONDONTWRITEBYTECODE=1`. This prevents pytest cache and Python bytecode artifacts from appearing as worktree changes during verification.

Verification records statuses including:
- `passed`
- `failed`
- `timed_out`
- `not_run`

When a check fails or times out, subsequent checks are marked `not_run`.

DevFlow captures the worktree diff identity before and after verification. Ignored files remain part of that identity; routine Python test artifacts are prevented by the Python check settings above. If the source diff changes or cannot be revalidated during verification, completed results are retained but marked failed, and the reason is recorded as a bounded, redacted workflow diagnostic. Genuine source modifications remain invalidating failures.

## 11. Final Reporting

The final report is built from checkpointed workflow evidence, including:
- The proposal and its revision.
- Human decisions.
- Implementation result.
- Changed files and diff validation.
- Verification results.
- Failures and remaining risks.
- Evidence references.

The report distinguishes:
- `passed`: approval, successful implementation, valid diff, and all required checks passed with no recorded failures.
- `failed`: the workflow did not satisfy its required success conditions.
- `rejected`: a human rejected the proposal.
- `awaiting_approval`: the workflow is paused for a human decision.

Missing required verification results prevent a successful report.

The final report does not treat an implementation process exit code alone as proof of a successful lifecycle.

## 12. Current Limitations

The following capabilities are not implemented as separate workflow stages:

- A dedicated independent code-review stage.
- An automated review-and-correction loop.
- A graphical approval interface.
- Model-selected verification commands.

The workflow currently performs host-configured verification and evidence-based reporting, but these are not substitutes for an independent human code review.

On Windows environments where managed execution policy blocks Codex from launching its shell process, repository inspection may fail. The fail-closed analysis behavior prevents such a refusal from being treated as successful analysis. This does not itself resolve the underlying process-launch restriction.

## 13. Security Principles

DevFlow follows these boundaries:

- Analysis is read-only.
- Implementation requires explicit human approval.
- Approval is bound to a validated proposal fingerprint and revision.
- Implementation takes place in a registered, isolated worktree.
- File changes are checked against approved scope.
- Verification commands are host-configured.
- Process execution uses argument arrays and `shell=False`.
- Timeouts and output limits constrain subprocess execution.
- Checkpoint corruption and invalid approval state fail closed.
- Reports are derived from recorded evidence rather than model claims.
