"""HTTP API around WordPilot's existing planning and approval workflow."""

from __future__ import annotations

import hmac
import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.agent.orchestration import execute_approved_proposal, plan_document
from backend.agent.provider import get_planning_configuration
from backend.docx_editor.inspection import inspect_document

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "generated-documents"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
_lock = RLock()
_documents: dict[str, Path] = {}
_proposals: dict[str, dict[str, Any]] = {}
_downloads: dict[str, Path] = {}

app = FastAPI(title="WordPilot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class PlanRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=10_000)


class ApprovalRequest(BaseModel):
    proposal_token: str = Field(min_length=20, max_length=200)
    proposal: dict[str, Any]
    approved: bool


class RejectRequest(BaseModel):
    proposal_token: str = Field(min_length=20, max_length=200)


def _doc(document_id: str) -> Path:
    try:
        uuid.UUID(document_id)
    except (ValueError, TypeError):
        raise HTTPException(404, detail={"code": "document_not_found", "message": "Document not found."}) from None
    with _lock:
        path = _documents.get(document_id)
    if path is None or not path.is_file():
        raise HTTPException(404, detail={"code": "document_not_found", "message": "Document not found."})
    return path


def _public_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    # Internal absolute paths and the orchestration digest never leave this API.
    return {k: v for k, v in proposal.items() if k not in {"document_path", "output_path", "proposal_digest", "source_fingerprint"}}


def _redact_local_paths(value: Any) -> Any:
    """Avoid returning machine-specific paths embedded in execution messages."""
    if isinstance(value, str):
        return value.replace(str(ROOT), "[server storage]")
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_local_paths(item) for key, item in value.items()}
    return value


def _fail(status: int, code: str, message: str) -> None:
    raise HTTPException(status, detail={"code": code, "message": message})


@app.get("/api/health")
def health() -> dict[str, str]:
    provider_name, model_name = get_planning_configuration()
    return {"status": "ok", "provider": provider_name, "model": model_name}


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename or Path(file.filename).suffix.lower() != ".docx":
        _fail(415, "unsupported_format", "Upload a .docx document.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    doc_id = str(uuid.uuid4())
    destination = UPLOAD_DIR / f"{doc_id}.docx"
    size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=UPLOAD_DIR, suffix=".docx", delete=False) as temp:
            temp_path = Path(temp.name)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    _fail(413, "upload_too_large", "DOCX upload exceeds the 15 MiB limit.")
                temp.write(chunk)
        if not zipfile.is_zipfile(temp_path):
            _fail(400, "invalid_docx", "File is not a valid DOCX package.")
        with zipfile.ZipFile(temp_path) as package:
            names = set(package.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names or package.testzip():
                _fail(400, "invalid_docx", "File is not a valid DOCX package.")
        summary = inspect_document(temp_path).to_dict()
        os.replace(temp_path, destination)
        with _lock:
            _documents[doc_id] = destination
        return {"document_id": doc_id, "filename": Path(file.filename).name, "inspection": summary}
    except HTTPException:
        raise
    except Exception as exc:
        # Return only the exception type; avoid exposing local paths or file contents.
        _fail(400, "invalid_docx", f"Could not read this DOCX document ({type(exc).__name__}).")
    finally:
        await file.close()
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)


@app.post("/api/documents/{document_id}/plan")
def plan(document_id: str, request: PlanRequest) -> dict[str, Any]:
    path = _doc(document_id)
    result = plan_document(path, request.instruction)
    if result.get("planning_status") == "failed":
        _fail(502, "planning_failed", "The planner could not produce a proposal.")
    if result.get("planning_status") == "awaiting_approval":
        token = result.get("proposal_digest")
        if not token:
            _fail(502, "planning_failed", "The planner returned an invalid proposal.")
        with _lock:
            _proposals[token] = {"document_id": document_id, "proposal": result, "state": "pending"}
        result = {**_public_proposal(result), "proposal_token": token}
    else:
        result = _public_proposal(result)
    return result


@app.post("/api/documents/{document_id}/approve")
def approve(document_id: str, request: ApprovalRequest) -> dict[str, Any]:
    _doc(document_id)
    with _lock:
        entry = _proposals.get(request.proposal_token)
        if not entry or entry["document_id"] != document_id or entry["state"] != "pending":
            _fail(409, "proposal_stale", "Proposal is unknown, stale, or already used.")
        expected = _public_proposal(entry["proposal"])
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
        if not hmac.compare_digest(canonical(expected), canonical(request.proposal)):
            _fail(409, "proposal_modified", "Proposal content does not match the generated proposal.")
        entry["state"] = "consumed"
        proposal = entry["proposal"]
    result = execute_approved_proposal(proposal, approved=request.approved, output_directory=_output_dir())
    output_id = None
    if result.get("output_path"):
        output = Path(result["output_path"]).resolve()
        if output.parent == OUTPUT_DIR.resolve() and output.is_file():
            output_id = str(uuid.uuid4())
            with _lock:
                _downloads[output_id] = output
    safe = _redact_local_paths({k: v for k, v in result.items() if k not in {"document_path", "output_path"}})
    safe["download_id"] = output_id
    return safe


def _output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


@app.post("/api/documents/{document_id}/reject")
def reject(document_id: str, request: RejectRequest) -> dict[str, str]:
    _doc(document_id)
    with _lock:
        entry = _proposals.get(request.proposal_token)
        if not entry or entry["document_id"] != document_id or entry["state"] != "pending":
            _fail(409, "proposal_stale", "Proposal is unknown, stale, or already used.")
        entry["state"] = "consumed"
        proposal = entry["proposal"]
    result = execute_approved_proposal(proposal, approved=False, output_directory=_output_dir())
    return {"status": result["execution_status"], "message": result["final_response"]}


@app.get("/api/documents/{document_id}/download")
def download(document_id: str, download_id: str) -> FileResponse:
    _doc(document_id)
    try:
        uuid.UUID(download_id)
    except (ValueError, TypeError):
        raise HTTPException(404, detail={"code": "download_not_found", "message": "Download not found."}) from None
    with _lock:
        path = _downloads.get(download_id)
    if path is None or not path.is_file() or path.parent != OUTPUT_DIR.resolve():
        _fail(404, "download_not_found", "Download not found.")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename="wordpilot-edited.docx")
