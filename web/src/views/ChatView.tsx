import clsx from "clsx";
import { ArrowDown, CheckCircle2, Circle, CircleDot, Image as ImageIcon, ListChecks, PanelRightClose, PanelRightOpen, Sparkles, Target, Wrench } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Composer } from "../components/Composer";
import { MessageItem } from "../components/Messages";
import { ContextMeter, Dot, Spinner } from "../components/ui";
import type { Status } from "../lib/api";
import { useStore } from "../lib/store";

const STARTERS = [
  { icon: Wrench, text: "Run the test suite and fix anything that fails" },
  { icon: Target, text: "Find the riskiest function in this repo and add tests for it" },
  { icon: ImageIcon, text: "Look at the attached screenshot and tell me what's wrong" },
  { icon: Sparkles, text: "Explain how this codebase is structured" },
];

export function ChatView({ sid, status, onNew }: { sid: string | null; status: Status | null; onNew: (text?: string) => void }) {
  const store = useStore();
  const chat = sid ? store.chats[sid] : undefined;
  const scroller = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const [rail, setRail] = useState(() => localStorage.getItem("kestrel.rail") !== "0");

  useEffect(() => {
    if (sid && !store.chats[sid]?.loaded) store.loadChat(sid).catch(() => undefined);
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    try {
      localStorage.setItem("kestrel.rail", rail ? "1" : "0");
    } catch {
      /* storage unavailable */
    }
  }, [rail]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned) el.scrollTop = el.scrollHeight;
  }, [chat?.items, pinned]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  const send = useCallback(
    (text: string, uploads: { id: string; preview: string }[]) => {
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
  const todos = chat?.todos ?? [];

  return (
    <div className="flex h-full min-w-0 flex-1">
      <div className="flex h-full min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line px-5">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{sid ? title : "Kestrel"}</div>
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
          <button onClick={() => setRail((r) => !r)} className="btn-ghost hidden px-2 lg:inline-flex" title="Toggle side panel">
            {rail ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
          </button>
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

      {rail && sid && (
        <aside className="hidden w-72 shrink-0 flex-col gap-5 overflow-y-auto border-l border-line bg-panel/40 p-4 lg:flex">
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
              <Row k="sandbox" v={status.sandbox.mode + (status.sandbox.network ? "" : " · offline")} ok={status.sandbox.mode !== "none"} warn={status.sandbox.mode === "none"} />
              <Row k="memory" v={`${status.memory.backend} · ${status.memory.total}`} ok={!status.memory.error} />
              <div className="pt-1 font-mono text-[11px] text-faint">session {sid}</div>
            </section>
          )}
        </aside>
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
      <Logo className="mb-5 h-12 w-12" />
      <h2 className="text-2xl font-semibold tracking-tight">What should we build?</h2>
      <p className="mt-2 text-center text-sm text-muted">
        Kestrel reads, edits and runs code in <span className="font-mono text-ink">{status?.workspace.split("/").pop() ?? "your workspace"}</span>.
        Steer it mid-task, interrupt with Esc, or drop in a screenshot.
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
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <defs>
        <linearGradient id="kg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="rgb(var(--brand))" />
          <stop offset="1" stopColor="rgb(var(--flame))" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="url(#kg)" />
      <path d="M7 21.5 16 8l3.2 6.4L25 11l-6.6 12.5-3-5.2L7 21.5Z" fill="#111" opacity=".9" />
    </svg>
  );
}
