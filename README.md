# DOCX editing foundation

This project provides read-only DOCX inspection and controlled text replacement, paragraph insertion, and paragraph removal in Python 3.11+.

## Install and run

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

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

The default provider is local Ollama through LangChain `ChatOllama`, using `qwen2.5:7b` at `http://localhost:11434`. Pull the model with `ollama pull qwen2.5:7b`, then configure `WORDPILOT_LLM_PROVIDER=ollama`, `WORDPILOT_LLM_MODEL=qwen2.5:7b`, and `WORDPILOT_OLLAMA_BASE_URL=http://localhost:11434` (see `.env.example`). Set `WORDPILOT_LLM_PROVIDER=openai` explicitly and provide `OPENAI_API_KEY` to use `ChatOpenAI`; its default model is `gpt-4o-mini`, and `WORDPILOT_LLM_MODEL` overrides the model for either provider. There is no provider fallback or automatic retry. Both adapters request the existing Pydantic `PlanningResult` structure; the planner then validates every operation locally against the strict operation schemas. Ollama structured output depends on the selected model's JSON/schema adherence and may fail or return invalid output; these cases become planning errors and are never automatically sent to OpenAI. Unit tests mock provider calls and make no external API calls. Planning is not connected to the deterministic editing graph.

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
