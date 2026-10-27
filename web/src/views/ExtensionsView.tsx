import clsx from "clsx";
import { Box, Download, FlaskConical, Pencil, Play, Plug, Plus, Puzzle, RefreshCw, Save, Webhook, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { act, Busy, CodeEditor, ConfirmButton, Dot, Empty, Field, Modal, PageHeader, Tabs, toast, Toggle } from "../components/ui";
import { api, type HookRow, type Hooks, type McpRow, type PluginRow, type SkillRow } from "../lib/api";
import { useLive, useStore } from "../lib/store";

type Tab = "skills" | "plugins" | "mcp" | "hooks";

export function ExtensionsView() {
  const [tab, setTab] = useState<Tab>("skills");
  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader title="Extensions" subtitle="Skills, plugins, MCP servers and hooks. Changes apply immediately, even to chats already running, and persist across restarts." />
      <div className="px-6">
        <Tabs value={tab} onChange={setTab} tabs={[
          { id: "skills", label: <><Box size={14} /> Skills</> },
          { id: "plugins", label: <><Puzzle size={14} /> Plugins</> },
          { id: "mcp", label: <><Plug size={14} /> MCP servers</> },
          { id: "hooks", label: <><Webhook size={14} /> Hooks</> },
        ]} />
      </div>
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-6 py-5">
          {tab === "skills" && <SkillsTab />}
          {tab === "plugins" && <PluginsTab />}
          {tab === "mcp" && <McpTab />}
          {tab === "hooks" && <HooksTab />}
        </div>
      </div>
    </div>
  );
}

function Row({ children, dim }: { children: React.ReactNode; dim?: boolean }) {
  return <div className={clsx("card flex items-start gap-3 p-3.5", dim && "opacity-60")}>{children}</div>;
}

// ──────────────────────────────────────────────────────────────── skills
function SkillsTab() {
  const { project } = useStore();
  const [data, setData] = useState<{ skills: SkillRow[]; errors: string[]; dirs: string[] }>();
  const [edit, setEdit] = useState<{ name: string; content: string; path: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const load = useCallback(() => api.skills(project).then(setData).catch(() => undefined), [project]);
  useEffect(() => void load(), [load]);
  useLive(["changed"], (m) => m.what === "skills" && load());

  const openEditor = async (name: string) => {
    const s = await act(() => api.skill(name, project));
    if (s) setEdit({ name, content: s.content, path: s.path });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-[13px] text-muted">
          A skill is a <span className="font-mono">SKILL.md</span> playbook. The agent sees each skill's name and description and loads the full text when a task matches.
        </p>
        <button className="btn-outline" onClick={() => setImporting(true)}><Download size={14} /> Import</button>
        <button className="btn-primary" onClick={() => setCreating(true)}><Plus size={14} /> New skill</button>
      </div>
      {data?.errors.map((e) => <div key={e} className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-[12.5px] text-bad">{e}</div>)}
      {data && data.skills.length === 0 && <Empty icon={<Box size={18} />} title="No skills yet">Create one here, or import a folder that contains a SKILL.md.</Empty>}
      {data?.skills.map((s) => (
        <Row key={s.name} dim={!s.enabled}>
          <Box size={16} className="mt-0.5 shrink-0 text-brand" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[13.5px] font-medium">{s.name}
              <span className="chip border-line text-faint">{s.scope === "project" ? "this project" : s.scope === "user" ? "all projects" : "external"}</span>
              {s.files.length > 0 && <span className="text-[11px] text-faint">{s.files.length} bundled file(s)</span>}
            </div>
            <div className="mt-0.5 text-[13px] text-muted">{s.description}</div>
            <div className="mt-0.5 truncate font-mono text-[11px] text-faint">{s.path}</div>
          </div>
          <Toggle on={s.enabled} label="enabled" onChange={async (v) => { await act(() => api.toggleSkill(s.name, v)); void load(); }} />
          {s.editable && <button className="btn-ghost px-2" onClick={() => openEditor(s.name)} title="Edit"><Pencil size={14} /></button>}
          {s.editable && <ConfirmButton onConfirm={async () => { await act(() => api.deleteSkill(s.name, project), "Skill deleted"); void load(); }} />}
        </Row>
      ))}
      <NewSkill open={creating} onClose={() => setCreating(false)} onCreated={(name) => { void load(); void openEditor(name); }} />
      <ImportSkill open={importing} onClose={() => setImporting(false)} onDone={load} />
      <SourceEditor open={!!edit} title={`Edit skill · ${edit?.name}`} path={edit?.path} value={edit?.content ?? ""} onClose={() => setEdit(null)}
        onSave={async (content) => {
          const ok = await act(() => api.saveSkill(edit!.name, project, content), "Skill saved");
          if (ok) void load();
          return !!ok;
        }} />
    </div>
  );
}

function NewSkill({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (name: string) => void }) {
  const { project, current } = useStore();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState<"user" | "project">("user");
  useEffect(() => { if (open) { setName(""); setDescription(""); } }, [open]);
  return (
    <Modal open={open} onClose={onClose} title="New skill"
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!name || !description} onClick={async () => {
          if (await act(() => api.createSkill({ name, description, scope, project }))) { onClose(); onCreated(name); }
        }}>Create & edit</button></>}>
      <Field label="Name" hint="lowercase-with-dashes"><input className="input font-mono" autoFocus value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-"))} placeholder="release-checklist" /></Field>
      <Field label="Description" hint="When should the agent use it? This is all the agent sees until it loads the skill.">
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Steps to cut a release: changelog, version bump, tag, publish" />
      </Field>
      <ScopePick scope={scope} setScope={setScope} projectName={current?.name} />
    </Modal>
  );
}

function ScopePick({ scope, setScope, projectName }: { scope: "user" | "project"; setScope: (s: "user" | "project") => void; projectName?: string }) {
  return (
    <div>
      <span className="label">Available in</span>
      <div className="flex gap-2">
        {([["user", "All projects", "~/.cadence/skills"], ["project", projectName ?? "This project", ".cadence/skills in the project"]] as const).map(([id, l, d]) => (
          <button key={id} onClick={() => setScope(id)} className={clsx("flex-1 rounded-lg border px-3 py-2 text-left text-[13px]", scope === id ? "border-brand/50 bg-brand/10" : "border-line")}>
            <div className="font-medium">{l}</div><div className="font-mono text-[11px] text-faint">{d}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function ImportSkill({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const { project, current } = useStore();
  const [source, setSource] = useState("");
  const [scope, setScope] = useState<"user" | "project">("user");
  const [busy, setBusy] = useState(false);
  return (
    <Modal open={open} onClose={onClose} title="Import a skill"
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!source.trim() || busy} onClick={async () => {
          setBusy(true);
          const r = await act(() => api.importSkill(source.trim(), scope, project));
          setBusy(false);
          if (r) { toast(`Imported ${r.name}`); onDone(); onClose(); setSource(""); }
        }}><Busy on={busy}>Import</Busy></button></>}>
      <Field label="Folder or URL" hint="A local folder containing SKILL.md (copied with its bundled files), or an https:// link to a raw SKILL.md.">
        <input className="input font-mono text-[13px]" autoFocus value={source} onChange={(e) => setSource(e.target.value)} placeholder="~/Downloads/pdf-skill  or  https://raw.githubusercontent.com/…/SKILL.md" />
      </Field>
      <ScopePick scope={scope} setScope={setScope} projectName={current?.name} />
    </Modal>
  );
}

function SourceEditor({ open, title, path, value, onClose, onSave, note }: {
  open: boolean; title: string; path?: string; value: string; onClose: () => void; onSave: (v: string) => Promise<boolean>; note?: React.ReactNode;
}) {
  const [text, setText] = useState(value);
  const [busy, setBusy] = useState(false);
  useEffect(() => setText(value), [value, open]);
  const save = async () => {
    setBusy(true);
    const ok = await onSave(text);
    setBusy(false);
    if (ok) onClose();
  };
  return (
    <Modal open={open} onClose={onClose} title={title} wide
      footer={<><span className="mr-auto self-center truncate font-mono text-[11px] text-faint">{path}</span>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" onClick={save} disabled={busy || text === value}><Busy on={busy}><Save size={14} /></Busy> Save</button></>}>
      {note}
      <CodeEditor value={text} onChange={setText} onSave={save} className="h-[55vh]" />
    </Modal>
  );
}

// ─────────────────────────────────────────────────────────────── plugins
function PluginsTab() {
  const [data, setData] = useState<{ plugins: PluginRow[]; dirs: string[] }>();
  const [edit, setEdit] = useState<{ name: string; source: string; path: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const load = useCallback(() => api.plugins().then(setData).catch(() => undefined), []);
  useEffect(() => void load(), [load]);
  useLive(["changed"], (m) => m.what === "plugins" && load());

  const openEditor = async (n: string) => {
    const s = await act(() => api.pluginSource(n));
    if (s) setEdit({ name: n, source: s.source, path: s.path });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-[13px] text-muted">
          Python modules with <span className="font-mono">register(api)</span> that add tools and hooks. They run in-process with your privileges, so only install plugins you trust.
        </p>
        <button className="btn-primary" onClick={() => setCreating(true)}><Plus size={14} /> New plugin</button>
      </div>
      {data && data.plugins.length === 0 && <Empty icon={<Puzzle size={18} />} title="No plugins">Create one from a template here, or drop a .py file in {data.dirs[0]}.</Empty>}
      {data?.plugins.map((p) => (
        <Row key={p.name} dim={!p.enabled}>
          <Puzzle size={16} className={clsx("mt-0.5 shrink-0", p.error ? "text-bad" : "text-brand")} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[13.5px] font-medium">{p.name}
              {p.loaded ? <span className="chip border-ok/40 text-ok">loaded</span> : p.enabled ? <span className="chip border-bad/40 text-bad">not loaded</span> : <span className="chip border-line text-faint">disabled</span>}
            </div>
            <div className="mt-0.5 text-[12.5px] text-muted">
              {p.tools.length ? <>tools: <span className="font-mono">{p.tools.join(", ")}</span></> : "no tools"}{p.hooks ? ` · ${p.hooks} hook(s)` : ""}
            </div>
            {p.error && <div className="mt-1 whitespace-pre-wrap font-mono text-[11.5px] text-bad">{p.error}</div>}
            <div className="mt-0.5 truncate font-mono text-[11px] text-faint">{p.origin}</div>
          </div>
          <Toggle on={p.enabled} label="enabled" onChange={async (v) => { const r = await act(() => api.pluginAction(p.name, v ? "enable" : "disable")); if (r) setData((d) => d && { ...d, plugins: r.plugins }); }} />
          {p.enabled && <button className="btn-ghost px-2" title="Reload from disk" onClick={async () => { const r = await act(() => api.pluginAction(p.name, "reload")); if (r) setData((d) => d && { ...d, plugins: r.plugins }); }}><RefreshCw size={13} /></button>}
          {p.editable && <button className="btn-ghost px-2" onClick={() => openEditor(p.name)} title="Edit source"><Pencil size={14} /></button>}
          {p.editable && <ConfirmButton onConfirm={async () => { const r = await act(() => api.deletePlugin(p.name), "Plugin deleted"); if (r) setData((d) => d && { ...d, plugins: r.plugins }); }} />}
        </Row>
      ))}
      <Modal open={creating} onClose={() => setCreating(false)} title="New plugin"
        footer={<><button className="btn-ghost" onClick={() => setCreating(false)}>Cancel</button>
          <button className="btn-primary" disabled={!name} onClick={async () => {
            const r = await act(() => api.createPlugin(name));
            if (r) { setCreating(false); void load(); void openEditor(name); setName(""); }
          }}>Create from template</button></>}>
        <Field label="Name" hint="Becomes ~/.cadence/plugins/<name>.py"><input className="input font-mono" autoFocus value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))} placeholder="jira_tools" /></Field>
      </Modal>
      <SourceEditor open={!!edit} title={`Edit plugin · ${edit?.name}`} path={edit?.path} value={edit?.source ?? ""} onClose={() => setEdit(null)}
        note={<p className="text-[12.5px] text-faint">Saving checks the syntax, writes the file and reloads the plugin. Its tools update in every chat.</p>}
        onSave={async (source) => {
          const r = await act(() => api.savePlugin(edit!.name, source));
          if (!r) return false;
          void load();
          if (!r.ok) {
            toast(`Saved, but loading failed: ${r.plugin.error}`, "bad");
            return false;
          }
          toast(`Reloaded ${edit!.name}: ${r.plugin.tools.length} tool(s)`);
          return true;
        }} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────── MCP
function McpTab() {
  const [rows, setRows] = useState<McpRow[]>();
  const [edit, setEdit] = useState<McpRow | "new" | null>(null);
  const [importing, setImporting] = useState(false);
  const load = useCallback(() => api.mcp().then(setRows).catch(() => undefined), []);
  useEffect(() => void load(), [load]);
  useLive(["changed"], (m) => m.what === "mcp" && load());

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-[13px] text-muted">
          Model Context Protocol servers over stdio. Their tools appear as <span className="font-mono">mcp__server__tool</span> in every project.
        </p>
        <button className="btn-outline" onClick={() => setImporting(true)}><Download size={14} /> Paste JSON</button>
        <button className="btn-primary" onClick={() => setEdit("new")}><Plus size={14} /> Add server</button>
      </div>
      {rows && rows.length === 0 && (
        <Empty icon={<Plug size={18} />} title="No MCP servers">
          Try <span className="font-mono">npx -y @modelcontextprotocol/server-filesystem ~/notes</span> or paste a Claude Desktop style config.
        </Empty>
      )}
      {rows?.map((s) => (
        <Row key={s.name} dim={!s.enabled}>
          <span className="mt-1.5"><Dot tone={!s.enabled ? "idle" : s.connected ? "ok" : "bad"} /></span>
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] font-medium">{s.name} <span className="ml-1 text-[12px] font-normal text-faint">{s.connected ? `${s.tools.length} tools` : s.enabled ? "not connected" : "disabled"}</span></div>
            <div className="mt-0.5 truncate font-mono text-[12px] text-muted">{s.command} {s.args.join(" ")}</div>
            {s.error && <div className="mt-1 font-mono text-[11.5px] text-bad">{s.error}</div>}
            {s.connected && s.tools.length > 0 && <div className="mt-1 line-clamp-2 font-mono text-[11px] text-faint">{s.tools.map((t) => t.split("__").pop()).join(" · ")}</div>}
          </div>
          <Toggle on={s.enabled} label="enabled" onChange={async (v) => { const r = await act(() => api.putMcp(s.name, { command: s.command, args: s.args, env: s.env, enabled: v })); if (r) setRows(r); }} />
          {s.enabled && <button className="btn-ghost px-2" title="Restart" onClick={async () => { const r = await act(() => api.restartMcp(s.name)); if (r) setRows(r); }}><Play size={13} /></button>}
          <button className="btn-ghost px-2" title="Edit" onClick={() => setEdit(s)}><Pencil size={14} /></button>
          <ConfirmButton onConfirm={async () => { const r = await act(() => api.deleteMcp(s.name), "Server removed"); if (r) setRows(r); }} />
        </Row>
      ))}
      <McpForm open={edit !== null} row={edit === "new" ? null : edit} onClose={() => setEdit(null)} onSaved={setRows} />
      <McpImport open={importing} onClose={() => setImporting(false)} onSaved={setRows} />
    </div>
  );
}

function McpForm({ open, row, onClose, onSaved }: { open: boolean; row: McpRow | null; onClose: () => void; onSaved: (r: McpRow[]) => void }) {
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setName(row?.name ?? "");
    setCommand(row?.command ?? "");
    setArgs(row?.args.join("\n") ?? "");
    setEnv(Object.entries(row?.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
  }, [open, row]);
  const save = async () => {
    setBusy(true);
    const envObj = Object.fromEntries(env.split("\n").filter((l) => l.includes("=")).map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1)]));
    if (row && row.name !== name) await api.deleteMcp(row.name).catch(() => undefined);
    const r = await act(() => api.putMcp(name, { command, args: args.split("\n").map((a) => a.trim()).filter(Boolean), env: envObj, enabled: row?.enabled ?? true }));
    setBusy(false);
    if (r) {
      onSaved(r);
      const me = r.find((x) => x.name === name);
      toast(me?.connected ? `${name} connected: ${me.tools.length} tools` : `${name} saved but not connected: ${me?.error || "see the error"}`, me?.connected ? "ok" : "bad");
      onClose();
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={row ? `Edit ${row.name}` : "Add MCP server"}
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!name || !command || busy} onClick={save}><Busy on={busy}>Save & connect</Busy></button></>}>
      <Field label="Name"><input className="input font-mono" value={name} onChange={(e) => setName(e.target.value.replace(/[^A-Za-z0-9_-]/g, "-"))} placeholder="filesystem" /></Field>
      <Field label="Command"><input className="input font-mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" /></Field>
      <Field label="Arguments" hint="One per line."><textarea className="input min-h-[70px] font-mono text-[12.5px]" value={args} onChange={(e) => setArgs(e.target.value)} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/home/you/notes"} /></Field>
      <Field label="Environment" hint="KEY=value, one per line. Stored secrets show as •••; leave them to keep the saved value. Use $NAME to read from CADENCE's environment.">
        <textarea className="input min-h-[60px] font-mono text-[12.5px]" value={env} onChange={(e) => setEnv(e.target.value)} placeholder="GITHUB_TOKEN=$GITHUB_TOKEN" />
      </Field>
    </Modal>
  );
}

function McpImport({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved: (r: McpRow[]) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal open={open} onClose={onClose} title="Import MCP servers" wide
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!text.trim() || busy} onClick={async () => {
        let block: unknown;
        try { block = JSON.parse(text); } catch { return toast("That is not valid JSON", "bad"); }
        setBusy(true);
        const r = await act(() => api.importMcp(block));
        setBusy(false);
        if (r) { onSaved(r.servers); toast(`Imported ${r.added.join(", ")}`); onClose(); setText(""); }
      }}><Busy on={busy}>Import & connect</Busy></button></>}>
      <p className="text-[13px] text-muted">Paste the <span className="font-mono">mcpServers</span> block from Claude Desktop, Cursor or a server's README.</p>
      <CodeEditor value={text} onChange={setText} className="h-72" placeholder={'{\n  "mcpServers": {\n    "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" } }\n  }\n}'} />
    </Modal>
  );
}

// ───────────────────────────────────────────────────────────────── hooks
const EVENT_HELP: Record<string, string> = {
  session_start: "first turn of an agent", user_prompt: "before the model sees a message (exit 2 refuses it)",
  pre_tool: "before a tool runs (exit 2 blocks it)", post_tool: "after a tool runs (stdout is appended)",
  stop: "the agent finished a turn", task_done: "a task finished", goal_achieved: "a goal was achieved",
};

function HooksTab() {
  const { project } = useStore();
  const [hooks, setHooks] = useState<Hooks>();
  const [rows, setRows] = useState<HookRow[]>([]);
  const [results, setResults] = useState<Record<number, string>>({});
  const load = useCallback(() => api.hooks().then((h) => { setHooks(h); setRows(h.shell); }).catch(() => undefined), []);
  useEffect(() => void load(), [load]);
  const dirty = JSON.stringify(rows) !== JSON.stringify(hooks?.shell ?? []);
  const set = (i: number, patch: Partial<HookRow>) => setRows((r) => r.map((x, k) => (k === i ? { ...x, ...patch } : x)));

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">
        Shell commands run on agent events, with the event as JSON on stdin. Exit 0 adds stdout as context; exit 2 blocks, with stderr as the reason. Same protocol as Claude Code hooks.
      </p>
      {rows.map((h, i) => (
        <div key={i} className="card space-y-2 p-3.5">
          <div className="flex flex-wrap items-center gap-2">
            <select className="input h-8 w-40 py-0 text-[13px]" value={h.event} onChange={(e) => set(i, { event: e.target.value })}>
              {hooks?.events.map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
            {(h.event === "pre_tool" || h.event === "post_tool") && (
              <input className="input h-8 w-40 py-0 font-mono text-[12.5px]" value={h.matcher} onChange={(e) => set(i, { matcher: e.target.value })} placeholder="tool glob, e.g. bash" title="tool-name glob" />
            )}
            <span className="text-[12px] text-faint">{EVENT_HELP[h.event]}</span>
            <div className="ml-auto flex items-center gap-1">
              <input className="input h-8 w-20 py-0 text-[12.5px]" type="number" value={h.timeout} onChange={(e) => set(i, { timeout: Number(e.target.value) })} title="timeout (s)" />
              <button className="btn-ghost px-2" title="Test with a sample event" onClick={async () => {
                const r = await act(() => api.testHook(h, project));
                if (r) setResults((x) => ({ ...x, [i]: r.blocked ? `blocked: ${r.reason}` : r.context ? `context: ${r.context}` : "ran: no output (allowed)" }));
              }}><FlaskConical size={14} /> Test</button>
              <button className="btn-ghost px-2" onClick={() => setRows((r) => r.filter((_, k) => k !== i))} title="Remove"><X size={14} /></button>
            </div>
          </div>
          <input className="input font-mono text-[12.5px]" value={h.command} onChange={(e) => set(i, { command: e.target.value })}
            placeholder={h.event === "pre_tool" ? `jq -e '.arguments.command | test("rm -rf") | not' >/dev/null || { echo "no rm -rf" >&2; exit 2; }` : "notify-send cadence \"$(jq -r .title)\""} />
          {results[i] && <div className={clsx("font-mono text-[12px]", results[i].startsWith("blocked") ? "text-warn" : "text-ok")}>{results[i]}</div>}
        </div>
      ))}
      <div className="flex gap-2">
        <button className="btn-outline" onClick={() => setRows((r) => [...r, { event: "pre_tool", matcher: "bash", command: "", timeout: 30 }])}><Plus size={14} /> Add hook</button>
        {dirty && <button className="btn-primary ml-auto" disabled={rows.some((r) => !r.command.trim())} onClick={async () => {
          const r = await act(() => api.putHooks(rows), "Hooks saved and active");
          if (r) { setHooks(r); setRows(r.shell); }
        }}><Save size={14} /> Save hooks</button>}
      </div>
      {hooks && hooks.plugin.length > 0 && (
        <div className="pt-3">
          <div className="label">From plugins</div>
          <div className="space-y-1 text-[13px]">
            {hooks.plugin.map((h, i) => <div key={i} className="flex gap-2 text-muted"><Webhook size={13} className="mt-0.5 text-faint" /><span className="font-mono">{h.event}{h.matcher !== "*" ? ` · ${h.matcher}` : ""}</span> from <b>{h.owner}</b></div>)}
          </div>
        </div>
      )}
    </div>
  );
}
