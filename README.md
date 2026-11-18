# DOCX editing foundation

This project provides read-only DOCX inspection and controlled text replacement, paragraph insertion, and paragraph removal in Python 3.11+.

## Quick start

For WordPilot, install dependencies, set up the default Ollama provider, and start the backend from the repository root:

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
ollama pull qwen2.5:7b
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

In another terminal, start the frontend:

```powershell
cd frontend
npm ci
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
npm run dev
```

Upload the existing synthetic sample `therapist_session_notes.docx`, enter the instruction from [`therapist_session_notes_instruction.txt`](therapist_session_notes_instruction.txt), review the proposed operations, and explicitly approve before execution. Inspect the generated DOCX and returned integrity results (`completed_verified`, `completed_partially_verified`, or a reported failure). The sample is a fictional software demonstration fixture, not a clinical template. For Groq setup or details about verification limits, see [LLM operation planning](#llm-operation-planning) and [DOCX integrity verification](#docx-integrity-verification).

DevFlow is an optional, separate development tool; it uses Codex CLI and is not needed to run WordPilot.

## Install and run

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

## FastAPI backend

Install dependencies with `python -m pip install -r requirements.txt`, configure Ollama locally or explicitly select Groq in `.env`, then start the API with `uvicorn backend.api:app --host 127.0.0.1 --port 8000`. The API stores uploads in `uploads/` and edited files in `generated-documents/`; these are persistent project directories, not temporary storage, and are ignored by Git. Upload a DOCX, plan an edit, present the returned proposal for approval, then download a completed output by its returned download ID. Pending proposals and document/download registries are process-local and are lost when the server restarts, so those old files become inaccessible through the API but are not automatically deleted. Run a single API process so approval and download state remains available. Remove orphaned files manually only after stopping the server. A future cleanup policy should be limited to files owned by an expired process and run after its registries are no longer active; the API currently provides no public cleanup endpoint and performs no automatic deletion.

On Windows environments where Python's Proactor self-pipe socket setup is blocked, run API tests under WSL instead. From the cloned repository directory, the following creates a Linux virtual environment on first use, installs the declared dependencies, and runs both API and full suites:

```powershell
wsl bash -lc 'python3 -m venv .venv-linux && . .venv-linux/bin/activate && python -m pip install -r requirements.txt && python -m pytest tests/test_api.py -v && python -m pytest -q'
```

## Frontend workspace

Install frontend dependencies and start the Next.js development server:

```powershell
cd frontend
npm install
Copy-Item .env.example .env.local
npm run dev
```

The frontend reads `NEXT_PUBLIC_WORDPILOT_API_URL` from `frontend/.env.local` (default `http://127.0.0.1:8000`). Start the FastAPI backend separately. The browser calls its existing endpoints directly; Groq credentials remain in the backend environment.

## DevFlow development orchestrator

`dev_workflow/` is an optional, separate Python package for orchestrating software-development tasks with Codex CLI. It is not part of WordPilot's DOCX backend and does not use the Groq/Ollama document-editing providers. WordPilot's DOCX application does not require Codex. DevFlow requires the evaluator to install and authenticate Codex CLI independently; this repository does not install it, provide credentials, or inherit another developer's login.

### Architecture overview

```text
WordPilot: DOCX upload -> host inspection -> typed LLM proposal -> human approval
          -> deterministic DOCX edits -> integrity results + generated DOCX

DevFlow:   read-only Codex analysis -> structured proposal -> human approve/revise
          -> isolated Git worktree -> implementation -> diff validation
          -> host-defined verification -> deterministic final report
```

In WordPilot, the LLM proposes typed operations; Python validates them and the deterministic editor applies approved operations. The LLM does not edit DOCX files directly. DevFlow is a separate development workflow and Codex CLI is not a runtime document-editing dependency.

DevFlow's implemented stages are read-only analysis, strict structured proposal, human approval or bounded revision, isolated worktree implementation, host-side diff validation, host-defined verification, and a deterministic report built from recorded evidence. Approval is bound to the reviewed proposal fingerprint and revision. The report distinguishes passed, failed, rejected, and awaiting-approval runs, and records missing or unrun checks without claiming they passed. There is no approval UI, independent review stage, or correction loop.

### Clean-clone setup and host prerequisites

The repository declares Python 3.11+ dependencies in `requirements.txt` and requires Git for DevFlow worktrees. Codex CLI is an external prerequisite for DevFlow only. Install it using the current [official Codex CLI installation instructions](https://developers.openai.com/codex/cli/); with Node.js/npm installed, the documented npm package command is:

```powershell
npm install -g @openai/codex
codex --version
```

Start `codex` and complete sign-in using an account or credential permitted in your environment, then verify with `codex login status`. Authentication is local to the evaluator's Codex setup and is never provisioned by this repository. Current Codex installation and sign-in methods can change; consult the official instructions if those commands differ.

From the repository root, set up Python and install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On POSIX shells use `python3 -m venv .venv`, `source .venv/bin/activate`, then the same two `python -m pip` commands. Run the read-only startup diagnostic (it checks Python dependencies, Git, Codex version/authentication, and whether CLI help advertises analysis/write options; it never runs an agent task):

```powershell
python scripts/devflow_diagnostics.py
```

By default DevFlow discovers `codex` on `PATH`. To select a specific installation, set `CODEX_CLI_PATH` to its executable file in the current shell:

```powershell
$env:CODEX_CLI_PATH = 'C:\path\to\codex.exe'
```

On native Windows, set `DEVFLOW_WINDOWS_SANDBOX` in the same shell before starting DevFlow. It is required for implementation and the optional write-capability preflight. Accepted values are `mxc`, `elevated`, and `unelevated`:

```powershell
$env:DEVFLOW_WINDOWS_SANDBOX = 'elevated'
```

Choose a backend that is available and permitted by the host: use `mxc` when supported by the device and policy, or `elevated` when its administrator-provisioned setup is available. Use `unelevated` only when it is permitted and appropriate for the environment; it provides weaker network isolation than `elevated`. DevFlow passes this value as an explicit Codex Windows sandbox configuration while keeping implementation in `workspace-write`. Missing or invalid values stop implementation before Codex is launched. Selecting a value does not guarantee that the host supports it; Codex setup and managed policy remain authoritative, and DevFlow does not fall back to another implementation. Read-only analysis and non-Windows runs do not require this setting.

The diagnostic's `workspace_write_capability` result only checks CLI help. It does not prove that Codex can successfully write in the current host environment. For the optional manual marker-write preflight and its evidence details, see the single canonical section below. The workflow does not run preflight automatically before implementation.

Run all repository tests with `python -m pytest -q`. Start the document-editing backend with `python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000`; this does not require Codex CLI.

### Interactive DevFlow CLI

From a clean clone, install Python 3.12, Git, and Codex CLI, sign in with `codex login`, then install the repository dependencies. The CLI uses only this checkout's APIs and stores restartable approvals in `.devflow/checkpoints.sqlite3`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
py -3.12 -m dev_workflow
```

On Linux, create and install into a Python 3.12 virtual environment (`python3.12 -m venv .venv`, `. .venv/bin/activate`, `python -m pip install -r requirements.txt`) and launch with `python -m dev_workflow`. If PowerShell blocks activation, invoke `.venv\Scripts\python.exe` directly; activation is optional. On Windows, set `DEVFLOW_WINDOWS_SANDBOX` to an allowed backend before implementation, as described above.

The menu provides these actions: `1` check environment, `2` start a task, `3` review a pending approval, `4` resume a saved workflow, `5` inspect status/report, `6` run configured verification in a registered implementation worktree, and `7` exit. The approval screen shows the exact proposal and file scope before accepting approve, reject, or change requests. Implementation starts only after approval and runs in the registered detached worktree. Verification checks are fixed by the host (`python -m pytest -q` and `npm run typecheck`); a manual verification rerun is displayed in the terminal and does not replace the workflow's checkpointed report evidence.

End-to-end code-generation demo: launch `py -3.12 -m dev_workflow`, choose `2`, and enter `Create hello.py that prints Hello, DevFlow!`. Review the proposed file and scope, then choose `1` to approve. DevFlow implements only after approval in `.devflow-worktrees/<run-id>`, validates the resulting diff, runs its configured checks, and prints the final report. If the session is interrupted, use menu item `4` to select the saved run; no UUID copying is needed.

The repository has no published OS support matrix, and a clean clone has not been verified across Windows, Linux, and macOS. Treat the commands above as setup guidance, not a cross-platform support claim. Windows sandbox behavior depends on the host Codex sandbox backend and managed execution policy; inspect and configure these on the evaluator's machine. DevFlow does not change those settings. Linux and macOS operation, including sandbox capabilities, must be verified on those hosts before claiming support.

For a reproducible Windows CTO demonstration of generated Python source, approval, isolated worktree execution, checkpointing, validation, verification, and reporting, follow the dedicated [DevFlow CTO demo guide](docs/DEVFLOW_CTO_DEMO.md).

DevFlow approvals use a persistent SQLite LangGraph checkpointer. The host must select and reuse the same database file and workflow run ID to resume an approval after restarting the process. The default local developer location is `.devflow/checkpoints.sqlite3`; this directory is ignored by Git. Evidence JSON files are for inspection and are never used as approval tokens. Open and close the saver around each process lifecycle:

```python
from pathlib import Path
from dev_workflow import (
    build_dev_workflow,
    new_workflow_state,
    open_sqlite_checkpointer,
    new_workflow_decision,
    resume_pending_approval,
    workflow_config,
)
from dev_workflow.schemas import FinalReport, TaskProposal

database = Path(".devflow/checkpoints.sqlite3")
with open_sqlite_checkpointer(database) as checkpointer:
    graph = build_dev_workflow(checkpointer=checkpointer)
    state = new_workflow_state(task, repository_path)
    pending = graph.invoke(state, workflow_config(state["run_id"]))
    proposal = TaskProposal.model_validate(pending["proposal"])
    # Display this proposal and fingerprint to a human reviewer before deciding.
    human_decision = new_workflow_decision(
        proposal,
        action="approve",  # Or "reject" / "request_changes" with feedback.
        proposal_revision=pending["revision_count"],
    )

# Reopen the same database and use the run ID from the saved pending state.
with open_sqlite_checkpointer(database) as checkpointer:
    graph = build_dev_workflow(checkpointer=checkpointer)
    result = resume_pending_approval(graph, pending["run_id"], human_decision)
    report = FinalReport.model_validate(result["final_report"])
    print(report.model_dump_json(indent=2))
```

`new_workflow_decision` binds the human action to the proposal fingerprint and revision. `resume_pending_approval` verifies that the saved run is paused at its approval interrupt, that the interrupt and current proposal agree, and that the decision fingerprint and revision match before resuming LangGraph. In this example, `task` and `repository_path` are values supplied by the caller. A `request_changes` decision requires `feedback`; the workflow sends it back for a bounded proposal revision.

### Optional manual write-capability preflight

The write-capability preflight runs only when invoked manually, in a separate disposable Git worktree. It uses the same Codex CLI implementation adapter and `workspace-write` sandbox, requests a unique marker file, verifies its exact contents and complete diff, saves a JSON evidence record under the system temporary directory, and removes only that disposable worktree after saving evidence. A failed preflight reports failure; it does not change Windows sandbox settings or retry with weaker permissions. Run it from the repository root with:

```powershell
python -c "from dev_workflow.preflight import run_write_capability_preflight; import json; print(json.dumps(run_write_capability_preflight('.').model_dump(mode='json'), indent=2))"
```

`CODEX_CLI_PATH` must name an existing Codex executable file. DevFlow uses it first, then falls back to `codex` on `PATH`; it does not search extension directories. The same resolver and implementation adapter are used by preflight and approved implementation.

This command invokes Codex CLI in a disposable worktree and writes/removes its unique marker there. Inspect the reported evidence path. If it fails on Windows, investigate the host-controlled Codex `windows.sandbox` backend selection and any managed execution policy; DevFlow does not alter either setting. This preflight is not a task-graph stage and is not invoked automatically.

After implementation passes diff validation, DevFlow runs its fixed, host-defined verification checks in the worktree:

```text
python -m pytest -q
npm run typecheck
```

The frontend command runs from `frontend/`. Its dependencies must be available in the detached worktree; `node_modules` is not part of the Git worktree, and DevFlow does not install packages as part of its checks. The host must provision dependencies for the worktree before typechecking; the current graph has no package-install hook between implementation and verification. Verification evidence is tied to the worktree diff. If a process is interrupted before a verification result is checkpointed, verification may be retried on resume. Completed, checkpointed stages are resumed from their saved state and are not unnecessarily repeated. The deterministic final report records implementation, diff-validation, and verification outcomes, plus failures, remaining risks, and evidence references.

Run the DevFlow tests from the repository root:

```powershell
python -m pytest tests/test_dev_workflow_schemas.py tests/test_dev_workflow_state.py tests/test_dev_workflow_codex_cli.py tests/test_dev_workflow_preflight.py tests/test_dev_workflow_verification.py tests/test_dev_workflow_reporting.py tests/test_dev_workflow_graph.py tests/test_dev_workflow_worktree.py tests/test_dev_workflow_checkpointing.py tests/test_devflow_diagnostics.py -q
```

Import the workflow from the root package with `from dev_workflow import build_dev_workflow`. The full backend and repository test command remains `python -m pytest -q`.

## Agent foundation

`backend/operation_schemas.py` defines three strict-by-shape Pydantic operation models and a discriminated union keyed by `operation_type` (`replace`, `insert`, or `remove`). `backend/agent/schemas.py` re-exports those same models for compatibility. Extra fields and unknown operation names are rejected. `DocumentAgentState` in `backend/agent/state.py` contains paths and JSON-friendly values only; it does not hold open DOCX objects.

`create_document_tools(document_path, output_path=None)` creates exactly four LangChain tools bound to host-selected paths: inspect, replace, insert, and remove. The model cannot supply paths or choose arbitrary Python functions. Tool arguments use the validated schemas and results are JSON strings containing the operation result. A successful edit advances the tool set's active document to its output so later edits can build on earlier ones. This module provides tool adapters and state only; it does not build or invoke a LangGraph workflow.

## LangGraph workflow

`build_document_graph(output_directory)` compiles the deterministic workflow. The output directory must already exist and is selected by the host. Invoke the compiled graph with the input path, user instruction, and proposed operation dictionaries:

```python
from backend.agent import build_document_graph

graph = build_document_graph("./edited-documents")
result = graph.invoke({
    "document_path": "./source.docx",
    "user_instruction": "Update the heading and add a paragraph.",
    "proposed_operations": [
        {"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"},
    ],
})
print(result["workflow_status"], result["output_path"], result["final_response"])
```

The graph inspects first, validates the full operation list before any edits, then runs operations sequentially through the fixed tools. Each run writes to a unique output filename in the configured directory; the source document is never used as the output. A failure before edits produces `failed` with no output. A failure after one or more successful edits produces `partial` and points to the valid partial output. Validation errors prevent all editing. Run the complete tests with `python -m pytest -q`; no LLM or external API is needed.

## LLM operation planning

`OperationPlanner` (or `plan_operations`) turns a user instruction and the JSON-friendly result of `inspect_document` into a `PlanningResult`. Proposed operation dictionaries are validated again against the existing strict Pydantic operation union. The result can instead request clarification or report an unsupported request, with no operations in either case. Repeated or absent exact targets detected in host-side summary checks become clarification requests. Planning does not execute document tools or receive filesystem access.

The default provider is local Ollama through LangChain `ChatOllama`, using `qwen2.5:7b` at `http://localhost:11434`. Pull the model with `ollama pull qwen2.5:7b`, then configure `WORDPILOT_LLM_PROVIDER=ollama`, `WORDPILOT_LLM_MODEL=qwen2.5:7b`, and `WORDPILOT_OLLAMA_BASE_URL=http://localhost:11434` (see `.env.example`). To use the optional Groq API, set `WORDPILOT_LLM_PROVIDER=groq`, `GROQ_MODEL=openai/gpt-oss-120b`, and `GROQ_API_KEY`. The provider-specific model settings are independent: Ollama reads `WORDPILOT_LLM_MODEL`; Groq reads `GROQ_MODEL`. Provider creation loads a project `.env` file without overriding values already set in the process environment. Keep `.env` private and out of version control. Groq receives the planner instruction and inspection summary, so select it explicitly. There is no provider fallback or automatic retry. The provider requests the Pydantic `PlanningResult`; the planner validates every operation locally against the strict operation schemas. Unit tests mock provider calls and make no external API calls. Run `python -u scripts/ollama_planner_integration.py` or `python -u scripts/groq_planner_integration.py` to benchmark six synthetic planning cases; these scripts never execute proposals. Planning is not connected to the deterministic editing graph.

## Planning and human approval

The LLM-assisted workflow is exposed separately from `build_document_graph`:

```python
from backend.agent import plan_document, execute_approved_proposal

proposal = plan_document("source.docx", "Replace Draft with Final")
if proposal["planning_status"] == "awaiting_approval":
    # Present proposal["proposed_operations"] to a human before deciding.
    result = execute_approved_proposal(
        proposal, approved=True, output_directory="edited-documents",
    )
```

Planning only inspects and proposes. Its serializable result contains the summary, validated operations, a planning status, and a message; it does not create an edited document. Clarification and unsupported requests have no executable operations. The caller must explicitly pass `approved=True` to execute, `approved=False` rejects, and omitted approval remains pending. Execution verifies the opaque one-time process token, requires explicit approval, checks the source DOCX fingerprint, and validates operations again before editing through the deterministic tools. The output path is generated inside the host-selected directory and cannot be supplied by the caller. The original source is not overwritten.

After a complete edit, orchestration calls the integrity verifier with the original source path, generated output path, and exact operations bound to the approved proposal. The returned result includes `integrity_status` and the full `integrity_report`:

- `completed_verified`: supported integrity checks passed.
- `completed_partially_verified`: checks passed, but the report lists features that could not be fully checked.
- `integrity_failed`: verification found unexpected changes or raised an error. The generated file is kept for diagnosis and explicitly reported as unverified; it is not automatically deleted.
- `execution_failed`: editing or output validation failed. Any partial output is preserved and labeled unverified.
- `pending` / `rejected`: no document edits or integrity verification occurred.

The in-memory approval registry remains process-local, so planning and approval must happen in the same Python process. It is not a defense against arbitrary code already running inside that process. The deterministic graph continues to accept explicitly supplied operations and retains its prior inspect/validate/execute behavior without this approval and verification step.

## DOCX integrity verification

`backend.docx_editor.integrity.verify_document_integrity(original_path, edited_path, approved_operations)` is a read-only comparison API. It validates operations with the existing Pydantic schemas, simulates the approved replacements/insertions/removals in an in-memory expectation, and returns an `IntegrityReport` with an overall status, named checks, expected changes, unexpected changes, unverified features, and errors. It does not modify either DOCX. The approved execution flow calls it only after a complete deterministic edit.

The verifier checks ordered body and table-cell paragraphs (including nested tables and paragraph position relative to tables), paragraph styles, alignment and direct paragraph properties, run bold/italic/underline/font name/font size and other direct run properties, table dimensions and table/row/cell properties, document section properties, inline image references and bytes, and every ZIP package component. Image placement and cropping are not compared, so documents with inline images are reported as `partially_verified`. Supported operations affect only the main `word/document.xml` content; all other package parts are required to remain byte-identical. ZIP compression and entry order are ignored by comparing decompressed component hashes. Package inspection is bounded to 10,000 entries, 64 MiB per component, and 256 MiB total uncompressed size. Invalid operations, missing files, malformed packages, unexpected differences, or missing expected targets produce `failed`. Known Word features not semantically interpreted, such as non-empty headers/footers, tracked changes, content controls, embedded objects, VML shapes, fields, bookmarks, and hyperlinks, produce `partially_verified` if no other failure is found. This checks preservation within the described scope and does not certify full OOXML compatibility.

## Replacement approach

`replace_text` scans body paragraphs and table-cell paragraphs in document order. It groups adjacent runs only when they contain paragraph run properties and text nodes. It changes the underlying `w:t` nodes directly, leaving the run elements, paragraph properties, and other XML in place. Text spanning multiple runs is placed in the first matched run, so it inherits that run's formatting. A run containing a drawing, field, break, or other non-text element forms a hard boundary. Missing targets return zero replacements and do not save the document.

The output path is optional. If omitted, a found replacement updates the source file. Supplying an output path leaves the source unchanged.

## Paragraph operations

`insert_paragraph` inserts before or after one exact-text paragraph found in the body or any table cell. It refuses missing or duplicate targets unless a location is supplied. Body locations use `(paragraph_index,)`; table-cell locations use `(table_index, row_index, cell_index, paragraph_index)`, with nested tables included in table order. The selected paragraph must match the target text exactly. The new paragraph inherits the target style unless a style is supplied. The target paragraph's XML is left in place.

`remove_paragraph` removes one exact-text paragraph only when the match is unique, or when a location is supplied. Body locations use `(paragraph_index,)`; table-cell locations use `(table_index, row_index, cell_index, paragraph_index)`, with nested tables included in table order. It refuses headings, the only paragraph in a location, and paragraphs containing non-text XML such as images, breaks, or fields.

Insertion and removal return `OperationResult`. Replacement keeps its existing `replacements` field and also reports `operation_type`, `success`, `affected_elements`, `warnings`, and `error`. Missing/ambiguous targets produce unsuccessful results without saving an output file.

## Limitations

- Replacement does not span paragraphs, table cells, or runs containing non-text XML elements.
- Paragraph insertion requires exactly one exact-text target; removal supports only direct body paragraph indexes and direct paragraphs in table cells through the explicit location tuple. Removal intentionally refuses heading-like paragraphs and paragraphs containing non-text XML.
- Tracked changes, field codes, hyperlinks, content controls, text boxes, headers, footers, footnotes, and endnotes are not comprehensively inspected or edited. No support is claimed for those structures.
- The inspector reports body paragraphs and table cell paragraphs (including nested tables), top-level tables, and inline images. Floating images and shapes are not counted.
- DOCX package parts that `python-docx` does not preserve may be at risk when a document is opened and saved. Keep an original backup for documents containing unsupported Word features.

## Package preservation check

The synthetic package regression test compares ZIP entries around a replacement. In that fixture, the package entry names remain the same; `word/document.xml` changes, while `word/styles.xml`, `word/_rels/document.xml.rels`, `docProps/core.xml`, `docProps/app.xml`, and the embedded image bytes remain unchanged. This is evidence for the tested fixture only. Saving through python-docx serializes the package and does not guarantee preservation of every extension or unsupported Word feature in arbitrary DOCX files.
