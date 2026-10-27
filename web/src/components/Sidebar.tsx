import clsx from "clsx";
import { Brain, Gauge, MessageSquare, Moon, Plus, Sun, Target, Trash2 } from "lucide-react";
import { useStore } from "../lib/store";
import { Logo } from "../views/ChatView";
import { Dot, ago } from "./ui";

export type View = "chat" | "goals" | "memory" | "system";

const NAV: { id: View; label: string; icon: typeof Target }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "goals", label: "Goals", icon: Target },
  { id: "memory", label: "Memory", icon: Brain },
  { id: "system", label: "System", icon: Gauge },
];

export function Sidebar({
  view, setView, active, onOpen, onNew, theme, toggleTheme, goalBadge,
}: {
  view: View;
  setView: (v: View) => void;
  active: string | null;
  onOpen: (sid: string) => void;
  onNew: () => void;
  theme: "dark" | "light";
  toggleTheme: () => void;
  goalBadge: number;
}) {
  const store = useStore();
  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-line bg-panel/60">
      <div className="flex h-14 items-center gap-2.5 px-4">
        <Logo className="h-7 w-7" />
        <div className="flex-1">
          <div className="text-[15px] font-semibold tracking-tight">Kestrel</div>
        </div>
        <span title={store.connected ? "connected" : "reconnecting…"}>
          <Dot tone={store.connected ? "ok" : "bad"} />
        </span>
      </div>

      <div className="px-3">
        <button onClick={onNew} className="btn-outline w-full justify-start">
          <Plus size={15} /> New chat
        </button>
      </div>

      <nav className="mt-3 space-y-0.5 px-3">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setView(id)}
            className={clsx(
              "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition",
              view === id ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink",
            )}
          >
            <Icon size={15} className={view === id ? "text-brand" : ""} />
            <span className="flex-1 text-left">{label}</span>
            {id === "goals" && goalBadge > 0 && (
              <span className="rounded-full bg-violet/15 px-1.5 text-[10px] font-semibold text-violet">{goalBadge}</span>
            )}
          </button>
        ))}
      </nav>

      <div className="label mt-5 px-5">Recent</div>
      <div className="flex-1 space-y-px overflow-y-auto px-3 pb-3">
        {store.sessions.length === 0 && <p className="px-2.5 py-2 text-[13px] text-faint">No conversations yet.</p>}
        {store.sessions.map((s) => (
          <div
            key={s.id}
            className={clsx(
              "group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition",
              view === "chat" && active === s.id ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink",
            )}
            onClick={() => onOpen(s.id)}
          >
            {s.running ? <Dot tone="live" /> : s.kind === "goal" ? <Target size={13} className="shrink-0 text-faint" /> : null}
            <div className="min-w-0 flex-1">
              <div className="truncate">{s.title}</div>
              <div className="text-[10.5px] text-faint">{s.kind === "goal" ? "goal run · " : ""}{ago(s.updated)}</div>
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation();
                if (confirm(`Delete "${s.title}"?`)) void store.remove(s.id);
              }}
              className="hidden rounded p-1 text-faint hover:bg-bad/10 hover:text-bad group-hover:block"
              aria-label="delete conversation"
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[11px] text-faint">
        <span>local · private</span>
        <button onClick={toggleTheme} className="btn-ghost px-1.5 py-1" title="Toggle theme">
          {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
    </aside>
  );
}
