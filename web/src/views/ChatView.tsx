import clsx from "clsx";
import { ArrowDown, CheckCircle2, Circle, CircleDot, Globe, ListChecks, NotebookPen, Sparkles, Wrench, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { BrowserPanel } from "../components/BrowserPanel";
import { CanvasPanel } from "../components/CanvasPanel";
import { Composer } from "../components/Composer";
import { MessageItem } from "../components/Messages";
import { ContextMeter, Dot, Spinner } from "../components/ui";
import type { Status } from "../lib/api";
import { useStore, type Attachment, type DockTab } from "../lib/store";

const STARTERS = [
  { icon: Wrench, text: "Run the test suite and fix anything that fails" },
  { icon: NotebookPen, text: "Write a design doc for this project on the canvas" },
  { icon: Globe, text: "Open the docs for the main library we use and summarize what changed recently" },
  { icon: Sparkles, text: "Explain how this codebase is structured" },
];

const DOCK: { id: DockTab; label: string; icon: typeof Globe }[] = [
  { id: "plan", label: "Plan", icon: ListChecks },
  { id: "canvas", label: "Canvas", icon: NotebookPen },
  { id: "browser", label: "Browser", icon: Globe },
];

function useDockWidth() {
  const [w, setW] = useState(() => {
    try {
      return Number(localStorage.getItem("kestrel.dockw")) || 520;
    } catch {
      return 520;
    }
  });
  const start = (e: PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const x0 = e.clientX;
    const w0 = w;
    const move = (ev: globalThis.PointerEvent) => setW(Math.max(320, Math.min(window.innerWidth * 0.7, w0 + (x0 - ev.clientX))));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setW((cur) => {
        try {
          localStorage.setItem("kestrel.dockw", String(Math.round(cur)));
        } catch {
          /* storage unavailable */
        }
        return cur;
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return { width: w, start };
}

export function ChatView({ sid, status, onNew }: { sid: string | null; status: Status | null; onNew: (text?: string) => void }) {
  const store = useStore();
  const chat = sid ? store.chats[sid] : undefined;
  const scroller = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const dock = store.dock.tab;
  const { width, start } = useDockWidth();

  useEffect(() => {
    if (sid && !store.chats[sid]?.loaded) store.loadChat(sid).catch(() => undefined);
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned) el.scrollTop = el.scrollHeight;
  }, [chat?.items, pinned]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  const send = useCallback(
    (text: string, uploads: Attachment[]) => {
      if (!sid) return;
      setPinned(true);
      store.send(sid, text, uploads);
    },
    [sid, store],
  );
  const stop = useCallback(() => sid && store.interrupt(sid), [sid, store]);

  const title = store.sessions.find((s) => s.id === sid)?.title ?? "New chat";
  const items = chat?.items ?? [];
  const running = !!chat?.running;
  const waiting = running && (items.length === 0 || items[items.length - 1].kind === "user" || items[items.length - 1].kind === "steer");

  return (
    <div className="flex h-full min-w-0 flex-1">
      <div className="flex h-full min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line px-5">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{sid ? title : "Cadence"}</div>
            {status && (
              <div className="flex items-center gap-1.5 truncate text-[11px] text-faint">
                <span className="font-mono">{status.models.big.name}</span>
                <span>·</span>
                <span className="truncate font-mono">{status.workspace.replace(/^\/home\/[^/]+/, "~")}</span>
              </div>
            )}
          </div>
          {running && (
            <span className="chip border-brand/40 bg-brand/10 text-brand">
              <Dot tone="live" /> working{chat?.usage?.iteration ? ` · step ${chat.usage.iteration}` : ""}
            </span>
          )}
          {chat?.usage && <ContextMeter tokens={chat.usage.tokens} window={chat.usage.window} compact />}
          <div className="hidden items-center rounded-lg border border-line p-0.5 lg:flex">
            {DOCK.map(({ id, label, icon: Icon }) => (
              <button key={id} onClick={() => store.setDock({ tab: dock === id ? null : id })} title={label}
                className={clsx("flex items-center gap-1 rounded-md px-2 py-1 text-[12px] transition", dock === id ? "bg-raised text-ink" : "text-faint hover:text-ink")}>
                <Icon size={14} className={dock === id ? "text-brand" : ""} />
                <span className="hidden xl:inline">{label}</span>
              </button>
            ))}
          </div>
        </header>

        <div ref={scroller} onScroll={onScroll} className="relative flex-1 overflow-y-auto">
          {items.length === 0 && !running ? (
            <Welcome status={status} onPick={(t) => (sid ? send(t, []) : onNew(t))} />
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
              {items.map((it, i) => (
                <MessageItem key={i} item={it} />
              ))}
              {waiting && (
                <div className="flex items-center gap-2 text-sm text-faint">
                  <Spinner className="text-brand" /> {status?.models.big.name ?? "model"} is thinking…
                </div>
              )}
            </div>
          )}
          {!pinned && (
            <button
              onClick={() => setPinned(true)}
              className="sticky bottom-3 left-1/2 mx-auto flex -translate-x-1/2 items-center gap-1 rounded-full border border-line bg-panel px-3 py-1 text-xs text-muted shadow-card hover:text-ink"
            >
              <ArrowDown size={12} /> Latest
            </button>
          )}
        </div>

        <Composer running={running} disabled={!store.connected} onSend={(t, u) => (sid ? send(t, u) : onNew(t))} onStop={stop} />
      </div>

      {dock && (
        <aside className="relative hidden shrink-0 flex-col border-l border-line bg-panel/40 lg:flex" style={{ width: dock === "plan" ? 288 : width }}>
          {dock !== "plan" && <div onPointerDown={start} className="absolute -left-1 top-0 z-10 h-full w-2 cursor-col-resize hover:bg-brand/20" />}
          <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-2">
            {DOCK.map(({ id, label }) => (
              <button key={id} onClick={() => store.setDock({ tab: id })}
                className={clsx("rounded-md px-2.5 py-1 text-[12.5px]", dock === id ? "bg-raised text-ink" : "text-muted hover:text-ink")}>{label}</button>
            ))}
            <button onClick={() => store.setDock({ tab: null })} className="btn-ghost ml-auto px-1.5" aria-label="close panel"><X size={14} /></button>
          </div>
          <div className="min-h-0 flex-1">
            {dock === "plan" && <PlanPanel sid={sid} status={status} />}
            {dock === "canvas" && <CanvasPanel />}
            {dock === "browser" && <BrowserPanel />}
          </div>
        </aside>
      )}
    </div>
  );
}

function PlanPanel({ sid, status }: { sid: string | null; status: Status | null }) {
  const store = useStore();
  const chat = sid ? store.chats[sid] : undefined;
  const todos = chat?.todos ?? [];
  return (
    <div className="flex h-full flex-col gap-5 overflow-y-auto p-4">
      <section>
        <div className="label flex items-center gap-1.5"><ListChecks size={12} /> Task list</div>
        {todos.length === 0 ? (
          <p className="text-[13px] text-faint">The agent writes a plan here for multi-step work.</p>
        ) : (
          <ul className="space-y-1.5">
            {todos.map((t, i) => (
              <li key={i} className="flex items-start gap-2 text-[13px]">
                {t.status === "done" ? <CheckCircle2 size={15} className="mt-px shrink-0 text-ok" />
                  : t.status === "in_progress" ? <CircleDot size={15} className="mt-px shrink-0 animate-pulse text-brand" />
                  : <Circle size={15} className="mt-px shrink-0 text-faint" />}
                <span className={clsx(t.status === "done" && "text-faint line-through")}>{t.content}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <div className="label">Context</div>
        {chat?.usage ? <ContextMeter tokens={chat.usage.tokens} window={chat.usage.window} /> : <p className="text-[13px] text-faint">—</p>}
        <p className="mt-1.5 text-[12px] text-faint">
          The small model compacts automatically at 70%{chat?.compactions ? ` · ${chat.compactions} compaction(s) so far` : ""}.
        </p>
      </section>
      {status && (
        <section className="space-y-1.5 text-[12.5px]">
          <div className="label">Session</div>
          <Row k="big" v={status.models.big.name} ok={status.models.big.ok} />
          <Row k="small" v={status.models.small.name} ok={status.models.small.ok} />
          <Row k="vision" v={status.vision} ok />
          <Row k="sandbox" v={status.sandbox.mode + (status.sandbox.network ? "" : " · offline")} ok={status.sandbox.mode !== "none" && !status.sandbox.problem} warn={status.sandbox.mode === "none"} />
          <Row k="memory" v={`${status.memory.backend} · ${status.memory.total}`} ok={!status.memory.error} />
          {sid && <div className="pt-1 font-mono text-[11px] text-faint">session {sid}</div>}
        </section>
      )}
    </div>
  );
}

function Row({ k, v, ok, warn }: { k: string; v: string; ok?: boolean; warn?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <Dot tone={warn ? "warn" : ok ? "ok" : "bad"} />
      <span className="w-14 text-faint">{k}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-muted">{v}</span>
    </div>
  );
}

function Welcome({ status, onPick }: { status: Status | null; onPick: (t: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-6 py-12">
      <Logo className="mb-3 h-[140px] w-[140px]" />
      <div className="wordmark mb-4 text-3xl font-bold tracking-tight">Cadence</div>
      <h2 className="text-xl font-semibold tracking-tight">What should we build?</h2>
      <p className="mt-2 text-center text-sm text-muted">
        Cadence reads, edits and runs code in <span className="font-mono text-ink">{status?.workspace.replace(/^\/home\/[^/]+/, "~") ?? "your project"}</span>.
        Steer it mid-task, interrupt with Esc, drop in a screenshot, or work together on the canvas and in the browser.
      </p>
      <div className="mt-8 grid w-full gap-2 sm:grid-cols-2">
        {STARTERS.map(({ icon: Icon, text }) => (
          <button key={text} onClick={() => onPick(text)} className="card flex items-start gap-3 p-3.5 text-left text-sm text-muted transition hover:border-faint hover:text-ink">
            <Icon size={16} className="mt-0.5 shrink-0 text-brand" />
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}

export function Logo({ className }: { className?: string }) {
  return <img src="/cadence-mark.png" alt="Cadence" className={clsx("brand-glow object-contain", className)} draggable={false} />;
}
