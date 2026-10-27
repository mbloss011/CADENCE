import clsx from "clsx";
import { ArrowUpRight, Bot, EyeOff, Lightbulb, ListTodo, Pencil, Plus, Sparkles, Target, Undo2, User } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { View } from "../components/Sidebar";
import { act, ago, Busy, ConfirmButton, Empty, Field, IdeaSize, Modal, PageHeader } from "../components/ui";
import { api, type Idea } from "../lib/api";
import { useStore } from "../lib/store";

const FILTERS = [
  { id: "new", label: "Inbox" },
  { id: "promoted", label: "Promoted" },
  { id: "dismissed", label: "Dismissed" },
] as const;

export function IdeasView({ onGo }: { onGo: (v: View) => void }) {
  const { liveTick, project } = useStore();
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [filter, setFilter] = useState<Idea["status"]>("new");
  const [editing, setEditing] = useState<Idea | null | "new">(null);
  const [generating, setGenerating] = useState(false);

  const load = useCallback(() => api.ideas(project).then(setIdeas).catch(() => undefined), [project]);
  useEffect(() => void load(), [load, liveTick]);

  const generate = async () => {
    setGenerating(true);
    const added = await act(() => api.generateIdeas(project, 3));
    setGenerating(false);
    if (added) {
      void load();
      setFilter("new");
    }
  };

  const promote = async (i: Idea, to: "task" | "goal") => {
    const r = await act(() => api.promoteIdea(i.id, to), to === "goal" ? "Promoted to a goal; the planner will take it from here" : "Promoted to a task and queued");
    if (r) void load();
  };

  const shown = ideas.filter((i) => i.status === filter);

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader
        title="Ideas"
        subtitle="Things worth doing, from you or from the daemon's idea cycles. Nothing runs until you promote an idea to a task (one change) or a goal (a longer push)."
        actions={<>
          <button className="btn-outline" onClick={generate} disabled={generating}><Busy on={generating}><Sparkles size={14} className="text-brand" /></Busy> Brainstorm</button>
          <button className="btn-primary" onClick={() => setEditing("new")}><Plus size={15} /> New idea</button>
        </>}
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 px-6 py-5">
          <div className="flex gap-1.5">
            {FILTERS.map((f) => (
              <button key={f.id} onClick={() => setFilter(f.id)}
                className={clsx("rounded-full border px-3 py-1 text-xs transition", filter === f.id ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted hover:text-ink")}>
                {f.label} <span className="text-faint">{ideas.filter((i) => i.status === f.id).length}</span>
              </button>
            ))}
          </div>
          {shown.length === 0 ? (
            <Empty icon={<Lightbulb size={18} />} title={filter === "new" ? "Inbox zero" : `No ${filter} ideas`}>
              {filter === "new" ? <>Jot down an idea, or press <b>Brainstorm</b> to have the big model read the project and suggest some. The daemon also adds ideas during its heartbeat cycles.</> : null}
            </Empty>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {shown.map((i) => (
                <div key={i.id} className="card flex flex-col p-4">
                  <div className="flex items-center gap-2">
                    <IdeaSize size={i.size} />
                    <span className="flex items-center gap-1 text-[11px] text-faint">{i.source === "auto" ? <Bot size={11} /> : <User size={11} />}{i.source === "auto" ? "daemon" : "you"} · {ago(i.created)}</span>
                    <div className="ml-auto flex items-center">
                      {i.status === "new" && <button className="btn-ghost px-1.5" onClick={() => setEditing(i)} title="Edit"><Pencil size={13} /></button>}
                      {i.status === "new" ? (
                        <button className="btn-ghost px-1.5" title="Dismiss" onClick={async () => { await act(() => api.editIdea(i.id, { status: "dismissed" })); void load(); }}><EyeOff size={13} /></button>
                      ) : i.status === "dismissed" ? (
                        <button className="btn-ghost px-1.5" title="Back to inbox" onClick={async () => { await act(() => api.editIdea(i.id, { status: "new" })); void load(); }}><Undo2 size={13} /></button>
                      ) : null}
                      <ConfirmButton onConfirm={async () => { await act(() => api.deleteIdea(i.id)); void load(); }} className="px-1.5" />
                    </div>
                  </div>
                  <div className="mt-2 text-[14.5px] font-medium leading-snug">{i.title}</div>
                  {i.description && <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-[13px] text-muted">{i.description}</p>}
                  {i.acceptance.length > 0 && (
                    <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[12px] text-faint">{i.acceptance.slice(0, 4).map((a, k) => <li key={k}>{a}</li>)}</ul>
                  )}
                  <div className="mt-auto pt-3">
                    {i.status === "new" ? (
                      <div className="flex gap-2">
                        <button className={clsx("flex-1", i.size === "task" ? "btn-primary" : "btn-outline")} onClick={() => promote(i, "task")}><ListTodo size={14} /> Make it a task</button>
                        <button className={clsx("flex-1", i.size === "goal" ? "btn-primary" : "btn-outline")} onClick={() => promote(i, "goal")}><Target size={14} /> Make it a goal</button>
                      </div>
                    ) : i.status === "promoted" ? (
                      <button className="btn-ghost px-0 text-[12.5px] text-ok" onClick={() => onGo(i.promoted_to.startsWith("goal") ? "goals" : "tasks")}>
                        <ArrowUpRight size={13} /> Promoted to {i.promoted_to.split(":")[0]}
                      </button>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <IdeaForm open={editing !== null} idea={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={load} />
    </div>
  );
}

function IdeaForm({ open, idea, onClose, onSaved }: { open: boolean; idea: Idea | null; onClose: () => void; onSaved: () => void }) {
  const { project } = useStore();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [size, setSize] = useState<Idea["size"]>("task");
  const [acceptance, setAcceptance] = useState("");
  const [verify, setVerify] = useState("");
  useEffect(() => {
    if (!open) return;
    setTitle(idea?.title ?? "");
    setDescription(idea?.description ?? "");
    setSize(idea?.size ?? "task");
    setAcceptance(idea?.acceptance.join("\n") ?? "");
    setVerify(idea?.verify_command ?? "");
  }, [open, idea]);
  const save = async () => {
    const body = { title, description, size, acceptance: acceptance.split("\n").map((x) => x.trim()).filter(Boolean), verify_command: verify };
    const r = await act(() => (idea ? api.editIdea(idea.id, body) : api.addIdea({ ...body, project })), idea ? "Idea saved" : "Idea added to the inbox");
    if (r) {
      onSaved();
      onClose();
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={idea ? "Edit idea" : "New idea"}
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!title.trim()} onClick={save}>Save</button></>}>
      <Field label="Idea"><input className="input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Cache API responses" /></Field>
      <Field label="Notes"><textarea className="input min-h-[80px]" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
      <div>
        <span className="label">Size</span>
        <div className="flex gap-2">
          {(["task", "goal"] as const).map((s) => (
            <button key={s} onClick={() => setSize(s)} className={clsx("flex-1 rounded-lg border px-3 py-2 text-left text-[13px] transition", size === s ? "border-brand/50 bg-brand/10" : "border-line hover:border-faint")}>
              <div className="font-medium">{s === "task" ? "Task-sized" : "Goal-sized"}</div>
              <div className="text-[12px] text-faint">{s === "task" ? "One focused change" : "Needs planning over many sessions"}</div>
            </button>
          ))}
        </div>
      </div>
      <Field label={size === "goal" ? "Success criteria" : "Acceptance criteria"} hint="One per line (optional)."><textarea className="input min-h-[60px]" value={acceptance} onChange={(e) => setAcceptance(e.target.value)} /></Field>
      {size === "task" && <Field label="Verify command (optional)"><input className="input font-mono" value={verify} onChange={(e) => setVerify(e.target.value)} placeholder="npm test" /></Field>}
    </Modal>
  );
}
