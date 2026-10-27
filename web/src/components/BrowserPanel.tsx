import clsx from "clsx";
import { ArrowLeft, ArrowRight, Bot, Globe, Hand, Loader2, MousePointer2, Power, RotateCw } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent, type MouseEvent, type WheelEvent } from "react";
import { socket } from "../lib/socket";
import { useLive, useStore } from "../lib/store";
import { toast } from "./ui";

type BState = { started: boolean; url: string; title?: string; width: number; height: number; driver: string };

const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

export function BrowserPanel() {
  const { project } = useStore();
  const [state, setState] = useState<BState>({ started: false, url: "", width: 1280, height: 800, driver: "" });
  const [frame, setFrame] = useState("");
  const [url, setUrl] = useState("");
  const [editingUrl, setEditingUrl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [focused, setFocused] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const wheel = useRef({ dy: 0, t: 0 as unknown as ReturnType<typeof setTimeout> | 0 });

  useEffect(() => {
    socket.send({ op: "browser_watch", project, on: true });
    return () => socket.send({ op: "browser_watch", project, on: false });
  }, [project]);

  useLive(["browser_frame", "browser_state", "browser_error"], (m) => {
    if (m.project !== project) return;
    if (m.kind === "browser_frame") {
      setFrame(`data:image/jpeg;base64,${m.data as string}`);
      setBusy(false);
    } else if (m.kind === "browser_state") {
      setState((s) => ({ ...s, ...(m as unknown as BState) }));
      if (!editingUrl && typeof m.url === "string") setUrl(m.url as string);
      if (!m.started) setFrame("");
    } else {
      setBusy(false);
      toast(m.message as string, "bad");
    }
  });

  const send = (action: string, extra: Record<string, unknown> = {}) => {
    socket.send({ op: "browser", project, action, ...extra });
  };

  const go = (target = url) => {
    if (!target.trim()) return;
    setBusy(true);
    setEditingUrl(false);
    send("goto", { url: target.trim() });
  };

  const point = (e: MouseEvent<HTMLImageElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * state.width, y: ((e.clientY - r.top) / r.height) * state.height };
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!state.started || (e.target as HTMLElement).tagName === "INPUT") return;
    if (e.metaKey || e.ctrlKey) {
      if (e.key.toLowerCase() === "a") { e.preventDefault(); send("key", { key: "ControlOrMeta+A" }); }
      return; // leave copy/paste shortcuts to the browser (paste is handled below)
    }
    if (KEYS.has(e.key)) {
      e.preventDefault();
      send("key", { key: e.key });
    } else if (e.key.length === 1) {
      e.preventDefault();
      send("type", { text: e.key });
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).tagName === "INPUT") return;
    const text = e.clipboardData.getData("text");
    if (text && state.started) {
      e.preventDefault();
      send("type", { text });
    }
  };

  const onWheel = (e: WheelEvent<HTMLDivElement>) => {
    if (!state.started) return;
    wheel.current.dy += e.deltaY;
    if (!wheel.current.t) {
      wheel.current.t = setTimeout(() => {
        send("scroll", { dy: wheel.current.dy });
        wheel.current = { dy: 0, t: 0 };
      }, 80);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col" tabIndex={0} onKeyDown={onKey} onPaste={onPaste}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}>
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-2 py-1.5">
        <button onClick={() => send("back")} disabled={!state.started} className="btn-ghost px-1.5" title="Back"><ArrowLeft size={15} /></button>
        <button onClick={() => send("forward")} disabled={!state.started} className="btn-ghost px-1.5" title="Forward"><ArrowRight size={15} /></button>
        <button onClick={() => send("reload")} disabled={!state.started} className="btn-ghost px-1.5" title="Reload"><RotateCw size={14} /></button>
        <form className="relative min-w-0 flex-1" onSubmit={(e) => { e.preventDefault(); go(); }}>
          <Globe size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input value={url} onChange={(e) => { setUrl(e.target.value); setEditingUrl(true); }} onBlur={() => setEditingUrl(false)}
            placeholder="Search or enter a URL" className="input h-8 py-0 pl-7 font-mono text-[12px]" />
        </form>
        {state.started && <button onClick={() => send("close")} className="btn-ghost px-1.5" title="Close the browser"><Power size={14} /></button>}
      </div>

      <div className="flex shrink-0 items-center gap-2 border-b border-line/60 px-3 py-1 text-[11px] text-faint">
        {state.driver === "agent" ? <span className="flex items-center gap-1 text-brand"><Bot size={12} /> agent is driving</span>
          : state.driver === "user" ? <span className="flex items-center gap-1"><Hand size={12} /> you are driving</span>
          : <span>shared with the agent</span>}
        <span className="min-w-0 flex-1 truncate text-right">{state.title}</span>
        {busy && <Loader2 size={12} className="animate-spin" />}
      </div>

      <div className={clsx("relative min-h-0 flex-1 overflow-hidden bg-raised/40", focused && state.started && "ring-1 ring-inset ring-brand/40")} onWheel={onWheel}>
        {frame ? (
          <img ref={imgRef} src={frame} alt="browser" draggable={false}
            onClick={(e) => send("click", point(e))}
            className="absolute inset-0 m-auto max-h-full max-w-full cursor-pointer select-none object-contain" />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-raised text-muted">
              {busy ? <Loader2 size={18} className="animate-spin" /> : <Globe size={18} />}
            </div>
            <div className="text-sm font-medium">{busy ? "Starting the browser…" : "Shared browser"}</div>
            <p className="max-w-xs text-[13px] text-muted">
              Open a page here, or ask the agent to browse. You both drive the same browser: log in or solve a captcha here, then tell the agent to carry on.
            </p>
            <div className="flex flex-wrap justify-center gap-1.5">
              {["duckduckgo.com", "github.com", "localhost:3000"].map((u) => (
                <button key={u} onClick={() => { setUrl(u); go(u); }} className="btn-outline px-2.5 py-1 font-mono text-[11.5px]">{u}</button>
              ))}
            </div>
          </div>
        )}
      </div>
      {state.started && (
        <div className="flex shrink-0 items-center gap-1.5 border-t border-line/60 px-3 py-1 text-[10.5px] text-faint">
          <MousePointer2 size={11} /> click to interact · {focused ? "typing goes to the page" : "click the panel to type"} · scroll to scroll
        </div>
      )}
    </div>
  );
}
