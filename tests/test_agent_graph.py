from __future__ import annotations

import json

from docx import Document

from backend.agent import build_document_graph


def _invoke(graph, path, operations):
    """Invoke the graph with its three public inputs and a proposed edit list."""
    return graph.invoke({
        "document_path": str(path),
        "user_instruction": "Apply the proposed document edits.",
        "proposed_operations": operations,
    })


def test_successful_inspection_and_one_replacement(make_docx, tmp_path):
    """Checks inspection, replacement, final response, and source-file protection."""
    source = make_docx()
    original = source.read_bytes()
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [{
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated",
    }])

    assert result["workflow_status"] == "completed"
    assert result["integrity_status"] == "passed"
    assert result["integrity_report"]["status"] == "passed"
    assert result["document_summary"]["paragraphs"][1]["text"] == "Unrelated paragraph"
    assert result["execution_results"][0]["success"] is True
    assert Document(result["output_path"]).paragraphs[1].text == "Updated paragraph"
    assert source.read_bytes() == original
    assert result["output_path"] != str(source)
    assert result["final_response"].startswith("Completed 1 document operation")


def test_graph_integrity_failure_cannot_finalize_success(make_docx, tmp_path, monkeypatch):
    from backend.docx_editor.integrity import IntegrityCheck, IntegrityReport

    source = make_docx()
    report = IntegrityReport(status="failed", checks=[IntegrityCheck(name="test", status="failed", details=["failure"])],
                             unexpected_changes=[], expected_changes=[], unverified_features=[], errors=[], summary="test failure")
    result = _invoke(build_document_graph(tmp_path, verifier=lambda *args: report), source, [{
        "operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated",
    }])
    assert result["workflow_status"] == "failed"
    assert result["integrity_status"] == "failed"
    assert result["output_path"] is not None
    assert "failed" in result["final_response"].lower()
    assert not Document(result["output_path"]).paragraphs[1].text == "Unrelated paragraph"


def test_multiple_operations_execute_sequentially(make_docx, tmp_path):
    """Checks later edits read the output produced by earlier edits."""
    source = make_docx()
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [
        {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Changed"},
        {
            "operation_type": "insert", "anchor_paragraph_text": "Changed paragraph",
            "inserted_text": "Temporary", "position": "after",
        },
        {"operation_type": "replace", "target_text": "Temporary", "replacement_text": "Remove me"},
        {"operation_type": "remove", "target_text": "Remove me"},
    ])

    assert result["workflow_status"] == "completed"
    assert [item["success"] for item in result["execution_results"]] == [True, True, True, True]
    reopened = Document(result["output_path"])
    assert reopened.paragraphs[1].text == "Changed paragraph"
    assert all(p.text != "Remove me" for p in reopened.paragraphs)
    assert result["execution_results"][1]["output_path"] == result["output_path"]


def test_invalid_operation_list_prevents_all_edits(make_docx, tmp_path):
    """Checks full-list validation blocks valid edits when any operation is invalid."""
    source = make_docx()
    original = source.read_bytes()
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [
        {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Changed"},
        {"operation_type": "run_python", "code": "open('bad').write('x')"},
    ])

    assert result["workflow_status"] == "failed"
    assert len(result["validation_errors"]) == 1
    assert not result["execution_results"]
    assert result["output_path"] is None
    assert source.read_bytes() == original
    assert list(tmp_path.glob("*_edited_*.docx")) == []
    assert "failed" in result["final_response"].lower()


def test_missing_input_document_fails_cleanly(tmp_path):
    """Checks a missing source produces a failed state and no output path."""
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, tmp_path / "missing.docx", [
        {"operation_type": "replace", "target_text": "x", "replacement_text": "y"},
    ])
    assert result["workflow_status"] == "failed"
    assert result["document_summary"] is None
    assert "does not exist" in result["errors"][0]
    assert result["output_path"] is None
    assert "failed" in result["final_response"].lower()


def test_corrupt_docx_fails_inspection_without_output(tmp_path):
    """Checks unreadable DOCX bytes fail at inspection without generating output."""
    source = tmp_path / "corrupt.docx"
    source.write_text("not a DOCX archive", encoding="utf-8")
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [
        {"operation_type": "replace", "target_text": "x", "replacement_text": "y"},
    ])
    assert result["workflow_status"] == "failed"
    assert result["document_summary"] is None
    assert "inspection failed" in result["errors"][0].lower()
    assert result["output_path"] is None


def test_missing_target_during_execution_fails_without_output(make_docx, tmp_path):
    """Checks a first-operation target miss fails cleanly without an output file."""
    source = make_docx()
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [
        {"operation_type": "replace", "target_text": "not found", "replacement_text": "x"},
    ])
    assert result["workflow_status"] == "failed"
    assert result["execution_results"][0]["success"] is False
    assert result["output_path"] is None
    assert "failed" in result["final_response"].lower()
    assert list(tmp_path.glob("*_edited_*.docx")) == []


def test_middle_failure_returns_partial_output_and_stops(make_docx, tmp_path):
    """Checks a later failure reports partial output and prevents following edits."""
    source = make_docx()
    original = source.read_bytes()
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, source, [
        {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated"},
        {"operation_type": "replace", "target_text": "missing", "replacement_text": "x"},
        {"operation_type": "replace", "target_text": "Keep", "replacement_text": "Should not run"},
    ])

    assert result["workflow_status"] == "partial"
    assert len(result["execution_results"]) == 2
    assert result["execution_results"][0]["success"] is True
    assert result["execution_results"][1]["success"] is False
    assert result["output_path"] is not None
    partial = Document(result["output_path"])
    assert partial.paragraphs[1].text == "Updated paragraph"
    assert partial.paragraphs[0].text == "Keep old text end"
    assert source.read_bytes() == original
    assert "partial output" in result["final_response"].lower()


def test_graph_state_is_json_serializable(make_docx, tmp_path):
    """Checks the complete returned graph state round-trips through JSON."""
    graph = build_document_graph(tmp_path)
    result = _invoke(graph, make_docx(), [
        {"operation_type": "replace", "target_text": "Unrelated", "replacement_text": "Updated"},
    ])
    restored = json.loads(json.dumps(result))
    assert restored["workflow_status"] == "completed"
    assert restored["execution_results"][0]["operation_index"] == 0


def test_graph_requires_existing_host_output_directory(tmp_path):
    """Checks graph construction rejects an output directory that does not exist."""
    missing_output = tmp_path / "not-created"
    try:
        build_document_graph(missing_output)
    except ValueError as exc:
        assert "must already exist" in str(exc)
    else:
        raise AssertionError("Expected a missing output directory to be rejected")
