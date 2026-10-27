import clsx from "clsx";
import { Archive, ArchiveRestore, Brain, Cpu, Eye, FolderOpen, Globe, Lock, Pencil, Plus, Radar, RefreshCw, Save, Server, Shield, SlidersHorizontal, Trash2, Zap } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ProjectDialog, ProjectMark } from "../components/ProjectDialog";
import { act, Busy, Dot, Field, PageHeader, Tabs, toast, Toggle } from "../components/ui";
import { api, type Discovery, type Project, type Provider, type Settings, type Status } from "../lib/api";
import { useStore } from "../lib/store";
import { StatusPanel } from "./StatusPanel";

type Tab = "models" | "agent" | "safety" | "projects" | "status";

export function SettingsView({ status, refreshStatus }: { status: Status | null; refreshStatus: () => void }) {
  const [tab, setTab] = useState<Tab>("models");
  const [settings, setSettings] = useState<Settings>();
  const load = useCallback(() => api.settings().then(setSettings).catch((e) => toast(e.message, "bad")), []);
  useEffect(() => void load(), [load]);

  const patch = async (p: Record<string, unknown>, ok = "Saved") => {
    const s = await act(() => api.patchSettings(p));
    if (s) {
      setSettings(s);
      refreshStatus();
      toast(s.restart_required.length ? `${ok}. Restart Kestrel to apply: ${s.restart_required.join(", ")}` : `${ok} and applied`, s.restart_required.length ? "info" : "ok");
    }
    return s;
  };

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader title="Settings" subtitle={settings ? <>Saved to <span className="font-mono">{settings.paths.settings.replace(/^\/home\/[^/]+/, "~")}</span>, applied live, and kept across restarts.</> : undefined} />
      <div className="px-6">
        <Tabs value={tab} onChange={setTab} tabs={[
          { id: "models", label: <><Cpu size={14} /> Models</> },
          { id: "agent", label: <><SlidersHorizontal size={14} /> Agent & daemon</> },
          { id: "safety", label: <><Shield size={14} /> Sandbox, memory & browser</> },
          { id: "projects", label: <><FolderOpen size={14} /> Projects</> },
          { id: "status", label: <><Radar size={14} /> Status</> },
        ]} />
      </div>
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-6 py-5">
          {!settings ? <p className="text-sm text-faint">Loading…</p> : (
            <>
              {tab === "models" && <ModelsTab s={settings} patch={patch} />}
              {tab === "agent" && <AgentTab s={settings} patch={patch} />}
              {tab === "safety" && <SafetyTab s={settings} patch={patch} />}
              {tab === "projects" && <ProjectsTab />}
              {tab === "status" && <StatusPanel status={status} refresh={refreshStatus} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

type Patch = (p: Record<string, unknown>, ok?: string) => Promise<Settings | undefined>;

function Card({ title, icon, children, actions }: { title: string; icon: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card p-5">
      <div className="mb-4 flex items-center gap-2">
        <span className="text-brand">{icon}</span>
        <h3 className="text-[15px] font-semibold">{title}</h3>
        <div className="ml-auto flex gap-2">{actions}</div>
      </div>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function Locked({ s, path }: { s: Settings; path: string }) {
  const env = s.locked[path];
  return env ? <span className="chip border-warn/40 text-warn" title={`Set by $${env}; it overrides this setting at startup`}><Lock size={10} /> ${env}</span> : null;
}

// ──────────────────────────────────────────────────────────────── models
const ROLES = [
  { id: "big", label: "Big model", icon: Zap, hint: "Plans, writes code, uses tools, reflects. Needs tool calling.", types: ["llm", "vlm"] },
  { id: "small", label: "Small model", icon: Cpu, hint: "Housekeeping: context compaction, project names, lesson distillation.", types: ["llm", "vlm"] },
  { id: "vision", label: "Vision model", icon: Eye, hint: "Screenshots, attachments, browser_look. Empty uses the big model.", types: ["vlm", "llm"] },
  { id: "embed", label: "Embeddings", icon: Brain, hint: "Vector memory. Changing it re-indexes memory after a restart.", types: ["embeddings"] },
] as const;

function ModelsTab({ s, patch }: { s: Settings; patch: Patch }) {
  const [disc, setDisc] = useState<Discovery>();
  const [scanning, setScanning] = useState(false);
  const [providers, setProviders] = useState<Provider[]>(s.providers);
  useEffect(() => setProviders(s.providers), [s.providers]);

  const scan = useCallback(async () => {
    setScanning(true);
    setDisc(await api.models().catch(() => undefined));
    setScanning(false);
  }, []);
  useEffect(() => void scan(), [scan]);

  const all = useMemo(() => (disc?.providers ?? []).flatMap((p) => p.models.map((m) => ({ ...m, provider: p.name, base_url: p.base_url }))), [disc]);
  const current = (role: string) => {
    if (role === "embed") return { name: s.memory.embed_model, base_url: s.memory.embed_url || s.big.base_url };
    const m = (s as unknown as Record<string, { name: string; base_url: string } | null>)[role];
    return m ? { name: m.name, base_url: m.base_url } : null;
  };

  const assign = async (role: string, value: string) => {
    if (role === "vision" && !value) return patch({ vision: null }, "Vision now uses the big model");
    const [base_url, name] = JSON.parse(value) as [string, string];
    if (role === "embed") return patch({ memory: { embed_model: name, embed_url: base_url === s.big.base_url ? "" : base_url } }, `Embeddings → ${name}`);
    return patch({ [role]: { name, base_url } }, `${role} model → ${name}`);
  };

  const providersDirty = JSON.stringify(providers.map(({ name, kind, url }) => ({ name, kind, url }))) !== JSON.stringify(s.providers.map(({ name, kind, url }) => ({ name, kind, url })));

  return (
    <div className="space-y-5">
      <Card title="Model roles" icon={<Zap size={16} />} actions={<button className="btn-outline" onClick={scan} disabled={scanning}><Busy on={scanning}><RefreshCw size={13} /></Busy> Scan servers</button>}>
        {ROLES.map((r) => {
          const cur = current(r.id);
          const options = all.filter((m) => (r.types as readonly string[]).includes(m.type));
          const others = all.filter((m) => !(r.types as readonly string[]).includes(m.type));
          const known = cur && all.some((m) => m.id === cur.name && m.base_url === cur.base_url);
          const value = cur ? JSON.stringify([cur.base_url, cur.name]) : "";
          return (
            <div key={r.id} className="grid items-start gap-3 md:grid-cols-[180px_1fr]">
              <div>
                <div className="flex items-center gap-1.5 text-[13.5px] font-medium"><r.icon size={14} className="text-faint" /> {r.label}
                  {r.id !== "embed" && r.id !== "vision" && <Locked s={s} path={`${r.id}.name`} />}</div>
                <div className="mt-0.5 text-[12px] text-faint">{r.hint}</div>
              </div>
              <div className="space-y-1.5">
                <select className="input font-mono text-[13px]" value={value} onChange={(e) => assign(r.id, e.target.value)}>
                  {r.id === "vision" && <option value="">(same as the big model)</option>}
                  {cur && !known && <option value={value}>{cur.name} · {cur.base_url} (not found on any server)</option>}
                  {(disc?.providers ?? []).map((p) => {
                    const ms = options.filter((m) => m.provider === p.name);
                    return ms.length ? (
                      <optgroup key={p.name} label={`${p.name} · ${p.url}`}>
                        {ms.map((m) => <option key={m.id} value={JSON.stringify([p.base_url, m.id])}>{m.id}{m.state === "loaded" ? " ●" : ""}{m.detail ? ` · ${m.detail}` : ""}</option>)}
                      </optgroup>
                    ) : null;
                  })}
                  {r.id !== "embed" && others.length > 0 && (
                    <optgroup label="Other models">
                      {others.map((m) => <option key={m.provider + m.id} value={JSON.stringify([m.base_url, m.id])}>{m.id} ({m.type})</option>)}
                    </optgroup>
                  )}
                </select>
                {r.id !== "embed" && cur && <ModelParams role={r.id} s={s} patch={patch} />}
              </div>
            </div>
          );
        })}
        <p className="text-[12px] text-faint">● = loaded in LM Studio. Unloaded LM Studio models load on first use, which can take a while.</p>
      </Card>

      <Card title="Model servers" icon={<Server size={16} />}
        actions={providersDirty ? <button className="btn-primary" onClick={async () => { if (await patch({ providers }, "Servers saved")) void scan(); }}><Save size={13} /> Save</button> : undefined}>
        {disc?.providers.map((p, i) => (
          <div key={i} className="rounded-lg border border-line p-3">
            <div className="flex items-center gap-2">
              <Dot tone={p.ok ? "ok" : "bad"} />
              <span className="text-[13.5px] font-medium">{p.name}</span>
              <span className="chip border-line text-faint">{p.kind}</span>
              <span className="font-mono text-[12px] text-muted">{p.url}</span>
              <span className="ml-auto text-[12px] text-faint">{p.ok ? `${p.models.length} model(s)` : p.error || "unreachable"}</span>
            </div>
            {p.ok && p.models.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {p.models.map((m) => (
                  <span key={m.id} className={clsx("chip font-mono", m.state === "loaded" ? "border-ok/40 text-ok" : "border-line text-muted")} title={`${m.type}${m.detail ? ` · ${m.detail}` : ""}${m.context ? ` · ${m.context} ctx` : ""}`}>
                    {m.type === "embeddings" ? <Brain size={10} /> : m.type === "vlm" ? <Eye size={10} /> : <Cpu size={10} />} {m.id}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
        <div className="space-y-2">
          <div className="label">Servers to scan</div>
          {providers.map((p, i) => (
            <div key={i} className="flex gap-2">
              <input className="input w-40" value={p.name} onChange={(e) => setProviders((x) => x.map((y, k) => (k === i ? { ...y, name: e.target.value } : y)))} />
              <select className="input w-36" value={p.kind} onChange={(e) => setProviders((x) => x.map((y, k) => (k === i ? { ...y, kind: e.target.value as Provider["kind"] } : y)))}>
                <option value="lmstudio">LM Studio</option><option value="ollama">Ollama</option><option value="openai">OpenAI-compatible</option>
              </select>
              <input className="input flex-1 font-mono text-[13px]" value={p.url} onChange={(e) => setProviders((x) => x.map((y, k) => (k === i ? { ...y, url: e.target.value } : y)))} />
              <button className="btn-ghost px-2" onClick={() => setProviders((x) => x.filter((_, k) => k !== i))}><Trash2 size={14} /></button>
            </div>
          ))}
          <button className="btn-outline" onClick={() => setProviders((x) => [...x, { name: "Server", kind: "openai", url: "http://127.0.0.1:8080" }])}><Plus size={14} /> Add server</button>
          <p className="text-[12px] text-faint">LM Studio defaults to :1234 and Ollama to :11434. Any OpenAI-compatible server works: llama.cpp, vLLM, LocalAI.</p>
        </div>
      </Card>
    </div>
  );
}

function ModelParams({ role, s, patch }: { role: string; s: Settings; patch: Patch }) {
  const m = (s as unknown as Record<string, Settings["big"] | null>)[role];
  const [open, setOpen] = useState(false);
  const [v, setV] = useState(m);
  useEffect(() => setV(m), [m]);
  if (!m || !v) return null;
  const dirty = v.temperature !== m.temperature || v.max_tokens !== m.max_tokens || v.context_window !== m.context_window;
  return (
    <div>
      <button className="text-[12px] text-faint hover:text-muted" onClick={() => setOpen((o) => !o)}>
        {open ? "▾" : "▸"} temperature {m.temperature} · max tokens {m.max_tokens} · context {Math.round(m.context_window / 1024)}k
      </button>
      {open && (
        <div className="mt-2 grid grid-cols-3 gap-2">
          <Field label="Temperature"><input className="input" type="number" step="0.05" value={v.temperature} onChange={(e) => setV({ ...v, temperature: Number(e.target.value) })} /></Field>
          <Field label="Max tokens"><input className="input" type="number" value={v.max_tokens} onChange={(e) => setV({ ...v, max_tokens: Number(e.target.value) })} /></Field>
          <Field label="Context window"><input className="input" type="number" value={v.context_window} onChange={(e) => setV({ ...v, context_window: Number(e.target.value) })} /></Field>
          {dirty && <button className="btn-primary col-span-3 justify-self-end" onClick={() => patch({ [role]: { temperature: v.temperature, max_tokens: v.max_tokens, context_window: v.context_window } })}><Save size={13} /> Save</button>}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────── agent/daemon
function NumberRow({ label, hint, value, onChange, step = 1 }: { label: string; hint: string; value: number; onChange: (n: number) => void; step?: number }) {
  return (
    <div className="flex items-center gap-4">
      <div className="flex-1"><div className="text-[13.5px]">{label}</div><div className="text-[12px] text-faint">{hint}</div></div>
      <input className="input w-28 text-right" type="number" step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}
function ToggleRow({ label, hint, value, onChange }: { label: string; hint: ReactNode; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center gap-4">
      <div className="flex-1"><div className="text-[13.5px]">{label}</div><div className="text-[12px] text-faint">{hint}</div></div>
      <Toggle on={value} onChange={onChange} label={label} />
    </div>
  );
}

function useSection<T>(initial: T): [T, (p: Partial<T>) => void, boolean, () => void] {
  const [v, setV] = useState(initial);
  useEffect(() => setV(initial), [initial]);
  return [v, (p) => setV((x) => ({ ...x, ...p })), JSON.stringify(v) !== JSON.stringify(initial), () => setV(initial)];
}

function SaveBar({ dirty, onSave, onReset }: { dirty: boolean; onSave: () => void; onReset: () => void }) {
  return dirty ? <><button className="btn-ghost" onClick={onReset}>Reset</button><button className="btn-primary" onClick={onSave}><Save size={13} /> Save</button></> : null;
}

function AgentTab({ s, patch }: { s: Settings; patch: Patch }) {
  const [agent, setAgent, agentDirty, resetAgent] = useSection(s.agent);
  const [daemon, setDaemon, daemonDirty, resetDaemon] = useSection(s.daemon);
  return (
    <div className="space-y-5">
      <Card title="Agent" icon={<SlidersHorizontal size={16} />} actions={<SaveBar dirty={agentDirty} onReset={resetAgent} onSave={() => patch({ agent })} />}>
        <NumberRow label="Max steps per turn" hint="Model calls before the agent stops and reports progress" value={agent.max_iterations} onChange={(n) => setAgent({ max_iterations: n })} />
        <NumberRow label="Tool timeout (s)" hint="Longest a single tool call may run" value={agent.tool_timeout} onChange={(n) => setAgent({ tool_timeout: n })} />
        <NumberRow label="Tool output limit (chars)" hint="Longer results are truncated in the middle" value={agent.tool_output_limit} onChange={(n) => setAgent({ tool_output_limit: n })} />
        <ToggleRow label="Allow shell" hint="The bash tool. Off makes the agent read/write-only." value={agent.allow_shell} onChange={(v) => setAgent({ allow_shell: v })} />
      </Card>
      <Card title="Daemon" icon={<Radar size={16} />} actions={<SaveBar dirty={daemonDirty} onReset={resetDaemon} onSave={() => patch({ daemon })} />}>
        <NumberRow label="Idea cycles per day" hint="Heartbeat that fills the idea inbox and consolidates lessons" value={daemon.reflection_cycles} onChange={(n) => setDaemon({ reflection_cycles: n })} />
        <NumberRow label="Parallel tasks" hint="Tasks the daemon runs at once (they share your model server)" value={daemon.max_parallel_goals} onChange={(n) => setDaemon({ max_parallel_goals: n })} />
        <NumberRow label="Tasks per goal per day" hint="Budget the planner may spend on each long-horizon goal daily" value={daemon.goal_daily_tasks} onChange={(n) => setDaemon({ goal_daily_tasks: n })} />
        <NumberRow label="Retries per task" hint="Reflect-and-retry rounds before a task is marked failed" value={daemon.max_reflections_per_goal} onChange={(n) => setDaemon({ max_reflections_per_goal: n })} />
        <ToggleRow label="Auto-approve ideas" hint="Task-sized ideas from the daemon become tasks without asking you" value={daemon.auto_approve_ideas} onChange={(v) => setDaemon({ auto_approve_ideas: v })} />
      </Card>
    </div>
  );
}

// ────────────────────────────────────────────────────── sandbox / memory
function SafetyTab({ s, patch }: { s: Settings; patch: Patch }) {
  const [sb, setSb, sbDirty, resetSb] = useSection(s.sandbox);
  const [mem, setMem, memDirty, resetMem] = useSection(s.memory);
  const [br, setBr, brDirty, resetBr] = useSection(s.browser);
  return (
    <div className="space-y-5">
      <Card title="Sandbox" icon={<Shield size={16} />} actions={<SaveBar dirty={sbDirty} onReset={resetSb} onSave={() => patch({ sandbox: sb })} />}>
        <div className="grid gap-2 sm:grid-cols-3">
          {([["none", "None", "bash runs as you, unrestricted"], ["bwrap", "Bubblewrap", "read-only system, writable project, hidden $HOME"], ["docker", "Docker", "throwaway container with the project mounted"]] as const).map(([id, l, d]) => (
            <button key={id} onClick={() => setSb({ mode: id })} className={clsx("rounded-lg border px-3 py-2.5 text-left", sb.mode === id ? "border-brand/50 bg-brand/10" : "border-line hover:border-faint")}>
              <div className="flex items-center gap-1.5 text-[13.5px] font-medium">{l} <Locked s={s} path="sandbox.mode" /></div>
              <div className="text-[12px] text-faint">{d}</div>
            </button>
          ))}
        </div>
        <ToggleRow label="Network inside the sandbox" hint="Needed for package installs; off for untrusted work" value={sb.network} onChange={(v) => setSb({ network: v })} />
        <ToggleRow label="Hide home directory" hint="bwrap: mount an empty $HOME so ~/.ssh and friends are invisible" value={sb.hide_home} onChange={(v) => setSb({ hide_home: v })} />
        {sb.mode === "docker" && <Field label="Docker image"><input className="input font-mono" value={sb.image} onChange={(e) => setSb({ image: e.target.value })} /></Field>}
      </Card>
      <Card title="Memory" icon={<Brain size={16} />} actions={<SaveBar dirty={memDirty} onReset={resetMem} onSave={() => patch({ memory: mem })} />}>
        <div className="grid gap-2 sm:grid-cols-3">
          {([["qdrant", "Qdrant", "vector server (docker compose)"], ["local", "Local", "vectors in a JSONL file"], ["keyword", "Keyword", "no embeddings"]] as const).map(([id, l, d]) => (
            <button key={id} onClick={() => setMem({ backend: id })} className={clsx("rounded-lg border px-3 py-2.5 text-left", mem.backend === id ? "border-brand/50 bg-brand/10" : "border-line hover:border-faint")}>
              <div className="flex items-center gap-1.5 text-[13.5px] font-medium">{l} <Locked s={s} path="memory.backend" /></div>
              <div className="text-[12px] text-faint">{d}</div>
            </button>
          ))}
        </div>
        {mem.backend === "qdrant" && <Field label="Qdrant URL"><input className="input font-mono" value={mem.qdrant_url} onChange={(e) => setMem({ qdrant_url: e.target.value })} /></Field>}
        <NumberRow label="Semantic weight" hint="Blend of meaning vs keyword overlap (0-1)" step={0.05} value={mem.semantic_weight} onChange={(n) => setMem({ semantic_weight: n })} />
        <NumberRow label="Similarity floor" hint="Below this, a memory counts as unrelated" step={0.01} value={mem.min_similarity} onChange={(n) => setMem({ min_similarity: n })} />
        <p className="text-[12px] text-faint">Changing the backend or embedding model takes effect after a restart.</p>
      </Card>
      <Card title="Shared browser" icon={<Globe size={16} />} actions={<SaveBar dirty={brDirty} onReset={resetBr} onSave={() => patch({ browser: br })} />}>
        <ToggleRow label="Browser tools" hint="Lets the agent open and drive the shared browser (it runs outside the sandbox, with network access)" value={br.enabled} onChange={(v) => setBr({ enabled: v })} />
        <Field label="Chrome / Chromium executable" hint="Empty finds google-chrome or chromium on PATH. Profiles (logins) live in ~/.kestrel/state/browser/<project>.">
          <input className="input font-mono" value={br.executable} onChange={(e) => setBr({ executable: e.target.value })} placeholder="/usr/bin/google-chrome" />
        </Field>
      </Card>
    </div>
  );
}

// ────────────────────────────────────────────────────────────── projects
function ProjectsTab() {
  const store = useStore();
  const [dialog, setDialog] = useState<{ open: boolean; project?: Project }>({ open: false });
  return (
    <div className="space-y-3">
      <div className="flex items-center">
        <p className="flex-1 text-[13px] text-muted">Each project is a folder. Chats, tasks, goals, ideas, canvas documents and the browser profile are kept separate per project. Archiving hides a project and stops the daemon working in it; nothing is deleted.</p>
        <button className="btn-primary" onClick={() => setDialog({ open: true })}><Plus size={14} /> New project</button>
      </div>
      {[...store.projects].sort((a, b) => Number(a.archived) - Number(b.archived)).map((p) => (
        <div key={p.id} className={clsx("card flex items-center gap-3 p-3.5", p.archived && "opacity-60")}>
          <ProjectMark project={p} size={30} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[14px] font-medium">{p.name}
              {p.default && <span className="chip border-line text-faint">launch folder</span>}
              {p.id === store.project && <span className="chip border-brand/40 text-brand">active</span>}
              {p.archived && <span className="chip border-line text-faint">archived</span>}
              {!p.exists && <span className="chip border-bad/40 text-bad">folder missing</span>}
            </div>
            <div className="truncate font-mono text-[12px] text-muted">{p.path}</div>
            {p.description && <div className="truncate text-[12.5px] text-faint">{p.description}</div>}
          </div>
          <div className="flex gap-3 text-[12px] text-faint">
            <span>{p.counts.goals} goals</span><span>{p.counts.tasks} open tasks</span><span>{p.counts.ideas} ideas</span>
          </div>
          {p.id !== store.project && <button className="btn-outline px-2.5 py-1" onClick={() => store.setProject(p.id)}>Open</button>}
          {!p.default && (
            <button className="btn-ghost px-2" title={p.archived ? "Restore" : "Archive: hide it and stop background work"} onClick={async () => {
              if (await act(() => api.editProject(p.id, { archived: !p.archived }), p.archived ? "Restored" : "Archived")) {
                await store.refreshProjects();
                if (!p.archived && p.id === store.project) store.setProject(store.projects.find((x) => x.default)?.id ?? "");
              }
            }}>{p.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}</button>
          )}
          <button className="btn-ghost px-2" onClick={() => setDialog({ open: true, project: p })}><Pencil size={14} /></button>
        </div>
      ))}
      <ProjectDialog open={dialog.open} project={dialog.project} onClose={() => setDialog({ open: false })} />
    </div>
  );
}
