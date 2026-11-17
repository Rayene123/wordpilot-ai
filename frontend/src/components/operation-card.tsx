import { ArrowLeftRight, Minus, Plus } from "lucide-react";
import type { DocumentOperation } from "@/lib/types";

export function OperationCard({ operation, index }: { operation: DocumentOperation; index: number }) {
  return (
    <article className="operation-card">
      <div className="operation-top">
        <span className="operation-kind">
          {operation.operation_type === "replace" ? <ArrowLeftRight size={14} /> : operation.operation_type === "insert" ? <Plus size={14} /> : <Minus size={14} />}
          {operation.operation_type === "replace" ? "Text replacement" : operation.operation_type === "insert" ? "Paragraph insertion" : "Paragraph removal"}
        </span>
        <span className="operation-index">Change {String(index + 1).padStart(2, "0")}</span>
      </div>
      <div className="operation-content">
        {operation.operation_type === "replace" && (
          <div className="diff-grid">
            <div className="diff-cell"><div className="diff-label">Current text</div><div className="diff-text">{operation.target_text}</div></div>
            <div className="diff-cell new"><div className="diff-label">Proposed text</div><div className="diff-text">{operation.replacement_text || <em>Empty text</em>}</div></div>
          </div>
        )}
        {operation.operation_type === "insert" && (
          <>
            <div className="context-label">Insert {operation.position === "before" ? "before" : "after"}</div>
            <div className="context-box">{operation.anchor_paragraph_text}</div>
            <div className="inserted-content"><div className="diff-label">New paragraph</div><div className="diff-text">{operation.inserted_text || <em>Empty paragraph</em>}</div></div>
          </>
        )}
        {operation.operation_type === "remove" && (
          <div className="diff-cell"><div className="diff-label">Paragraph to be removed</div><div className="diff-text">{operation.target_text}</div></div>
        )}
        {"location" in operation && operation.location && <div className="helper" style={{ marginTop: 9 }}>Document location: {operation.location.join(" · ")}</div>}
      </div>
    </article>
  );
}
