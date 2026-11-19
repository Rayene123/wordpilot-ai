from __future__ import annotations

import hashlib
import io
import zipfile

from docx import Document
from fastapi.testclient import TestClient
import pytest

from backend import api


@pytest.fixture(autouse=True)
def reset_api_state():
    api._documents.clear()
    api._proposals.clear()
    api._downloads.clear()
    yield
    api._documents.clear()
    api._proposals.clear()
    api._downloads.clear()


def _client():
    return TestClient(api.app)


def _docx_bytes() -> bytes:
    stream = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Status: Draft")
    doc.save(stream)
    return stream.getvalue()


def _upload(client):
    return client.post("/api/documents/upload", files={"file": ("sample.docx", _docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})


def test_health_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("WORDPILOT_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("WORDPILOT_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("GROQ_API_KEY", "secret-value")
    response = _client().get("/api/health")
    assert response.json() == {"status": "ok", "provider": "groq", "model": "openai/gpt-oss-120b"}
    assert "secret-value" not in response.text


def test_upload_valid_docx_and_reject_bad_or_large(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    client = _client()
    valid = _upload(client)
    assert valid.status_code == 200
    assert valid.json()["inspection"]["paragraph_count"] == 1
    assert client.post("/api/documents/upload", files={"file": ("bad.docx", b"no", "application/octet-stream")}).status_code == 400
    assert client.post("/api/documents/upload", files={"file": ("bad.txt", b"x", "text/plain")}).status_code == 415


def test_oversized_upload_returns_413(monkeypatch):
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 20)
    response = _upload(_client())
    assert response.status_code == 413


def test_oversized_planning_context_returns_clear_413(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    client = _client()
    doc_id = _upload(client).json()["document_id"]
    monkeypatch.setattr(api, "plan_document", lambda *args: {
        "planning_status": "too_large",
        "final_response": "This document is too large to plan safely in one request. Split it and try again.",
    })

    response = client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "edit"})

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "planning_context_too_large"
    assert "too large to plan safely" in response.json()["detail"]["message"]


def test_plan_and_approve_validates_exact_proposal(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "outputs")
    client = _client()
    uploaded = _upload(client).json()
    doc_id = uploaded["document_id"]
    source = api._documents[doc_id]
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    proposal = {"planning_status": "awaiting_approval", "approval_status": "pending", "execution_status": "not_executed",
        "user_instruction": "replace Draft", "proposed_operations": [{"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"}],
        "validated_operations": [{"operation_type": "replace", "target_text": "Draft", "replacement_text": "Final"}],
        "approved_operations": [], "clarification_request": None, "execution_results": [], "warnings": [], "errors": [], "final_response": "Ready.", "proposal_version": 1,
        "document_summary": {"paragraphs": [{"text": "Status: Draft", "runs": [{"font_size_pt": 12.0}]}]}}
    monkeypatch.setattr(api, "plan_document", lambda *a: {**proposal, "document_path": "private", "output_path": None, "proposal_digest": "very-secret-token-value-123456", "source_fingerprint": "x"})
    planned = client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "replace Draft"})
    assert planned.status_code == 200
    payload = planned.json()
    # Simulate a browser JSON round trip, which serializes 12.0 as 12.
    payload["document_summary"]["paragraphs"][0]["runs"][0]["font_size_pt"] = 12
    assert "document_path" not in payload
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    changed = dict(payload)
    changed["proposal"] = {**{k: v for k, v in payload.items() if k != "proposal_token"}, "proposed_operations": []}
    assert client.post(f"/api/documents/{doc_id}/approve", json={"proposal_token": payload["proposal_token"], "proposal": changed["proposal"], "approved": True}).status_code == 409
    called = {}
    def execute(proposal, *, approved, output_directory):
        called["approved"] = approved
        return {"execution_status": "completed_verified", "approval_status": "approved", "final_response": "done", "output_path": None, "integrity_report": {"status": "passed"}}
    monkeypatch.setattr(api, "execute_approved_proposal", execute)
    result = client.post(f"/api/documents/{doc_id}/approve", json={"proposal_token": payload["proposal_token"], "proposal": {k:v for k,v in payload.items() if k != "proposal_token"}, "approved": True})
    assert result.status_code == 200
    assert "output_path" not in result.json()
    assert called["approved"] is True
    replay = client.post(f"/api/documents/{doc_id}/approve", json={"proposal_token": payload["proposal_token"], "proposal": {k:v for k,v in payload.items() if k != "proposal_token"}, "approved": True})
    assert replay.status_code == 409


def test_approval_rejects_modified_server_upload_and_consumes_token(monkeypatch, tmp_path):
    """Mutate the exact API-stored source file after planning, before approval."""
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "outputs")

    class DeterministicProvider:
        def generate(self, messages, schema):
            return {
                "status": "proposed",
                "message": "Replace Draft with Final.",
                "operations": [{
                    "operation_type": "replace",
                    "target_text": "Draft",
                    "replacement_text": "Final",
                }],
            }

    from backend.agent.orchestration import plan_document as real_plan_document

    monkeypatch.setattr(
        api,
        "plan_document",
        lambda path, instruction: real_plan_document(path, instruction, provider=DeterministicProvider()),
    )
    client = _client()
    uploaded = _upload(client).json()
    document_id = uploaded["document_id"]
    server_source = api._documents[document_id]
    original_hash = hashlib.sha256(server_source.read_bytes()).hexdigest()

    planned = client.post(
        f"/api/documents/{document_id}/plan",
        json={"instruction": "Replace Draft with Final"},
    )
    assert planned.status_code == 200
    proposal = planned.json()
    token = proposal.pop("proposal_token")
    registered = api._proposals[token]["proposal"]
    assert registered["document_path"] == str(server_source.resolve())
    assert registered["source_fingerprint"] == original_hash

    # Modify the server's registered upload, not the client's source file.
    changed = Document(server_source)
    changed.paragraphs[0].text = "Status: Draft changed after planning"
    changed.save(server_source)
    assert hashlib.sha256(server_source.read_bytes()).hexdigest() != original_hash

    approval_body = {"proposal_token": token, "proposal": proposal, "approved": True}
    approval = client.post(f"/api/documents/{document_id}/approve", json=approval_body)
    assert approval.status_code == 200
    result = approval.json()
    assert result["execution_status"] == "execution_failed"
    assert result["integrity_status"] == "not_run"
    assert any("Source document changed after planning" in item for item in result["errors"])
    assert result["download_id"] is None
    assert not list((tmp_path / "outputs").glob("*.docx"))

    # The API consumes the token on the first approval attempt, so retrying the
    # original proposal cannot bypass the changed-source check.
    replay = client.post(f"/api/documents/{document_id}/approve", json=approval_body)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "proposal_stale"
    assert not api._downloads
    assert not list((tmp_path / "outputs").glob("*.docx"))


def test_clarification_unsupported_and_reject(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    client = _client()
    doc_id = _upload(client).json()["document_id"]
    monkeypatch.setattr(api, "plan_document", lambda *a: {"planning_status": "needs_clarification", "clarification_request": "Which?", "warnings": []})
    assert client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "unclear"}).json()["clarification_request"] == "Which?"
    monkeypatch.setattr(api, "plan_document", lambda *a: {"planning_status": "unsupported", "warnings": []})
    assert client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "unsupported"}).json()["planning_status"] == "unsupported"


def test_rejection_consumes_proposal_without_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    client = _client()
    doc_id = _upload(client).json()["document_id"]
    proposal = {"planning_status": "awaiting_approval", "document_path": "hidden", "proposal_digest": "opaque-token-which-is-long-enough-123", "proposed_operations": []}
    monkeypatch.setattr(api, "plan_document", lambda *a: proposal.copy())
    response = client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "edit"})
    token = response.json()["proposal_token"]
    called = {}
    def reject_proposal(p, *, approved, output_directory):
        called["approved"] = approved
        return {"execution_status": "rejected", "final_response": "rejected"}
    monkeypatch.setattr(api, "execute_approved_proposal", reject_proposal)
    rejected = client.post(f"/api/documents/{doc_id}/reject", json={"proposal_token": token})
    assert rejected.status_code == 200
    assert called["approved"] is False
    assert client.post(f"/api/documents/{doc_id}/reject", json={"proposal_token": token}).status_code == 409


def test_approval_invalid_token_and_integrity_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    client = _client()
    doc_id = _upload(client).json()["document_id"]
    assert client.post(f"/api/documents/{doc_id}/approve", json={"proposal_token": "x" * 24, "proposal": {}, "approved": True}).status_code == 409
    result = {"execution_status": "integrity_failed", "approval_status": "approved", "integrity_status": "failed",
              "integrity_report": {"status": "failed"}, "output_path": None}
    proposal = {"planning_status": "awaiting_approval", "document_path": "hidden", "proposal_digest": "opaque-token-which-is-long-enough-456", "proposed_operations": []}
    monkeypatch.setattr(api, "plan_document", lambda *a: proposal.copy())
    planned = client.post(f"/api/documents/{doc_id}/plan", json={"instruction": "edit"}).json()
    token = planned.pop("proposal_token")
    monkeypatch.setattr(api, "execute_approved_proposal", lambda *a, **k: result)
    approved = client.post(f"/api/documents/{doc_id}/approve", json={"proposal_token": token, "proposal": planned, "approved": True})
    assert approved.status_code == 200
    assert approved.json()["execution_status"] == "integrity_failed"
    assert approved.json()["integrity_report"]["status"] == "failed"


def test_download_returns_only_registered_output(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "outputs")
    client = _client()
    doc_id = _upload(client).json()["document_id"]
    outdir = api.OUTPUT_DIR
    outdir.mkdir()
    output_path = outdir / "edited.docx"
    Document().save(output_path)
    download_id = "8a8263c0-e82d-4dc9-9cc1-ec249d9ccbc5"
    api._downloads[download_id] = output_path.resolve()
    downloaded = client.get(f"/api/documents/{doc_id}/download", params={"download_id": download_id})
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"PK")
    assert client.get(f"/api/documents/{doc_id}/download", params={"download_id": "../../secret"}).status_code == 404


def test_fastapi_approval_invokes_compiled_graph_and_downloads_verified_output(monkeypatch, tmp_path):
    import backend.agent.orchestration as orchestration

    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "outputs")
    real_plan = orchestration.plan_document

    class Provider:
        def generate(self, messages, schema):
            return {"status": "proposed", "message": "Replace Draft.", "operations": [{
                "operation_type": "replace", "target_text": "Draft", "replacement_text": "Final",
            }]}

    monkeypatch.setattr(api, "plan_document", lambda path, instruction: real_plan(path, instruction, provider=Provider()))
    real_builder = orchestration.build_document_graph
    calls = []

    def spy_builder(directory, **kwargs):
        graph = real_builder(directory, **kwargs)
        class SpyGraph:
            def invoke(self, state):
                calls.append(state.copy())
                return graph.invoke(state)
        return SpyGraph()

    monkeypatch.setattr(orchestration, "build_document_graph", spy_builder)
    client = _client()
    document_id = _upload(client).json()["document_id"]
    planned = client.post(f"/api/documents/{document_id}/plan", json={"instruction": "Replace Draft"}).json()
    token = planned.pop("proposal_token")
    response = client.post(f"/api/documents/{document_id}/approve", json={
        "proposal_token": token, "proposal": planned, "approved": True,
    })
    assert response.status_code == 200
    result = response.json()
    assert calls and calls[0]["proposed_operations"] == planned["proposed_operations"]
    assert result["execution_status"] == "completed_verified"
    assert result["integrity_status"] == "passed"
    assert result["download_id"]
    downloaded = client.get(f"/api/documents/{document_id}/download", params={"download_id": result["download_id"]})
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as package:
        assert b"Final" in package.read("word/document.xml")


def test_api_does_not_register_partially_verified_output(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "outputs")
    client = _client()
    document_id = _upload(client).json()["document_id"]
    output_dir = api._output_dir()
    output = output_dir / "diagnostic.docx"
    Document().save(output)
    proposal = {"planning_status": "awaiting_approval", "document_path": "hidden",
                "proposal_digest": "opaque-token-which-is-long-enough-789", "proposed_operations": []}
    monkeypatch.setattr(api, "plan_document", lambda *a: proposal.copy())
    planned = client.post(f"/api/documents/{document_id}/plan", json={"instruction": "edit"}).json()
    token = planned.pop("proposal_token")
    monkeypatch.setattr(api, "execute_approved_proposal", lambda *a, **k: {
        "execution_status": "completed_partially_verified", "integrity_status": "partially_verified",
        "approval_status": "approved", "output_path": str(output),
    })
    response = client.post(f"/api/documents/{document_id}/approve", json={
        "proposal_token": token, "proposal": planned, "approved": True,
    })
    assert response.status_code == 200
    assert response.json()["download_id"] is None


def test_unknown_and_path_traversal_ids_are_404():
    client = _client()
    assert client.post("/api/documents/../../etc/passwd/plan", json={"instruction": "x"}).status_code == 404
    assert client.get("/api/documents/not-a-uuid/download?download_id=../../etc/passwd").status_code == 404
