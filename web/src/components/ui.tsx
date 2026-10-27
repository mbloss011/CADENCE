import clsx from "clsx";
import { Check, Loader2, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Goal, Idea, MemoryItem, TaskStatus } from "../lib/api";

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

const TASK_TONE: Record<TaskStatus, string> = {
  proposed: "border-violet/40 bg-violet/10 text-violet",
  pending: "border-warn/40 bg-warn/10 text-warn",
  active: "border-brand/50 bg-brand/10 text-brand",
  done: "border-ok/40 bg-ok/10 text-ok",
  failed: "border-bad/40 bg-bad/10 text-bad",
  blocked: "border-flame/40 bg-flame/10 text-flame",
};

export function TaskBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={clsx("chip", TASK_TONE[status])}>
      {status === "active" && <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-current" />}
      {status === "pending" ? "queued" : status === "active" ? "running" : status}
    </span>
  );
}

const GOAL_TONE: Record<Goal["status"], string> = {
  active: "border-brand/50 bg-brand/10 text-brand",
  paused: "border-warn/40 bg-warn/10 text-warn",
  achieved: "border-ok/40 bg-ok/10 text-ok",
  archived: "border-line bg-raised text-faint",
};

export function GoalBadge({ status }: { status: Goal["status"] }) {
  return <span className={clsx("chip", GOAL_TONE[status])}>{status}</span>;
}

export function IdeaSize({ size }: { size: Idea["size"] }) {
  return (
    <span className={clsx("chip", size === "goal" ? "border-violet/40 bg-violet/10 text-violet" : "border-info/40 bg-info/10 text-info")}>
      {size === "goal" ? "goal-sized" : "task-sized"}
    </span>
  );
}

export function Progress({ value, className }: { value: number; className?: string }) {
  return (
    <div className={clsx("h-1.5 overflow-hidden rounded-full bg-raised", className)}>
      <div className="h-full rounded-full bg-gradient-to-r from-brand to-flame transition-all duration-500" style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────── overlays
export function Modal({ open, onClose, title, children, footer, wide }: {
  open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-4 pt-[8vh] backdrop-blur-[2px]" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()} className={clsx("card w-full animate-rise", wide ? "max-w-3xl" : "max-w-lg")} role="dialog">
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <div className="text-[15px] font-semibold">{title}</div>
          <button onClick={onClose} className="btn-ghost px-1.5" aria-label="close"><X size={16} /></button>
        </div>
        <div className="space-y-4 px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[12px] text-faint">{hint}</span>}
    </label>
  );
}

export function Toggle({ on, onChange, disabled, label }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} onClick={() => onChange(!on)}
      className={clsx("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition disabled:opacity-40",
        on ? "border-brand/60 bg-brand/80" : "border-line bg-raised")}>
      <span className={clsx("inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition", on ? "translate-x-[18px]" : "translate-x-[3px]")} />
    </button>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, className }: {
  tabs: { id: T; label: ReactNode; badge?: number }[]; value: T; onChange: (t: T) => void; className?: string;
}) {
  return (
    <div className={clsx("flex gap-1 border-b border-line", className)}>
      {tabs.map((t) => (
        <button key={t.id} onClick={() => onChange(t.id)}
          className={clsx("-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition",
            value === t.id ? "border-brand text-ink" : "border-transparent text-muted hover:text-ink")}>
          {t.label}
          {!!t.badge && <span className="rounded-full bg-brand/15 px-1.5 text-[10px] font-semibold text-brand">{t.badge}</span>}
        </button>
      ))}
    </div>
  );
}

/** Two-step delete: first click arms, second confirms. Avoids blocking `confirm()` dialogs. */
export function ConfirmButton({ onConfirm, label = "Delete", icon = true, className }: { onConfirm: () => void; label?: string; icon?: boolean; className?: string }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button onClick={(e) => { e.stopPropagation(); if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}
      className={clsx(armed ? "btn-danger" : "btn-ghost", "px-2", className)} title={label}>
      {icon && <Trash2 size={14} />}{armed ? "Confirm" : icon ? null : label}
    </button>
  );
}

/** Monospace editor: Tab indents, Ctrl/Cmd+S saves, with a line-number gutter. */
export function CodeEditor({ value, onChange, onSave, className, placeholder, readOnly, minRows = 12 }: {
  value: string; onChange: (v: string) => void; onSave?: () => void; className?: string; placeholder?: string; readOnly?: boolean; minRows?: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const lines = Math.max(minRows, value.split("\n").length);
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      onSave?.();
    } else if (e.key === "Tab" && !readOnly) {
      e.preventDefault();
      const el = e.currentTarget;
      const { selectionStart: a, selectionEnd: b } = el;
      const next = value.slice(0, a) + "  " + value.slice(b);
      onChange(next);
      requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = a + 2; });
    }
  };
  return (
    <div className={clsx("flex overflow-hidden rounded-lg border border-line bg-bg font-mono text-[12.5px] leading-[1.6] focus-within:border-brand/50", className)}>
      <div ref={gutter} className="select-none overflow-hidden border-r border-line/70 bg-panel/50 px-2 py-2.5 text-right text-faint" aria-hidden>
        {Array.from({ length: lines }, (_, i) => <div key={i}>{i + 1}</div>)}
      </div>
      <textarea ref={ref} value={value} readOnly={readOnly} spellCheck={false} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} onKeyDown={onKey}
        onScroll={(e) => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop; }}
        className="min-h-0 flex-1 resize-none whitespace-pre bg-transparent px-3 py-2.5 text-ink outline-none placeholder:text-faint" />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────── toasts
type Toast = { id: number; text: string; tone: "ok" | "bad" | "info" };
let pushToast: (t: Omit<Toast, "id">) => void = () => undefined;

export function toast(text: string, tone: Toast["tone"] = "ok") {
  pushToast({ text, tone });
}

/** Run an async action, toasting its error (and optionally a success message). */
export async function act<T>(fn: () => Promise<T>, ok?: string): Promise<T | undefined> {
  try {
    const r = await fn();
    if (ok) toast(ok);
    return r;
  } catch (e) {
    toast((e as Error).message.slice(0, 300), "bad");
    return undefined;
  }
}

export function Toaster() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(() => {
    let n = 0;
    pushToast = (t) => {
      const id = ++n;
      setItems((x) => [...x.slice(-3), { ...t, id }]);
      setTimeout(() => setItems((x) => x.filter((y) => y.id !== id)), t.tone === "bad" ? 7000 : 3000);
    };
  }, []);
  return createPortal(
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2">
      {items.map((t) => (
        <div key={t.id} className={clsx("pointer-events-auto card flex animate-rise items-start gap-2 px-3 py-2.5 text-[13px]",
          t.tone === "bad" && "border-bad/40", t.tone === "ok" && "border-ok/40")}>
          {t.tone === "bad" ? <X size={15} className="mt-px shrink-0 text-bad" /> : <Check size={15} className="mt-px shrink-0 text-ok" />}
          <span className="min-w-0 flex-1 break-words">{t.text}</span>
        </div>
      ))}
    </div>,
    document.body,
  );
}

/** Fetch-on-mount helper with a reload function and a loading flag. */
export function useFetch<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | undefined; loading: boolean; error: string; reload: () => void } {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const reload = useCallback(() => {
    const n = ++seq.current;
    setLoading(true);
    fn().then((d) => { if (n === seq.current) { setData(d); setError(""); } })
      .catch((e) => { if (n === seq.current) setError((e as Error).message); })
      .finally(() => { if (n === seq.current) setLoading(false); });
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(reload, [reload]);
  return { data, loading, error, reload };
}

export function Busy({ on, children }: { on: boolean; children: ReactNode }) {
  return <>{on ? <Loader2 size={14} className="animate-spin" /> : null}{children}</>;
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
