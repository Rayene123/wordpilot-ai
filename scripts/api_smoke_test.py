"""Synchronous HTTP end-to-end smoke test against a running WordPilot API.

Uses only a temporary synthetic DOCX. It never imports or calls orchestration
functions and never changes provider configuration or falls back between them.
Run after starting the API: python scripts/api_smoke_test.py
"""
from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path

import httpx
from docx import Document

BASE_URL = "http://127.0.0.1:8000"
EXPECTED_OPERATION = {
    "operation_type": "replace",
    "target_text": "Draft",
    "replacement_text": "Final",
}
INSTRUCTION = "Replace the word 'Draft' with 'Final' in the project status."


def report(name: str, passed: bool, detail: str = "") -> None:
    print(f"{'PASS' if passed else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""), flush=True)


def _http_error(response: httpx.Response) -> str:
    """Show only the API's structured error, never request/document contents."""
    try:
        body = response.json()
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            code, message = detail.get("code"), detail.get("message")
            return f"HTTP {response.status_code}" + (f" [{code}]" if code else "") + (f": {message}" if message else "")
        if isinstance(detail, str):
            return f"HTTP {response.status_code}: {detail}"
    except (ValueError, AttributeError):
        pass
    return f"HTTP {response.status_code}; response body was not a structured API error"


def main() -> int:
    document_id: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="wordpilot-api-smoke-") as tmp:
            folder = Path(tmp)
            original = folder / "synthetic.docx"
            downloaded_file = folder / "downloaded.docx"
            source_doc = Document()
            source_doc.add_paragraph("Project status: Draft")
            source_doc.save(original)
            original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            report("Create synthetic DOCX", True)

            with httpx.Client(base_url=BASE_URL, timeout=150.0) as client:
                health = client.get("/api/health")
                if health.status_code != 200:
                    raise RuntimeError(f"Health endpoint: {_http_error(health)}")
                config = health.json()
                if config.get("provider") != "groq":
                    raise RuntimeError(f"Expected configured provider 'groq', got {config.get('provider')!r}; no fallback attempted.")
                report("Health and Groq provider", True, f"model={config.get('model')}")

                with original.open("rb") as source:
                    upload = client.post("/api/documents/upload", files={
                        "file": ("synthetic.docx", source, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                    })
                if upload.status_code != 200:
                    raise RuntimeError(f"Upload failed: {_http_error(upload)}")
                document_id = upload.json().get("document_id")
                uuid.UUID(document_id)
                report("Upload synthetic DOCX", True, f"document_id={document_id}")

                planning = client.post(f"/api/documents/{document_id}/plan", json={"instruction": INSTRUCTION})
                if planning.status_code != 200:
                    raise RuntimeError(f"Planning failed: {_http_error(planning)}")
                proposal = planning.json()
                operations = proposal.get("proposed_operations")
                if proposal.get("planning_status") != "awaiting_approval" or operations != [EXPECTED_OPERATION]:
                    raise RuntimeError(f"Unexpected planning result: status={proposal.get('planning_status')!r}, operations={operations!r}")
                if not proposal.get("proposal_token"):
                    raise RuntimeError("Planning response did not include the proposal token required for approval.")
                report("Plan and inspect proposed operation", True, "replace Draft → Final")

                if hashlib.sha256(original.read_bytes()).hexdigest() != original_hash:
                    raise RuntimeError("Original changed during planning.")
                report("Original unchanged before approval", True)

                token = proposal.pop("proposal_token")
                approval = client.post(f"/api/documents/{document_id}/approve", json={
                    "proposal_token": token,
                    "proposal": proposal,
                    "approved": True,
                })
                if approval.status_code != 200:
                    raise RuntimeError(f"Explicit approval failed: {_http_error(approval)}")
                result = approval.json()
                report("Explicit approval sent through API", result.get("approval_status") == "approved")
                integrity = result.get("integrity_report") or {}
                if result.get("execution_status") != "completed_verified" or integrity.get("status") != "passed":
                    raise RuntimeError(f"Execution/integrity did not pass: execution={result.get('execution_status')!r}, integrity={integrity.get('status')!r}")
                report("Execution and integrity verification", True, "completed_verified; integrity=passed")

                download_id = result.get("download_id")
                if not download_id:
                    raise RuntimeError("API did not return a safe download ID.")
                response = client.get(f"/api/documents/{document_id}/download", params={"download_id": download_id})
                if response.status_code != 200:
                    raise RuntimeError(f"Download failed: {_http_error(response)}")
                downloaded_file.write_bytes(response.content)
                checked_doc = Document(downloaded_file)
                text = "\n".join(paragraph.text for paragraph in checked_doc.paragraphs)
                if "Project status: Final" not in text:
                    raise RuntimeError("Downloaded DOCX does not contain the expected replacement.")
                report("Download and verify edited DOCX", True, "Project status: Final")

                unchanged = hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
                if not unchanged:
                    raise RuntimeError("Original changed after approval.")
                report("Original unchanged after approval", True)

                invalid_upload = client.post("/api/documents/upload", files={"file": ("invalid.docx", b"not a DOCX", "application/octet-stream")})
                unknown_doc = client.post(f"/api/documents/{uuid.uuid4()}/plan", json={"instruction": INSTRUCTION})
                unknown_download = client.get(f"/api/documents/{document_id}/download", params={"download_id": str(uuid.uuid4())})
                negatives_pass = invalid_upload.status_code == 400 and unknown_doc.status_code == 404 and unknown_download.status_code == 404
                report("Negative HTTP checks", negatives_pass, "invalid upload, unknown document and download IDs rejected")
                if not negatives_pass:
                    return 1
                return 0
    except (httpx.HTTPError, OSError, ValueError, RuntimeError, KeyError) as exc:
        report("Smoke workflow", False, str(exc))
        return 1
    finally:
        # Temporary source and downloaded files are removed by TemporaryDirectory.
        # The API currently has no HTTP cleanup endpoint for its server-side upload/output files.
        if document_id:
            print("INFO | Server-side uploaded/edited files remain in API storage; the API exposes no cleanup endpoint.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
