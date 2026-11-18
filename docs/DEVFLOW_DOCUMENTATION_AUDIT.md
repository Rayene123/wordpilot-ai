# DevFlow documentation accuracy audit

## Scope and evidence standard

This audit inspected the repository README, both Markdown files under `docs/`, the DevFlow lifecycle guide, the DevFlow SVG diagram, DevFlow source and CLI, `pyproject.toml`, `requirements.txt`, frontend package metadata, the diagnostics script, `.gitignore`, and the DevFlow-focused tests. The working tree contained the existing edit to `docs/DEVFLOW_CTO_DEMO.md`; that edit was preserved and extended. No source, test, dependency, runtime configuration, sample document, or diagram file was changed.

Evidence is separated into: (1) prior executions reported by the project owner, (2) implementation and automated-test evidence inspected in the repository, and (3) untested behavior or limitations. The live execution and suite totals below were supplied as prior execution records; this audit did not rerun Codex or the test suite.

## Findings

| Area | Previous documentation | Actual implementation/evidence | Issue | Action |
|---|---|---|---|---|
| README and onboarding | README described DevFlow's purpose and linked only to the lifecycle guide. | The repository has a specific Windows CTO demo guide with CMD setup, external Codex prerequisites, and mandatory preflight-first sequence. | A new developer lacked a direct reproducibility path from the README. | Added links to the demo, lifecycle, and audit, plus the Python/Git/Codex prerequisites and execution evidence boundary. |
| Lifecycle and CLI | Lifecycle described the graph but omitted the live environment preflight and interactive menu. | `dev_workflow/__main__.py` exposes options 1–7; option 2 and implementation resume require a successful live preflight in the current CLI session. | Important setup and user-flow gates were not discoverable in the lifecycle reference. | Added host setup, preflight behavior, supported menu actions, and links to the exact validated Windows workflow. |
| Architecture evidence note | Architecture stated that `dev_workflow/preflight.py` and its test had pre-existing uncommitted changes during an earlier audit. | At this audit, Git status showed only the existing `docs/DEVFLOW_CTO_DEMO.md` modification. | The note described a transient checkout state as current context. | Replaced it with a stable evidence note linking to this audit. |

The CTO demo guide was also checked against the option numbering, approval decisions, preflight evidence, checkpoint location, worktree behavior, verification selection, and report fields. It explicitly identifies `calculate_square(number)` as the previously validated task and `average.py` as a proposed future demo. The Windows live run used CMD, Python 3.12.10, `DEVFLOW_WINDOWS_SANDBOX=elevated`, and the VS Code extension's Codex executable path.

## Evidence categories

### 1. Reported as verified by prior execution

- Windows CMD workflow using Python 3.12.10 and `DEVFLOW_WINDOWS_SANDBOX=elevated`.
- Successful option 1 preflight (`Overall: passed`), including Codex authentication, read-only execution, workspace-write in a disposable worktree, marker and host diff validation, and cleanup.
- The isolated `calculate_square(number)` task.
- Automated suite result reported by the project owner: **393 passed, 1 skipped**.

These are prior execution results provided for this audit. They were not independently reproduced during this documentation-only work. They do not establish results for `average.py`, other sandbox choices, or every Windows installation.

### 2. Supported by source inspection and repository tests

- CLI menu options 1–7, preflight gating, proposal review, approval/rejection/change requests, workflow resume, status/report inspection, available verification, and exit behavior.
- Strict proposal validation and proposal fingerprints bound to the current revision; the graph routes to implementation only after a valid approval decision.
- Detached worktree creation under `.devflow-worktrees/<run_id>`, host-collected tracked/untracked/ignored-file inspection, and approved scope/action validation.
- SQLite checkpoints at the CLI-selected `.devflow/checkpoints.sqlite3`; guarded pending-approval resume and report reconstruction.
- Host-defined verification selection: Python changes use `python -m pytest -q -p no:cacheprovider`; frontend changes use `npm run typecheck`; documentation-only changes select no unrelated suite. Python verification sets `PYTHONDONTWRITEBYTECODE=1`; checks have timeouts and bounded/redacted output, and later checks are `not_run` after a failure or timeout.
- Final reports distinguish `passed`, `failed`, `rejected`, and `awaiting_approval`; check statuses include `passed`, `failed`, `timed_out`, and `not_run`.
- The DevFlow tests exercise these behaviors, predominantly with fake adapters and isolated temporary repositories. They do not establish live Codex authentication, account availability, or host sandbox policy.
- Frontend dependencies are not installed into/copy-provisioned for detached worktrees. There is no workflow dependency-install stage, so frontend typecheck may fail when dependencies are missing there.

### 3. Untested or limited

- The average task is an example for a future demonstration; no execution or passing result is claimed.
- The live run does not validate `mxc`, `unelevated`, other sandbox settings, or every managed Windows policy. Source accepts those backend names, subject to Codex and host policy.
- Linux/macOS live operation is not established by the supplied execution evidence. The documentation does not claim a tested cross-platform support matrix.
- No dedicated independent code-review stage, automated correction loop, graphical approval UI, or model-selected verification commands exist.
- The actual prior run logs and automated test report were not present as repository artifacts during this audit; their reported outcomes are recorded as owner-provided evidence.

## Corrections and diagram review

- Added the README entry point to the CMD demo and lifecycle guide, and stated the development-time/runtime boundary explicitly: DevFlow does not ship in WordPilot's delivered runtime or process its end-user instructions.
- Added the host setup, preflight gate, complete menu map, and platform/evidence qualifications to the lifecycle guide.
- Removed the stale architecture sentence about transient uncommitted source/test changes and linked its DevFlow evidence note to this audit.
- Retained the existing CMD demo details and its distinction between verified `calculate_square(number)` and proposed `average.py`.
- Inspected `docs/images/devflow-development-lifecycle.svg`; its preflight gate, proposal review/revision/rejection paths, approval boundary, detached worktree, host diff validation, verification, reporting, checkpoint, and runtime separation agree with the graph and CLI. Tightened its Python label to the declared and validated 3.12 version.

## Reproduce the previously validated Windows workflow

These are the previously validated launch commands. The project dependencies and Codex CLI must already be installed separately; DevFlow does not install Codex or provide credentials. The extension-version directory may differ on another installation.

```bat
cd /d C:\Users\rayen\Desktop\wordpilot-ai

set DEVFLOW_WINDOWS_SANDBOX=elevated
set CODEX_CLI_PATH=%USERPROFILE%\.vscode\extensions\openai.chatgpt-26.1111.21308-win32-x64\bin\windows-x86_64\codex.exe

if exist "%CODEX_CLI_PATH%" (echo Codex CLI found) else (echo Codex CLI NOT found)

py -3.12 -m dev_workflow
```

At the CLI, select **1. Check environment** and proceed only if the preflight passes. Then select **2. Start a new task**, enter the task description, type `END` alone on a new line to submit it, review the proposal, and explicitly approve before implementation. In CMD, `set` applies to the current terminal session. `CODEX_CLI_PATH` can be omitted if `codex` is available on `PATH`.

## Remaining documentation and validation gaps

- Preserve the prior live run logs and automated test report as reviewable evidence if they are available; no such evidence artifact was found in the inspected repository paths.
- Validate additional Windows sandbox setups and non-Windows hosts before documenting them as executed.
- Frontend verification still depends on dependencies being available inside the detached worktree; the current workflow has no provisioning step.

## Validation performed for this audit

- Inspected local Markdown links, image references, menu labels, Python/environment declarations, source entry points, and relevant DevFlow tests by source search/read.
- `git diff --check` is the requested whitespace validation; no runtime tests or live Codex commands were run during this documentation audit.
