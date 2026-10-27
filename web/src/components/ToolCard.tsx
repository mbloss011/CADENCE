import clsx from "clsx";
import {
  Bot, Brain, ChevronRight, FilePen, FilePlus2, FileText, FolderTree, Globe, Image, ListChecks, NotebookPen, Plug, Search,
  Sparkles, SquareTerminal, StickyNote, Wrench, type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { Spinner } from "./ui";

const ICONS: Record<string, LucideIcon> = {
  read_file: FileText, write_file: FilePlus2, edit_file: FilePen, list_files: FolderTree, grep: Search,
  bash: SquareTerminal, view_image: Image, memory_add: Brain, memory_search: Brain, todo_write: ListChecks,
  spawn_subagent: Bot, load_skill: Sparkles, scratchpad_set: StickyNote,
  canvas_write: NotebookPen, canvas_edit: NotebookPen, canvas_read: NotebookPen, canvas_list: NotebookPen, canvas_export: NotebookPen,
  browser_open: Globe, browser_snapshot: Globe, browser_click: Globe, browser_type: Globe, browser_scroll: Globe,
  browser_back: Globe, browser_key: Globe, browser_look: Globe,
};

function parse(args: string): Record<string, unknown> {
  try {
    const v = JSON.parse(args || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

function headline(name: string, a: Record<string, unknown>): string {
  const s = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : "");
  switch (name) {
    case "bash": return s("command");
    case "read_file": case "write_file": case "edit_file": return s("path");
    case "list_files": return `${s("path") || "."} ${s("pattern") ? `· ${s("pattern")}` : ""}`;
    case "grep": return `/${s("pattern")}/ ${s("path") ? `in ${s("path")}` : ""}`;
    case "view_image": return `${s("path")}${s("question") ? ` — ${s("question")}` : ""}`;
    case "memory_add": return `${s("kind")}: ${s("text")}`;
    case "memory_search": return s("query");
    case "spawn_subagent": return s("task");
    case "load_skill": return s("name");
    case "scratchpad_set": return s("key");
    case "todo_write": return `${Array.isArray(a.todos) ? a.todos.length : 0} tasks`;
    case "canvas_write": return s("title") || `revise ${s("id")}`;
    case "canvas_edit": case "canvas_read": return s("id");
    case "browser_open": return s("url");
    case "browser_click": return `element [${a.element}]`;
    case "browser_type": return `[${a.element}] ← ${s("text")}`;
    case "browser_look": return s("question") || "look at the page";
    default: {
      const first = Object.values(a).find((v) => typeof v === "string") as string | undefined;
      return first ?? "";
    }
  }
}

export function ToolCard({ name, arguments: args, result }: { name: string; arguments: string; result: string | null }) {
  const [open, setOpen] = useState(false);
  const a = parse(args);
  const running = result === null;
  const failed = !!result && result.startsWith("ERROR");
  const blocked = !!result && result.startsWith("ERROR: blocked by hook");
  const exit = result?.match(/^\[exit (-?\d+)\]/)?.[1];
  const Icon = name.startsWith("mcp__") ? Plug : ICONS[name] ?? Wrench;
  const label = name.startsWith("mcp__") ? name.split("__").slice(1).join(" · ") : name;
  const head = headline(name, a);

  return (
    <div
      className={clsx(
        "group overflow-hidden rounded-lg border bg-panel transition animate-rise",
        running ? "border-brand/40 shadow-glow" : failed ? "border-bad/35" : "border-line",
      )}
    >
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2.5 px-3 py-2 text-left">
        <ChevronRight size={14} className={clsx("shrink-0 text-faint transition", open && "rotate-90")} />
        <Icon size={15} className={clsx("shrink-0", running ? "text-brand" : failed ? "text-bad" : "text-muted")} />
        <span className="shrink-0 font-mono text-[12.5px] font-medium text-ink">{label}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted">{head}</span>
        {running && <Spinner className="text-brand" />}
        {blocked && <span className="chip border-flame/40 bg-flame/10 text-flame">blocked</span>}
        {exit !== undefined && (
          <span className={clsx("chip font-mono", exit === "0" ? "border-ok/30 bg-ok/10 text-ok" : "border-bad/30 bg-bad/10 text-bad")}>
            exit {exit}
          </span>
        )}
        {failed && !blocked && exit === undefined && <span className="chip border-bad/30 bg-bad/10 text-bad">error</span>}
      </button>
      {open && (
        <div className="space-y-2 border-t border-line bg-bg/60 px-3 py-2.5">
          {Object.keys(a).length > 0 && (
            <div>
              <div className="label">arguments</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-muted">
                {name === "bash" ? String(a.command ?? "") : JSON.stringify(a, null, 2)}
              </pre>
            </div>
          )}
          <div>
            <div className="label">{running ? "running…" : "result"}</div>
            <pre
              className={clsx(
                "max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed",
                failed ? "text-bad" : "text-ink/85",
              )}
            >
              {running ? "" : result?.replace(/^\[exit -?\d+\]\n?/, "") || "(no output)"}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
