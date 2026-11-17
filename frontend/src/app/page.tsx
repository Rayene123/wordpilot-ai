"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle, AlertTriangle, Check, ChevronRight, CircleHelp,
  CloudUpload, Download, FileCheck2, FileText, Info, LockKeyhole,
  RefreshCw, ShieldAlert, ShieldCheck, Sparkles, X,
} from "lucide-react";
import { ApiError, decideProposal, downloadDocument, getHealth, planDocument, uploadDocument } from "@/lib/api";
import type { ExecutionResponse, HealthResponse, IntegrityStatus, ProposalSnapshot, UploadResponse, WorkflowPhase } from "@/lib/types";
import { OperationCard } from "@/components/operation-card";
import { WorkflowRail } from "@/components/workflow-rail";

function immutable<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    Object.values(value as Record<string, unknown>).forEach((item) => immutable(item));
  }
  return value;
}

function statusLabel(phase: WorkflowPhase): string {
  const labels: Record<WorkflowPhase, string> = {
    idle: "Ready when you are", uploading: "Uploading document", ready: "Ready to plan",
    planning: "Preparing proposal", awaiting_approval: "Review required", executing: "Applying approved changes",
    verified: "Verified", partially_verified: "Partially verified", failed: "Action needed",
  };
  return labels[phase];
}

function IntegrityResult({ execution, onDownload, downloading }: {
  execution: ExecutionResponse;
  onDownload: () => void;
  downloading: boolean;
}) {
  const status: IntegrityStatus = execution.integrity_status;
  const passed = status === "passed" && execution.execution_status === "completed_verified";
  const partial = status === "partially_verified" && execution.execution_status === "completed_partially_verified";
  const failed = status === "failed" || execution.execution_status === "integrity_failed" || execution.execution_status === "execution_failed";
  const klass = passed ? "passed" : partial ? "partial" : "failed";
  const title = passed ? "Integrity verified" : partial ? "Integrity partially verified" : failed ? "Integrity check failed" : status === "passed" ? "Execution did not complete as verified" : "Verification was not completed";
  const report = execution.integrity_report;
  const canDownload = Boolean(execution.download_id && passed);
  return (
    <section className="panel result-panel" aria-live="polite" aria-label="Execution and integrity result">
      <div className="result-head">
        <span className={`result-icon ${klass}`}>{passed ? <ShieldCheck size={18} /> : partial ? <ShieldAlert size={18} /> : <AlertCircle size={18} />}</span>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 9, alignItems: "center" }}>
            <h2 className="result-title">{title}</h2>
            <span className={`status-pill ${passed ? "status-success" : partial ? "status-partial" : "status-failed"}`}>{status}</span>
          </div>
          <p className="result-message">{execution.final_response ?? "The operation finished without a verification summary."}</p>
        </div>
      </div>
      <div className="result-details">
        {report && <>
          <p className="helper" style={{ margin: "0 0 9px" }}>{report.summary}</p>
          {(report.checks.length > 0 || report.errors.length > 0 || report.unverified_features.length > 0) && <ul className="check-list">
            {report.checks.map((check) => <li key={check.name}>{check.status === "passed" ? <Check size={12} color="#267251" /> : check.status === "partially_verified" ? <Info size={12} color="#9a6a19" /> : <X size={12} color="#a23b35" />}<span><b>{check.name}</b>{check.details.length > 0 ? ` — ${check.details.join("; ")}` : ""}</span></li>)}
            {report.unverified_features.map((item) => <li key={item}><Info size={12} color="#9a6a19" /><span>Not fully checked: {item}</span></li>)}
            {report.errors.map((item, index) => <li key={`${index}-${item}`}><AlertCircle size={12} color="#a23b35" /><span>{item}</span></li>)}
          </ul>}
        </>}
        {partial && <div className="alert alert-warning" style={{ marginBottom: 12 }}><AlertTriangle size={15} /><p><strong>Review before relying on this file.</strong>The backend reports incomplete verification. No verified download is offered.</p></div>}
        {failed && <div className="alert alert-error" style={{ marginBottom: 12 }}><AlertCircle size={15} /><p><strong>Do not treat this output as verified.</strong>Download is disabled because the backend did not report a passing integrity check.</p></div>}
        <div className="result-actions">
          {canDownload ? <button className="button button-primary" onClick={onDownload} disabled={downloading}>
            {downloading ? <span className="spinner" /> : <Download size={15} />}{downloading ? "Preparing download…" : "Download verified DOCX"}
          </button> : <span className="helper">Download is available only after a passed integrity check.</span>}
        </div>
      </div>
    </section>
  );
}

export default function WorkspacePage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [connected, setConnected] = useState(false);
  const [phase, setPhase] = useState<WorkflowPhase>("idle");
  const [document, setDocument] = useState<UploadResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [instruction, setInstruction] = useState("");
  const [snapshot, setSnapshot] = useState<ProposalSnapshot | null>(null);
  const [execution, setExecution] = useState<ExecutionResponse | null>(null);
  const [message, setMessage] = useState<{ kind: "info" | "warning" | "success"; title: string; body: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const busy = ["uploading", "planning", "executing"].includes(phase);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then((value) => { setHealth(value); setConnected(true); }).catch(() => setConnected(false));
    return () => controller.abort();
  }, []);

  const clearFeedback = () => { setError(null); setMessage(null); };

  const upload = useCallback(async (file: File) => {
    clearFeedback();
    setSelectedFile(file);
    if (!file.name.toLowerCase().endsWith(".docx")) {
      setError("Choose a Word document ending in .docx."); setPhase(document ? "ready" : "idle"); return;
    }
    if (file.size > 15 * 1024 * 1024) {
      setError("This file exceeds the 15 MiB upload limit."); setPhase(document ? "ready" : "idle"); return;
    }
    setPhase("uploading"); setSnapshot(null); setExecution(null);
    try {
      const result = await uploadDocument(file);
      setDocument(result); setPhase("ready");
      setMessage({ kind: "success", title: "Document ready", body: "The DOCX was inspected. It has not been changed." });
    } catch (cause) {
      setPhase(document ? "ready" : "failed");
      setError(cause instanceof Error ? cause.message : "The document could not be uploaded.");
    }
  }, [document]);

  const plan = useCallback(async () => {
    if (!document || !instruction.trim() || busy || phase === "awaiting_approval") return;
    clearFeedback(); setExecution(null); setSnapshot(null); setPhase("planning");
    try {
      const result = await planDocument(document.document_id, instruction.trim());
      if (result.planning_status === "awaiting_approval") {
        if (!result.proposal_token) throw new Error("The planner did not return an approval token. No edits were made.");
        const { proposal_token, ...proposal } = result;
        const frozen = immutable(proposal);
        setSnapshot(Object.freeze({ token: proposal_token, proposal: frozen }));
        setPhase("awaiting_approval");
      } else {
        setPhase("ready");
        setMessage({
          kind: result.planning_status === "needs_clarification" ? "warning" : "info",
          title: result.planning_status === "needs_clarification" ? "More detail needed" : "Request not supported",
          body: result.clarification_request ?? result.final_response,
        });
      }
    } catch (cause) {
      setPhase("failed");
      setError(cause instanceof ApiError && cause.status === 502 ? `${cause.message} Check the configured provider and try again.` : cause instanceof Error ? cause.message : "Planning failed. Try again.");
    }
  }, [busy, document, instruction, phase]);

  const decide = async (approved: boolean) => {
    if (!document || !snapshot || busy) return;
    clearFeedback(); setPhase(approved ? "executing" : "executing");
    try {
      const result = await decideProposal(document.document_id, snapshot.token, snapshot.proposal, approved);
      setSnapshot(null);
      if (!approved) {
        setExecution(null);
        setPhase("ready");
        setMessage({ kind: "info", title: "Proposal rejected", body: result.final_response ?? "No document changes were executed." });
      } else {
        setExecution(result);
        if (result.integrity_status === "passed" && result.execution_status === "completed_verified") {
          setPhase("verified");
        } else if (result.integrity_status === "partially_verified" && result.execution_status === "completed_partially_verified") {
          setPhase("partially_verified");
        } else {
          setPhase("failed");
        }
      }
    } catch (cause) {
      setSnapshot(null);
      setPhase("failed"); setError(cause instanceof Error ? `${cause.message} Prepare a new proposal before deciding again.` : "The decision could not be completed. Prepare a new proposal before deciding again.");
    }
  };

  const download = async () => {
    if (!document || !execution?.download_id || execution.integrity_status !== "passed" || execution.execution_status !== "completed_verified" || downloadBusy) return;
    setDownloadBusy(true); clearFeedback();
    try {
      const blob = await downloadDocument(document.document_id, execution.download_id);
      const href = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = href; anchor.download = `${document.filename.replace(/\.docx$/i, "")}-verified.docx`;
      anchor.click(); URL.revokeObjectURL(href);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The verified document could not be downloaded.");
    } finally { setDownloadBusy(false); }
  };

  const retry = () => {
    if (selectedFile && (!document || phase === "failed" && error?.toLowerCase().includes("upload"))) void upload(selectedFile);
    else if (document && instruction.trim()) void plan();
  };

  return (
    <div className="workspace-shell">
      <header className="topbar">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">W</span><span className="brand-name">WordPilot</span><span className="brand-caption">Document workspace</span></div>
        <div className="topbar-meta"><span className="connection"><i className={`connection-dot${connected ? " online" : ""}`} />{connected ? "API connected" : "API not checked"}</span><span className={`status-pill ${phase === "verified" ? "status-success" : phase === "partially_verified" ? "status-partial" : phase === "failed" ? "status-failed" : "status-pending"}`} aria-live="polite">{statusLabel(phase)}</span><span className="topbar-model"><ShieldCheck size={14} /> Human approval required</span></div>
      </header>

      <main className="main-wrap">
        <div className="page-heading">
          <div><div className="eyebrow">Controlled document workflow</div><h1 className="page-title">Review every change before it reaches your document.</h1><p className="page-subtitle">Describe an edit in plain language. WordPilot prepares a structured proposal for your review, then verifies the approved output.</p></div>
          <div className="privacy-note"><LockKeyhole size={15} /><span>{health?.provider === "groq" ? "Planning uses the selected Groq API." : health?.provider === "ollama" ? "Planning uses your configured local Ollama service." : "Provider details appear when the backend responds."}</span></div>
        </div>

        <div className="workspace-grid">
          <div className="main-column">
            <section className="panel panel-pad" aria-labelledby="document-heading">
              <div className="panel-head"><div><div className="step-label"><span className="step-number">1</span> DOCUMENT</div><h2 id="document-heading" className="panel-title" style={{ marginTop: 9 }}>Select a Word document</h2><p className="panel-description">Only .docx files are supported. Your original remains unchanged.</p></div>{document && <span className="status-pill status-success"><Check size={12} /> Inspected</span>}</div>
              {!document ? <div className="upload-zone">
                <div><CloudUpload className="upload-icon" size={25} /><p className="upload-title">{phase === "uploading" ? "Uploading and inspecting…" : "Choose a document to begin"}</p><p className="upload-hint">DOCX format · up to 15 MiB</p>
                  <input ref={fileInput} type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" className="sr-only" aria-label="Choose a DOCX file" disabled={busy} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = ""; }} />
                  <button type="button" className="button button-secondary" onClick={() => fileInput.current?.click()} disabled={busy}>{phase === "uploading" ? <span className="spinner" /> : <FileText size={14} />}{phase === "uploading" ? "Please wait" : "Browse files"}</button>
                </div>
              </div> : <>
                <div className="file-row"><div className="file-info"><span className="file-badge"><FileCheck2 size={17} /></span><div style={{ minWidth: 0 }}><div className="file-name">{document.filename}</div><div className="file-meta">Inspected · original preserved</div></div></div><button type="button" className="button button-quiet" onClick={() => { setDocument(null); setSelectedFile(null); setSnapshot(null); setExecution(null); setMessage(null); setError(null); setPhase("idle"); }} disabled={busy}>Change file</button></div>
                <div className="summary-stats"><span><FileText size={13} />{document.inspection.paragraph_count} paragraphs</span><span>{document.inspection.table_count} tables</span><span>{document.inspection.inline_image_count} inline images</span></div>
                <div className="document-preview"><div className="preview-head">READ-ONLY DOCUMENT OUTLINE</div><div className="preview-body">{document.inspection.paragraphs.length ? document.inspection.paragraphs.slice(0, 9).map((paragraph, index) => <div className="preview-line" key={`${index}-${paragraph.text}`}><span className="line-no">{String(index + 1).padStart(2, "0")}</span><span className="line-text">{paragraph.text || <em>Empty paragraph</em>}</span></div>) : <div className="preview-empty">No body paragraphs were returned in the inspection summary.</div>}{document.inspection.paragraph_count > 9 && <div className="preview-empty">+ {document.inspection.paragraph_count - 9} more paragraphs</div>}</div></div>
              </>}
            </section>

            <section className="panel panel-pad" aria-labelledby="instruction-heading">
              <div className="panel-head"><div><div className="step-label"><span className="step-number">2</span> INSTRUCTION</div><h2 id="instruction-heading" className="panel-title" style={{ marginTop: 9 }}>What would you like to change?</h2><p className="panel-description">Be specific about the text or paragraph to edit.</p></div><Sparkles size={17} color="var(--accent)" /></div>
              <label className="sr-only" htmlFor="instruction">Editing instruction</label>
              <textarea id="instruction" className="instruction-box" value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder={'For example: Replace “Draft” with “Final” in the project status.'} maxLength={10_000} disabled={!document || busy || Boolean(snapshot)} />
              <div className="form-footer"><span className="helper">Your instruction and document inspection are sent to the configured planning provider.</span><button className="button button-primary" type="button" onClick={() => void plan()} disabled={!document || !instruction.trim() || busy || Boolean(snapshot)}>{phase === "planning" ? <span className="spinner" /> : <Sparkles size={14} />}{phase === "planning" ? "Preparing proposal…" : "Prepare proposal"}<ChevronRight size={14} /></button></div>
            </section>

            {message && <div className={`alert ${message.kind === "warning" ? "alert-warning" : message.kind === "success" ? "alert-success" : "alert-info"}`} role="status"><Info size={16} /><p><strong>{message.title}</strong>{message.body}</p><button className="button button-quiet" onClick={() => setMessage(null)} aria-label="Dismiss message"><X size={14} /></button></div>}
            {error && <div className="alert alert-error" role="alert"><AlertCircle size={16} /><p style={{ flex: 1 }}><strong>We couldn’t complete that step</strong>{error}</p><button className="button button-secondary" onClick={retry} disabled={busy}><RefreshCw size={13} />Retry</button></div>}

            {snapshot && <section className="panel proposal-panel" aria-labelledby="proposal-heading">
              <div className="proposal-head"><div><div className="proposal-title-line"><span className="step-number">3</span><h2 id="proposal-heading" className="panel-title">Review proposed changes</h2></div><p className="proposal-subtitle">Nothing has been edited. Confirm each operation before continuing.</p></div><span className="status-pill status-pending"><CircleHelp size={12} /> Awaiting your decision</span></div>
              <div className="operation-list">{snapshot.proposal.proposed_operations.map((operation, index) => <OperationCard operation={operation} index={index} key={`${index}-${operation.operation_type}`} />)}</div>
              {snapshot.proposal.warnings.length > 0 && <div style={{ padding: "0 20px 14px" }}><div className="alert alert-warning"><AlertTriangle size={15} /><p><strong>Planner notes</strong>{snapshot.proposal.warnings.join(" ")}</p></div></div>}
              <div className="approval-footer"><button className="button button-danger" onClick={() => void decide(false)} disabled={busy}><X size={14} />Reject proposal</button><button className="button button-primary" onClick={() => void decide(true)} disabled={busy}><Check size={14} />Approve and apply</button></div>
            </section>}

            {execution && <IntegrityResult execution={execution} onDownload={() => void download()} downloading={downloadBusy} />}
          </div>

          <WorkflowRail phase={phase} provider={health?.provider} model={health?.model} connected={connected} />
        </div>
        <p className="footer-note">WordPilot prepares proposals; you decide what is applied. Original documents are not overwritten.</p>
      </main>
    </div>
  );
}
