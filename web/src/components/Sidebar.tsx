import clsx from "clsx";
import {
  Archive, ArchiveRestore, Brain, Check, ChevronsUpDown, FolderOpen, Lightbulb, ListTodo, MessageSquare, Moon, Pencil, Plus, Puzzle, Settings, Sun, Target, Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type Project } from "../lib/api";
import { useStore } from "../lib/store";
import { Logo } from "../views/ChatView";
import { ProjectDialog, ProjectMark } from "./ProjectDialog";
import { act, Dot, ago } from "./ui";

export type View = "chat" | "files" | "goals" | "tasks" | "ideas" | "memory" | "extensions" | "settings";
export const VIEWS: View[] = ["chat", "files", "goals", "tasks", "ideas", "memory", "extensions", "settings"];

const NAV: { id: View; label: string; icon: typeof Target; group: 0 | 1 | 2 }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare, group: 0 },
  { id: "files", label: "Files", icon: FolderOpen, group: 0 },
  { id: "goals", label: "Goals", icon: Target, group: 1 },
  { id: "tasks", label: "Tasks", icon: ListTodo, group: 1 },
  { id: "ideas", label: "Ideas", icon: Lightbulb, group: 1 },
  { id: "memory", label: "Memory", icon: Brain, group: 2 },
  { id: "extensions", label: "Extensions", icon: Puzzle, group: 2 },
  { id: "settings", label: "Settings", icon: Settings, group: 2 },
];

export function Sidebar({
  view, setView, active, onOpen, onNew, theme, toggleTheme, badges,
}: {
  view: View;
  setView: (v: View) => void;
  active: string | null;
  onOpen: (sid: string) => void;
  onNew: () => void;
  theme: "dark" | "light";
  toggleTheme: () => void;
  badges: Partial<Record<View, number>>;
}) {
  const store = useStore();
  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-line bg-panel/60">
      <div className="flex h-14 items-center gap-2.5 px-4">
        <Logo className="h-7 w-7" />
        <div className="flex-1 text-[15px] font-semibold tracking-tight">Kestrel</div>
        <span title={store.connected ? "connected" : "reconnecting…"}>
          <Dot tone={store.connected ? "ok" : "bad"} />
        </span>
      </div>

      <div className="px-3">
        <ProjectSwitcher />
      </div>

      <div className="mt-2 px-3">
        <button onClick={onNew} className="btn-outline w-full justify-start">
          <Plus size={15} /> New chat
        </button>
      </div>

      <nav className="mt-3 px-3">
        {NAV.map(({ id, label, icon: Icon, group }, i) => (
          <div key={id}>
            {i > 0 && NAV[i - 1].group !== group && <div className="my-1.5 border-t border-line/60" />}
            <button
              onClick={() => setView(id)}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition",
                view === id ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink",
              )}
            >
              <Icon size={15} className={view === id ? "text-brand" : ""} />
              <span className="flex-1 text-left">{label}</span>
              {!!badges[id] && <span className="rounded-full bg-violet/15 px-1.5 text-[10px] font-semibold text-violet">{badges[id]}</span>}
            </button>
          </div>
        ))}
      </nav>

      <div className="label mt-4 px-5">Recent in {store.current?.name ?? "project"}</div>
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
            {s.running ? <Dot tone="live" /> : s.kind === "task" ? <ListTodo size={13} className="shrink-0 text-faint" /> : null}
            <div className="min-w-0 flex-1">
              <div className="truncate">{s.title}</div>
              <div className="text-[10.5px] text-faint">{s.kind === "task" ? "task run · " : ""}{ago(s.updated)}</div>
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

function ProjectSwitcher() {
  const store = useStore();
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<{ open: boolean; project?: Project }>({ open: false });
  const [showArchived, setShowArchived] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const cur = store.current;
  const active = store.projects.filter((p) => !p.archived);
  const archived = store.projects.filter((p) => p.archived);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2.5 rounded-lg border border-line bg-bg/60 px-2.5 py-2 text-left transition hover:border-faint">
        <ProjectMark project={cur} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium">{cur?.name ?? "Loading…"}</div>
          <div className="truncate font-mono text-[10.5px] text-faint">{cur?.path.replace(/^\/home\/[^/]+/, "~") ?? ""}</div>
        </div>
        <ChevronsUpDown size={14} className="shrink-0 text-faint" />
      </button>
      {open && (
        <div className="card absolute left-0 right-0 top-full z-40 mt-1 animate-rise overflow-hidden p-1">
          <div className="label px-2 pt-1">Projects</div>
          <div className="max-h-80 overflow-y-auto">
            {[...active, ...(showArchived ? archived : [])].map((p) => (
              <div key={p.id} onClick={() => { store.setProject(p.id); setOpen(false); }}
                className={clsx("group flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] hover:bg-raised", p.id === store.project && "bg-raised/70")}>
                <ProjectMark project={p} size={18} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 truncate">
                    <span className={clsx("truncate", p.archived && "text-faint")}>{p.name}</span>
                    {p.archived && <span className="chip border-line text-faint">archived</span>}
                    {!p.exists && <span className="chip border-bad/40 text-bad">missing</span>}
                  </div>
                  <div className="flex gap-2 text-[10.5px] text-faint">
                    {p.counts.goals > 0 && <span>{p.counts.goals} goal{p.counts.goals > 1 ? "s" : ""}</span>}
                    {p.counts.tasks > 0 && <span>{p.counts.tasks} task{p.counts.tasks > 1 ? "s" : ""}</span>}
                    {p.counts.ideas > 0 && <span>{p.counts.ideas} idea{p.counts.ideas > 1 ? "s" : ""}</span>}
                  </div>
                </div>
                {p.id === store.project && <Check size={14} className="text-brand" />}
                {!p.default && (
                  <button onClick={async (e) => {
                    e.stopPropagation();
                    if (await act(() => api.editProject(p.id, { archived: !p.archived }), p.archived ? `Restored “${p.name}”` : `Archived “${p.name}”`)) {
                      await store.refreshProjects();
                      if (!p.archived && p.id === store.project) store.setProject(store.projects.find((x) => x.default)?.id ?? "");
                    }
                  }} className="hidden rounded p-1 text-faint hover:text-ink group-hover:block" aria-label={p.archived ? "restore project" : "archive project"}
                    title={p.archived ? "Restore" : "Archive: hide it and stop background work"}>
                    {p.archived ? <ArchiveRestore size={12} /> : <Archive size={12} />}
                  </button>
                )}
                <button onClick={(e) => { e.stopPropagation(); setOpen(false); setDialog({ open: true, project: p }); }}
                  className="hidden rounded p-1 text-faint hover:text-ink group-hover:block" aria-label="edit project">
                  <Pencil size={12} />
                </button>
              </div>
            ))}
          </div>
          {archived.length > 0 && (
            <button onClick={() => setShowArchived((v) => !v)} className="flex w-full items-center gap-1.5 px-2 py-1.5 text-[11.5px] text-faint hover:text-muted">
              <Archive size={12} /> {showArchived ? "Hide" : "Show"} archived ({archived.length})
            </button>
          )}
          <button onClick={() => { setOpen(false); setDialog({ open: true }); }} className="mt-1 flex w-full items-center gap-2 rounded-md border-t border-line px-2 py-2 text-[13px] text-muted hover:bg-raised hover:text-ink">
            <Plus size={14} /> New project…
          </button>
        </div>
      )}
      <ProjectDialog open={dialog.open} project={dialog.project} onClose={() => setDialog({ open: false })} />
    </div>
  );
}
