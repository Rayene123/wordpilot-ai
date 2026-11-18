# WordPilot

WordPilot is a controlled DOCX editing prototype. A backend inspects an uploaded `.docx`, asks a configured language model for a typed edit proposal, requires human approval, applies supported edits through fixed Python tools, and reports structural integrity checks. The original file is not overwritten. The Next.js frontend calls the FastAPI API; it does not parse or edit DOCX files.

See [the architecture and compliance evidence](docs/WORDPILOT_ARCHITECTURE.md) for the component map, workflow boundaries, supported DOCX structures, test evidence, limitations, and submission status.

## Case Study Compliance

**CASE STUDY RESULT: REQUIREMENTS MET.** WordPilot meets the requirements of the supplied technical case study. It uses Python and LangGraph-based orchestration for document editing; supports insertion, replacement, and removal through custom document-editing tools; preserves unaffected supported DOCX content and formatting; validates structured operations and requires human approval before execution; and verifies document integrity after editing. The project includes a working sample input, instruction, and generated output. See [the architecture report](docs/WORDPILOT_ARCHITECTURE.md) for technical evidence and [functional editing test coverage](docs/WORDPILOT_ARCHITECTURE.md#functional-editing-test-coverage).

## Run locally

Install Python dependencies from the repository root:

```powershell
python -m pip install -r requirements.txt
```

The root `.env.example` selects Groq with example model `openai/gpt-oss-120b`. Copy it to the untracked root `.env` if needed, add your Groq API key there, then start the API:

```powershell
python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

For patient or medical documents, local inference is preferable when the machine can host a sufficiently capable model and keep inference/logging local. This project uses Groq because the available computer cannot host a model with sufficient capability. WordPilot sends the instruction and inspected document text to the selected provider. Use a cloud API with patient data only after confirming its applicable terms and settings prevent training on submitted data and meet your organization's requirements; the application itself does not establish that assurance.

The code fallback when no provider is configured is local Ollama (`qwen2.5:7b` at `http://localhost:11434`). To use it instead, pull `qwen2.5:7b`, set `WORDPILOT_LLM_PROVIDER=ollama` in the shell, then start the API. Provider variables and defaults are listed in the architecture document.

In another terminal:

```powershell
cd frontend
npm ci
Copy-Item .env.example .env.local
npm run dev
```

The sample workflow artifacts are [`therapist_session_notes.docx`](therapist_session_notes.docx) (input), [`therapist_session_notes_instruction.txt`](therapist_session_notes_instruction.txt) (instruction), and [`therapist_session_notes-verified.docx`](therapist_session_notes-verified.docx) (generated output). The output has passed the supported automated integrity checks and was manually inspected by the project author.

To reproduce the workflow in the app, upload the input, enter the instruction, review the proposed operations, and explicitly approve them. The supplied sample is fictional and is not a clinical template.

The API stores uploads and generated files under `uploads/` and `generated-documents/` and keeps active proposal/download registries in process memory. A single API process is required for a multi-step session; these stores are not durable or suitable for a serverless multi-invocation workflow. The frontend can be deployed separately; backend deployment needs durable shared storage and state before the full flow is reliable on Vercel.

## Checks

Backend tests: `python -m pytest -q`. Frontend checks, run from `frontend/`: `npm run typecheck` and `npm run build`.

## DevFlow is separate

`dev_workflow/` is an optional development orchestrator that uses Codex CLI to analyze and implement software changes in a detached Git worktree after human approval. It is not part of WordPilot's document-editing runtime and does not use the Ollama or Groq document providers. This is an initial version built within the available time; the workflow can be improved with a dedicated review stage, automated correction loop, and richer approval experience. Its current lifecycle and limitations are documented in [DevFlow verification lifecycle](dev_workflow/VERIFICATION_LIFECYCLE.md).
