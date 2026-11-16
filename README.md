# DOCX editing foundation

This project provides read-only DOCX inspection and controlled text replacement, paragraph insertion, and paragraph removal in Python 3.11+.

## Install and run

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

## Agent foundation

`backend/agent/schemas.py` defines three strict-by-shape Pydantic operation models and a discriminated union keyed by `operation_type` (`replace`, `insert`, or `remove`). Extra fields and unknown operation names are rejected. `DocumentAgentState` in `backend/agent/state.py` contains paths and JSON-friendly values only; it does not hold open DOCX objects.

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
