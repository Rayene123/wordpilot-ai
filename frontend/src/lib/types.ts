export type ParagraphInfo = {
  text: string;
  style: string | null;
  alignment: string | null;
  runs: Array<{
    text: string;
    bold: boolean | null;
    italic: boolean | null;
    underline: boolean | null;
    font_name: string | null;
    font_size_pt: number | null;
  }>;
};

export type DocumentSummary = {
  paragraphs: ParagraphInfo[];
  tables: Array<{ rows: string[][][] }>;
  paragraph_count: number;
  table_count: number;
  inline_image_count: number;
};

export type ReplaceOperation = {
  operation_type: "replace";
  target_text: string;
  replacement_text: string;
};

export type InsertOperation = {
  operation_type: "insert";
  anchor_paragraph_text: string;
  inserted_text: string;
  position: "before" | "after";
  paragraph_style: string | null;
  location: number[] | null;
};

export type RemoveOperation = {
  operation_type: "remove";
  target_text: string;
  location: number[] | null;
};

export type DocumentOperation = ReplaceOperation | InsertOperation | RemoveOperation;

export type PlanningStatus = "awaiting_approval" | "needs_clarification" | "unsupported";

export type PublicProposal = {
  planning_status: PlanningStatus;
  approval_status: "pending";
  execution_status: "not_executed";
  user_instruction: string;
  document_summary: DocumentSummary;
  proposed_operations: DocumentOperation[];
  validated_operations: DocumentOperation[];
  approved_operations: DocumentOperation[];
  clarification_request: string | null;
  execution_results: unknown[];
  warnings: string[];
  errors: string[];
  final_response: string;
  proposal_version: number;
};

export type PlanResponse = PublicProposal & { proposal_token?: string };

export type ProposalSnapshot = Readonly<{
  token: string;
  proposal: Readonly<PublicProposal>;
}>;

export type IntegrityStatus = "passed" | "partially_verified" | "failed" | "not_run";

export type IntegrityReport = {
  status: Exclude<IntegrityStatus, "not_run">;
  checks: Array<{ name: string; status: "passed" | "failed" | "partially_verified"; details: string[] }>;
  unexpected_changes: string[];
  expected_changes: string[];
  unverified_features: string[];
  errors: string[];
  summary: string;
};

export type ExecutionResponse = {
  planning_status: string;
  approval_status: string;
  execution_status: string;
  integrity_status: IntegrityStatus;
  integrity_report: IntegrityReport | null;
  user_instruction: string | null;
  proposed_operations: DocumentOperation[];
  validated_operations: DocumentOperation[];
  approved_operations: DocumentOperation[];
  execution_results: unknown[];
  warnings: string[];
  errors: string[];
  final_response: string | null;
  download_id: string | null;
};

export type UploadResponse = {
  document_id: string;
  filename: string;
  inspection: DocumentSummary;
};

export type HealthResponse = { status: string; provider: string; model: string };

export type WorkflowPhase =
  | "idle"
  | "uploading"
  | "ready"
  | "planning"
  | "awaiting_approval"
  | "executing"
  | "verified"
  | "partially_verified"
  | "failed";
