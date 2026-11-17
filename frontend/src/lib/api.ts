import type {
  ExecutionResponse,
  HealthResponse,
  PlanResponse,
  PublicProposal,
  UploadResponse,
} from "./types";

const API_BASE = (process.env.NEXT_PUBLIC_WORDPILOT_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  let message = `Request failed (${response.status}).`;
  let code: string | undefined;
  try {
    const body = await response.json();
    if (typeof body.detail === "object" && body.detail !== null) {
      message = body.detail.message ?? message;
      code = body.detail.code;
    } else if (typeof body.detail === "string") {
      message = body.detail;
    } else if (Array.isArray(body.detail)) {
      message = body.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join(" ") || message;
    }
  } catch {
    // Keep the generic status message when the response is not JSON.
  }
  throw new ApiError(message, response.status, code);
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return parseResponse(await fetch(`${API_BASE}/api/health`, { signal, cache: "no-store" }));
}

export async function uploadDocument(file: File, signal?: AbortSignal): Promise<UploadResponse> {
  const body = new FormData();
  body.append("file", file);
  return parseResponse(await fetch(`${API_BASE}/api/documents/upload`, { method: "POST", body, signal }));
}

export async function planDocument(documentId: string, instruction: string, signal?: AbortSignal): Promise<PlanResponse> {
  return parseResponse(await fetch(`${API_BASE}/api/documents/${encodeURIComponent(documentId)}/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
    signal,
  }));
}

export async function decideProposal(
  documentId: string,
  token: string,
  proposal: PublicProposal,
  approved: boolean,
  signal?: AbortSignal,
): Promise<ExecutionResponse> {
  const response = approved
    ? await fetch(`${API_BASE}/api/documents/${encodeURIComponent(documentId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_token: token, proposal, approved: true }),
        signal,
      })
    : await fetch(`${API_BASE}/api/documents/${encodeURIComponent(documentId)}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_token: token }),
        signal,
      });
  if (!approved) {
    if (!response.ok) return parseResponse(response);
    const result = await response.json();
    return {
      planning_status: "awaiting_approval", approval_status: "rejected",
      execution_status: result.status, integrity_status: "not_run", integrity_report: null,
      user_instruction: proposal.user_instruction, proposed_operations: proposal.proposed_operations,
      validated_operations: [], approved_operations: [], execution_results: [], warnings: [], errors: [],
      final_response: result.message, download_id: null,
    };
  }
  return parseResponse(response);
}

export async function downloadDocument(documentId: string, downloadId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/api/documents/${encodeURIComponent(documentId)}/download?download_id=${encodeURIComponent(downloadId)}`,
    { signal },
  );
  if (!response.ok) await parseResponse(response);
  return response.blob();
}
