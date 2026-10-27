import clsx from "clsx";
import type { ReactNode } from "react";
import type { Goal, MemoryItem } from "../lib/api";

export function Spinner({ className }: { className?: string }) {
  return (
    <span className={clsx("inline-flex items-center gap-0.5", className)} aria-label="working">
      {[0, 1, 2].map((i) => (
        <span key={i} className="h-1 w-1 rounded-full bg-current animate-pulseDot" style={{ animationDelay: `${i * 160}ms` }} />
      ))}
    </span>
  );
}

export function Dot({ tone }: { tone: "ok" | "bad" | "warn" | "idle" | "live" }) {
  return (
    <span className="relative inline-flex h-2 w-2">
      {tone === "live" && <span className="absolute inset-0 animate-ping rounded-full bg-brand/60" />}
      <span
        className={clsx("relative h-2 w-2 rounded-full", {
          "bg-ok": tone === "ok",
          "bg-bad": tone === "bad",
          "bg-warn": tone === "warn",
          "bg-faint": tone === "idle",
          "bg-brand": tone === "live",
        })}
      />
    </span>
  );
}

const GOAL_TONE: Record<Goal["status"], string> = {
  proposed: "border-violet/40 bg-violet/10 text-violet",
  pending: "border-warn/40 bg-warn/10 text-warn",
  active: "border-brand/50 bg-brand/10 text-brand",
  done: "border-ok/40 bg-ok/10 text-ok",
  failed: "border-bad/40 bg-bad/10 text-bad",
  blocked: "border-flame/40 bg-flame/10 text-flame",
};

export function GoalBadge({ status }: { status: Goal["status"] }) {
  return (
    <span className={clsx("chip", GOAL_TONE[status])}>
      {status === "active" && <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-current" />}
      {status}
    </span>
  );
}

const KIND_TONE: Record<MemoryItem["kind"], string> = {
  semantic: "border-info/40 bg-info/10 text-info",
  procedural: "border-ok/40 bg-ok/10 text-ok",
  episodic: "border-violet/40 bg-violet/10 text-violet",
};

export function KindChip({ kind }: { kind: MemoryItem["kind"] }) {
  return <span className={clsx("chip", KIND_TONE[kind])}>{kind}</span>;
}

export function ContextMeter({ tokens, window, compact }: { tokens: number; window: number; compact?: boolean }) {
  const frac = Math.min(1, tokens / Math.max(1, window));
  const tone = frac < 0.5 ? "bg-ok" : frac < 0.7 ? "bg-warn" : "bg-bad";
  return (
    <div className={clsx("flex items-center gap-2", compact ? "text-[11px]" : "text-xs")} title="Context window usage — compaction starts at 70%">
      <div className={clsx("relative overflow-hidden rounded-full bg-raised", compact ? "h-1.5 w-20" : "h-2 w-full")}>
        <div className={clsx("absolute inset-y-0 left-0 rounded-full transition-all", tone)} style={{ width: `${frac * 100}%` }} />
        <div className="absolute inset-y-0 w-px bg-faint/70" style={{ left: "70%" }} />
      </div>
      <span className="whitespace-nowrap font-mono text-muted">
        {(tokens / 1000).toFixed(1)}k / {(window / 1000).toFixed(0)}k
      </span>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
      <div className="mb-1 flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-raised text-muted">{icon}</div>
      <div className="text-sm font-medium text-ink">{title}</div>
      {children && <div className="max-w-sm text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border border-line bg-raised px-1 py-px font-mono text-[10px] text-muted">{children}</kbd>;
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 border-b border-line px-6 py-5">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
