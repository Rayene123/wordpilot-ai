# CTO demo: DevFlow Python source generation

This demo uses the existing interactive DevFlow CLI to ask Codex to create `average.py` and `test_average.py` in a detached Git worktree. The host checkpoints a validated proposal and waits for a human decision. Only approval continues into implementation. The existing diff validator, fixed verification checks, SQLite checkpoint, and report are used.

## Windows prerequisites

- Windows 10/11, Git for Windows, Python 3.11+ (`requirements.txt`), and Node.js/npm compatible with the project's Next.js 15 dependency (Node.js 18.18 or newer).
- A clean, trusted clone. Git must be able to create `.devflow-worktrees/` under the repository.
- Codex CLI installed independently and authenticated with an allowed account or credential. This repository does not provide or inherit credentials.
- A Codex Windows sandbox backend allowed by local or managed policy. DevFlow requires `DEVFLOW_WINDOWS_SANDBOX` for implementation; it does not change policy or retry with weaker settings.

## Install dependencies

Run these PowerShell commands from the repository root. Python dependencies come from `requirements.txt`; frontend dependencies come from `frontend/package.json` and its lockfile.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci --prefix frontend
npm install -g @openai/codex
codex --version
codex login status
```

If `codex login status` shows no valid session, run `codex` and complete sign-in. `CODEX_CLI_PATH` is optional when `codex` is on `PATH`; set it to the actual executable if needed. On native Windows, select a sandbox backend supported by this device and its policy, for example:

```powershell
$env:DEVFLOW_WINDOWS_SANDBOX = 'mxc'
```

Use `elevated` only when the administrator-provisioned setup is available. `unelevated` has weaker network isolation and should only be used when permitted and appropriate. The read-only startup diagnostic is:

```powershell
.\.venv\Scripts\python.exe scripts/devflow_diagnostics.py
```

## Run the demo

Use the venv interpreter directly so PowerShell script activation policy is irrelevant. Launch the existing CLI:

```powershell
.\.venv\Scripts\python.exe -m dev_workflow
```

Choose menu item `2` and enter this task:

```text
Create average.py with an average(numbers) function that returns the arithmetic mean for a non-empty sequence and raises ValueError for an empty sequence. Add test_average.py pytest tests for positive values, negative values, and the empty sequence.
```

Review the proposal's expected files, scope, and acceptance criteria. Choose `1` to approve, `2` to reject, or `3` to request a bounded revision. No source files or implementation worktree are created before approval. Approval is bound to the exact proposal fingerprint and revision. A rejection produces a report without an implementation worktree.

## Checkpoint and resume

The CLI stores checkpoints in `.devflow/checkpoints.sqlite3` (ignored by Git). If you close the terminal while approval is pending, relaunch the same command and choose menu item `3` to review a pending approval or `4` to select and resume a saved run. Do not remove or replace the SQLite file. The CLI reopens the database and uses the public `resume_pending_approval` API. The final report is printed and retained in checkpoint state.

## Inspect the worktree and diff

After approval, locate the worktree path in the final report. It must be under `.devflow-worktrees/<RUN_ID>`. Inspect the registered worktree and changed files:

```powershell
git worktree list
git -C .devflow-worktrees/<RUN_ID> status --short
git -C .devflow-worktrees/<RUN_ID> diff --no-ext-diff --no-color HEAD
Get-Content .devflow-worktrees/<RUN_ID>/average.py
Get-Content .devflow-worktrees/<RUN_ID>/test_average.py
```

Untracked files are included in DevFlow's host-collected diff report even though plain `git diff` omits them. The report lists changed files and diff validation status. The demo leaves the registered worktree available for inspection; it does not commit, push, or merge it into the main checkout.

## Verification and expected output

The workflow runs its fixed host checks in the worktree:

```text
python -m pytest -q
npm run typecheck   # working directory: frontend
```

The final report shows the proposal decision, implementation result, changed files, diff status, each check's command/status/exit code, failures, and overall status. A fully passing run reports `passed`; failed or unavailable checks remain visibly failed. The Python check runs the configured repository test suite, including any generated `test_average.py` tests.

Known limitation: `frontend/node_modules` is ignored and is not copied into detached Git worktrees. The current graph has no dependency-install stage between implementation and verification. Therefore `npm ci --prefix frontend` in the main checkout does not provision the detached worktree. On a fresh checkout, the frontend typecheck may fail because dependencies are absent there; the report must show that failure and overall status must not be described as successful. The demo does not weaken verification or add a package-install mechanism to hide this limitation.

## Windows execution-policy and troubleshooting

- If `Activate.ps1` is blocked by execution policy, do not change machine policy for this demo. Use the direct `.\.venv\Scripts\python.exe` commands above.
- If Git reports `dubious ownership`, ensure the clone belongs to the current Windows user. Do not broadly mark directories safe.
- If Codex is missing, install it or set `CODEX_CLI_PATH`. If authentication fails, complete `codex` sign-in and rerun `codex login status`.
- If implementation is rejected by Windows sandbox policy, inspect the installed Codex Windows sandbox backend and managed policy. Confirm `DEVFLOW_WINDOWS_SANDBOX` names a permitted, supported backend. DevFlow does not modify settings or retry with a bypass.
- If a check fails, inspect its bounded stdout/stderr in the workflow report and reproduce it from the registered worktree. Never report a failed or `not_run` check as passing.
- To reject a proposal, choose menu item `2`; do not edit SQLite approval state manually.
