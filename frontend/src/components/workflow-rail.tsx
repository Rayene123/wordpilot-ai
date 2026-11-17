import { Check, Circle, FileText, ScanSearch, ShieldCheck, Sparkles, ThumbsUp } from "lucide-react";
import type { WorkflowPhase } from "@/lib/types";

const steps = [
  { title: "Document", icon: FileText },
  { title: "Instruction", icon: Sparkles },
  { title: "Review proposal", icon: ScanSearch },
  { title: "Approval", icon: ThumbsUp },
  { title: "Integrity check", icon: ShieldCheck },
] as const;

export function WorkflowRail({ phase, provider, model, connected }: { phase: WorkflowPhase; provider?: string; model?: string; connected: boolean }) {
  const current = phase === "idle" || phase === "failed" ? -1 : phase === "uploading" || phase === "ready" ? 0 : phase === "planning" ? 1 : phase === "awaiting_approval" ? 2 : phase === "executing" ? 3 : 4;
  return (
    <aside className="panel workflow-panel" aria-label="Workflow progress">
      <div className="workflow-top">
        <div className="panel-head" style={{ marginBottom: 0 }}><div><h2 className="panel-title">Workflow</h2><p className="panel-description">Each change remains under your control.</p></div></div>
      </div>
      <div className="workflow-list">
        {steps.map((step, index) => {
          const Icon = step.icon;
          const done = index < current;
          const active = index === current;
          return <div className={`workflow-item${active ? " current" : ""}${done ? " done" : ""}`} key={step.title} aria-current={active ? "step" : undefined}>
            <span className="workflow-icon">{done ? <Check size={13} /> : active ? <Icon size={13} /> : <Circle size={9} />}</span>
            <span className="workflow-copy"><strong>{step.title}</strong><small>{index === 0 ? "Upload and inspect" : index === 1 ? "Describe the edit" : index === 2 ? "Check every operation" : index === 3 ? "Approve or reject" : "Review the result"}</small></span>
          </div>;
        })}
      </div>
      <div className="provider-note">
        <span className="connection"><i className={`connection-dot${connected ? " online" : ""}`} />{connected ? "Backend available" : "Backend not checked"}</span>
        <strong>{provider ? `${provider[0].toUpperCase()}${provider.slice(1)} planning` : "Planning provider"}</strong>
        <span>{model ?? "Provider details are unavailable"}</span>
      </div>
    </aside>
  );
}
