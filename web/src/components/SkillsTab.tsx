import clsx from "clsx";
import {
  AlertTriangle, Box, ChevronRight, Download, FileCode2, FilePlus2, FileText, FolderOpen, Image as ImageIcon, Library, Package, Plus,
  Save, Terminal, Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { api, ApiError, type SkillFile, type SkillRow } from "../lib/api";
import { useLive, useStore } from "../lib/store";
import { act, Busy, CodeEditor, ConfirmButton, Empty, Field, Modal, toast, Toggle } from "./ui";

type Scope = "user" | "project";

const GROUPS: { id: "references" | "scripts" | "assets"; label: string; hint: string; icon: typeof FileText }[] = [
  { id: "references", label: "references", hint: "docs the agent reads only when a step needs them", icon: Library },
  { id: "scripts", label: "scripts", hint: "code the agent runs (sandboxed) instead of rewriting", icon: Terminal },
  { id: "assets", label: "assets", hint: "templates, images, fonts copied into outputs", icon: ImageIcon },
];

/** Upload with a one-click retry as "replace" when a skill of that name exists. */
async function withOverwrite<T>(run: (overwrite: boolean) => Promise<T>): Promise<T | undefined> {
  try {
    return await run(false);
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && confirm(`${e.message}\n\nReplace it?`)) return act(() => run(true));
    toast((e as Error).message, "bad");
    return undefined;
  }
}

export function SkillsTab() {
  const { project, current } = useStore();
  const [data, setData] = useState<{ skills: SkillRow[]; errors: string[]; dirs: string[] }>();
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [scope, setScope] = useState<Scope>("user");
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const zipRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);
  const load = useCallback(() => api.skills(project).then(setData).catch(() => undefined), [project]);
  useEffect(() => void load(), [load]);
  useLive(["changed"], (m) => m.what === "skills" && load());

  const upload = async (files: File[]) => {
    if (!files.length) return;
    setBusy(true);
    const r = await withOverwrite((ow) => api.uploadSkill(files, scope, project, ow));
    setBusy(false);
    if (r) {
      toast(`Installed ${r.installed.join(", ")}`);
      void load();
      if (r.installed.length === 1) setOpen(r.installed[0]);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    void upload(Array.from(e.dataTransfer.files));
  };

  return (
    <div className={clsx("space-y-3 rounded-xl transition", drag && "bg-brand/5 ring-2 ring-brand/40")}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
      <div className="flex flex-wrap items-center gap-2">
        <p className="min-w-[18rem] flex-1 text-[13px] text-muted">
          A skill is a folder: <span className="font-mono">SKILL.md</span> plus optional <span className="font-mono">references/</span>,{" "}
          <span className="font-mono">scripts/</span> and <span className="font-mono">assets/</span>. The agent sees each name and description, loads
          the instructions when a task matches, and reads, runs or copies the bundled files as it needs them.
        </p>
        <div className="flex rounded-lg border border-line p-0.5 text-[12px]">
          {(["user", "project"] as const).map((s) => (
            <button key={s} onClick={() => setScope(s)} title={s === "user" ? "~/.cadence/skills" : "<project>/.cadence/skills"}
              className={clsx("rounded-md px-2 py-1", scope === s ? "bg-raised text-ink" : "text-faint hover:text-ink")}>
              {s === "user" ? "All projects" : current?.name ?? "This project"}
            </button>
          ))}
        </div>
        <input ref={zipRef} type="file" accept=".zip,.skill" multiple hidden onChange={(e) => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        {/* @ts-expect-error webkitdirectory is non-standard but supported by every major browser */}
        <input ref={dirRef} type="file" webkitdirectory="" multiple hidden onChange={(e) => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        <button className="btn-outline" onClick={() => zipRef.current?.click()} disabled={busy}><Busy on={busy}><Package size={14} /></Busy> Upload .zip / .skill</button>
        <button className="btn-outline" onClick={() => dirRef.current?.click()} disabled={busy}><FolderOpen size={14} /> Upload folder</button>
        <button className="btn-outline" onClick={() => setImporting(true)}><Download size={14} /> Import</button>
        <button className="btn-primary" onClick={() => setCreating(true)}><Plus size={14} /> New skill</button>
      </div>
      {data?.errors.map((e) => <div key={e} className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-[12.5px] text-bad">{e}</div>)}
      {data && data.skills.length === 0 && (
        <Empty icon={<Box size={18} />} title="No skills yet">
          Drop a skill folder or .zip here, import one from GitHub, or create one. New skills get references/, scripts/ and assets/ folders.
        </Empty>
      )}
      {data?.skills.map((s) => (
        <div key={s.name} className={clsx("card flex items-start gap-3 p-3.5", !s.enabled && "opacity-60")}>
          <Box size={16} className="mt-0.5 shrink-0 text-brand" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-[13.5px] font-medium">
              {s.name}
              <span className="chip border-line text-faint">{s.scope === "project" ? "this project" : s.scope === "user" ? "all projects" : "external"}</span>
              {GROUPS.map((g) => s.counts[g.id] > 0 && (
                <span key={g.id} className="chip border-line text-muted" title={g.hint}><g.icon size={10} /> {s.counts[g.id]} {g.label}</span>
              ))}
              {s.meta.license && <span className="text-[11px] text-faint">{s.meta.license}</span>}
            </div>
            <div className="mt-0.5 text-[13px] text-muted">{s.description}</div>
            {s.warnings.map((w) => <div key={w} className="mt-1 flex items-center gap-1 text-[12px] text-warn"><AlertTriangle size={12} /> {w}</div>)}
            <div className="mt-0.5 truncate font-mono text-[11px] text-faint">{s.root}</div>
          </div>
          <Toggle on={s.enabled} label="enabled" onChange={async (v) => { await act(() => api.toggleSkill(s.name, v)); void load(); }} />
          <button className="btn-ghost px-2" onClick={() => setOpen(s.name)} title={s.editable ? "Open files" : "View files"}><FolderOpen size={14} /></button>
          <a className="btn-ghost px-2" href={api.skillExportUrl(s.name, project)} title="Download as .zip"><Download size={14} /></a>
          {s.editable && <ConfirmButton onConfirm={async () => { await act(() => api.deleteSkill(s.name, project), "Skill deleted"); void load(); }} />}
        </div>
      ))}
      <NewSkill open={creating} scope={scope} onClose={() => setCreating(false)} onCreated={(name) => { void load(); setOpen(name); }} />
      <ImportSkill open={importing} scope={scope} onClose={() => setImporting(false)} onDone={(names) => { void load(); if (names.length === 1) setOpen(names[0]); }} />
      <SkillFiles name={open} row={data?.skills.find((s) => s.name === open)} onClose={() => setOpen(null)} />
    </div>
  );
}

function NewSkill({ open, scope, onClose, onCreated }: { open: boolean; scope: Scope; onClose: () => void; onCreated: (name: string) => void }) {
  const { project } = useStore();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [folders, setFolders] = useState<string[]>(["references", "scripts", "assets"]);
  useEffect(() => { if (open) { setName(""); setDescription(""); } }, [open]);
  const valid = /^[a-z0-9]+(-[a-z0-9]+)*$/.test(name);
  return (
    <Modal open={open} onClose={onClose} title="New skill"
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!valid || !description} onClick={async () => {
          if (await act(() => api.createSkill({ name, description, scope, project, folders }))) { onClose(); onCreated(name); }
        }}>Create</button></>}>
      <Field label="Name" hint="Lowercase letters, digits and hyphens. It is also the folder name.">
        <input className="input font-mono" autoFocus value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/--+/g, "-"))} placeholder="pdf-forms" />
      </Field>
      <Field label="Description" hint="What it does and when to use it. This is all the agent sees until it loads the skill (max 1024 characters).">
        <textarea className="input min-h-[70px]" maxLength={1024} value={description} onChange={(e) => setDescription(e.target.value)}
          placeholder="Fill PDF forms from structured data. Use when the user asks to complete, sign or populate a PDF form." />
      </Field>
      <div>
        <span className="label">Folders</span>
        <div className="flex gap-2">
          {GROUPS.map((g) => (
            <label key={g.id} className="flex flex-1 cursor-pointer items-start gap-2 rounded-lg border border-line px-3 py-2 text-[12.5px]">
              <input type="checkbox" className="mt-0.5 accent-[rgb(var(--brand))]" checked={folders.includes(g.id)}
                onChange={(e) => setFolders((f) => (e.target.checked ? [...f, g.id] : f.filter((x) => x !== g.id)))} />
              <span><span className="font-mono">{g.label}/</span><span className="block text-[11px] text-faint">{g.hint}</span></span>
            </label>
          ))}
        </div>
      </div>
      <p className="text-[12px] text-faint">Saved to {scope === "user" ? "~/.cadence/skills (all projects)" : "this project's .cadence/skills"}.</p>
    </Modal>
  );
}

function ImportSkill({ open, scope, onClose, onDone }: { open: boolean; scope: Scope; onClose: () => void; onDone: (names: string[]) => void }) {
  const { project } = useStore();
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal open={open} onClose={onClose} title="Import skills"
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!source.trim() || busy} onClick={async () => {
          setBusy(true);
          const r = await withOverwrite((ow) => api.importSkill(source.trim(), scope, project, ow));
          setBusy(false);
          if (r) { toast(`Installed ${r.installed.join(", ")}`); onDone(r.installed); onClose(); setSource(""); }
        }}><Busy on={busy}>Import</Busy></button></>}>
      <Field label="Source" hint={<>One of: a GitHub folder (<span className="font-mono">github.com/owner/repo/tree/main/skills/pdf</span>) or whole repo; an https link
        to a .zip, .skill or raw SKILL.md; a local folder, .zip or SKILL.md path. Every folder with a SKILL.md inside is installed with its references, scripts and assets.</>}>
        <input className="input font-mono text-[13px]" autoFocus value={source} onChange={(e) => setSource(e.target.value)}
          placeholder="https://github.com/anthropics/skills/tree/main/document-skills/pdf" />
      </Field>
    </Modal>
  );
}

function SkillFiles({ name, row, onClose }: { name: string | null; row?: SkillRow; onClose: () => void }) {
  const { project } = useStore();
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [sel, setSel] = useState("SKILL.md");
  const [file, setFile] = useState<{ binary: boolean; size: number; content?: string } | null>(null);
  const [draft, setDraft] = useState("");
  const [newPath, setNewPath] = useState<string | null>(null);
  const [uploadDir, setUploadDir] = useState("assets");
  const upRef = useRef<HTMLInputElement>(null);
  const editable = !!row?.editable;
  const dirty = !!file && !file.binary && draft !== file.content;

  const loadFiles = useCallback(() => (name ? api.skillFiles(name, project).then(setFiles).catch(() => undefined) : undefined), [name, project]);
  const openFile = useCallback(async (path: string) => {
    if (!name) return;
    setSel(path);
    const f = await act(() => api.skillFile(name, project, path));
    if (f) { setFile(f); setDraft(f.content ?? ""); }
  }, [name, project]);
  useEffect(() => { if (name) { void loadFiles(); void openFile("SKILL.md"); } }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  const grouped = useMemo(() => {
    const g: Record<string, SkillFile[]> = { references: [], scripts: [], assets: [], other: [] };
    for (const f of files) {
      if (f.dir || f.path === "SKILL.md") continue;
      const top = f.path.split("/")[0];
      (top in g && f.path.includes("/") ? g[top] : g.other).push(f);
    }
    return g;
  }, [files]);
  const dirs = new Set(files.filter((f) => f.dir).map((f) => f.path));

  const save = async () => {
    if (!name || !dirty) return;
    if (await act(() => api.saveSkillFile(name, project, sel, draft), `Saved ${sel}`)) {
      setFile((f) => f && { ...f, content: draft });
      void loadFiles();
    }
  };

  const Item = ({ f }: { f: SkillFile }) => {
    const Icon = /\.(py|sh|js|ts|rb|pl|mjs)$/.test(f.path) ? FileCode2 : /\.(png|jpe?g|gif|svg|webp|ico|ttf|otf|woff2?)$/i.test(f.path) ? ImageIcon : FileText;
    return (
      <button onClick={() => openFile(f.path)} className={clsx("flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-[12.5px]", sel === f.path ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink")}>
        <Icon size={13} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1 truncate">{f.path.includes("/") ? f.path.slice(f.path.indexOf("/") + 1) : f.path}</span>
        {f.exec && <span className="text-[10px] text-ok" title="executable">x</span>}
      </button>
    );
  };

  const Group = ({ id, label, hint, icon: Icon, children }: { id: string; label: string; hint: string; icon: typeof FileText; children: ReactNode }) => (
    <div>
      <div className="group flex items-center gap-1.5 px-2 pt-3 text-[11px] font-medium uppercase tracking-wide text-faint" title={hint}>
        <ChevronRight size={11} className="rotate-90" /><Icon size={11} /> {label}/
        {!dirs.has(id) && <span className="normal-case text-[10px] font-normal">(none)</span>}
        {editable && (
          <span className="ml-auto hidden gap-1 group-hover:flex">
            <button title={`New file in ${id}/`} onClick={() => setNewPath(`${id}/`)} className="hover:text-ink"><FilePlus2 size={12} /></button>
            <button title={`Upload into ${id}/`} onClick={() => { setUploadDir(id); upRef.current?.click(); }} className="hover:text-ink"><Upload size={12} /></button>
          </span>
        )}
      </div>
      {children}
    </div>
  );

  return (
    <Modal open={!!name} onClose={() => { if (!dirty || confirm("Discard unsaved changes?")) onClose(); }} wide title={
      <span className="flex items-center gap-2"><Box size={16} className="text-brand" /> {name}
        {!editable && <span className="chip border-line text-faint">read-only</span>}</span>}>
      <input ref={upRef} type="file" multiple hidden onChange={async (e) => {
        const list = Array.from(e.target.files ?? []);
        e.target.value = "";
        if (!name || !list.length) return;
        const r = await act(() => api.uploadSkillFiles(name, project, uploadDir, list), `Added to ${uploadDir}/`);
        if (r) { void loadFiles(); if (r.saved.length === 1) void openFile(r.saved[0]); }
      }} />
      <div className="-mx-5 -my-4 flex h-[65vh]">
        <div className="w-60 shrink-0 overflow-y-auto border-r border-line p-2">
          <button onClick={() => openFile("SKILL.md")} className={clsx("flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12.5px] font-medium", sel === "SKILL.md" ? "bg-raised text-ink" : "text-muted hover:bg-raised/60")}>
            <FileText size={13} className="text-brand" /> SKILL.md
          </button>
          {GROUPS.map((g) => <Group key={g.id} {...g}>{grouped[g.id].map((f) => <Item key={f.path} f={f} />)}</Group>)}
          {grouped.other.length > 0 && <Group id="" label="other" hint="files outside the standard folders" icon={FileText}>{grouped.other.map((f) => <Item key={f.path} f={f} />)}</Group>}
          {editable && (
            <div className="mt-3 space-y-1 border-t border-line px-1 pt-2">
              <button className="btn-ghost w-full justify-start px-2 py-1 text-[12px]" onClick={() => setNewPath("references/")}><FilePlus2 size={13} /> New file…</button>
              <button className="btn-ghost w-full justify-start px-2 py-1 text-[12px]" onClick={() => { setUploadDir("assets"); upRef.current?.click(); }}><Upload size={13} /> Upload to assets/</button>
            </div>
          )}
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line px-3">
            <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{sel}</span>
            {file && <span className="text-[11px] text-faint">{file.size.toLocaleString()} bytes{dirty ? " · unsaved" : ""}</span>}
            {name && <a className="btn-ghost px-2" href={api.skillFileUrl(name, project, sel)} title="Download"><Download size={14} /></a>}
            {editable && sel !== "SKILL.md" && name && (
              <ConfirmButton onConfirm={async () => {
                if (await act(() => api.deleteSkillFile(name, project, sel), `Deleted ${sel}`)) { void loadFiles(); void openFile("SKILL.md"); }
              }} />
            )}
            {editable && file && !file.binary && <button className="btn-primary px-3 py-1" disabled={!dirty} onClick={save}><Save size={13} /> Save</button>}
          </div>
          <div className="min-h-0 flex-1">
            {!file ? null : file.binary ? (
              /\.(png|jpe?g|gif|webp|svg)$/i.test(sel) && name ? (
                <div className="flex h-full items-center justify-center p-6"><img src={api.skillFileUrl(name, project, sel)} alt={sel} className="max-h-full max-w-full rounded shadow-card" /></div>
              ) : <Empty icon={<FileText size={18} />} title="Binary file">{file.size.toLocaleString()} bytes. The agent copies it into a project with use_skill_asset.</Empty>
            ) : (
              <CodeEditor value={draft} onChange={setDraft} onSave={save} readOnly={!editable} className="h-full rounded-none border-0" minRows={30} />
            )}
          </div>
          {sel === "SKILL.md" && (
            <div className="shrink-0 border-t border-line px-3 py-1.5 text-[11.5px] text-faint">
              Frontmatter needs <span className="font-mono">name</span> (matching the folder) and <span className="font-mono">description</span>; optional
              <span className="font-mono"> license</span>, <span className="font-mono">compatibility</span>, <span className="font-mono">allowed-tools</span>, <span className="font-mono">metadata</span>.
            </div>
          )}
        </div>
      </div>
      <Modal open={newPath !== null} onClose={() => setNewPath(null)} title="New file in the skill"
        footer={<><button className="btn-ghost" onClick={() => setNewPath(null)}>Cancel</button>
          <button className="btn-primary" disabled={!newPath || newPath.endsWith("/")} onClick={async () => {
            if (!name || !newPath) return;
            const content = newPath.endsWith(".py") ? "#!/usr/bin/env python3\n\"\"\"Usage: …\"\"\"\nimport os, sys\n\nSKILL_DIR = os.environ.get(\"SKILL_DIR\", \"\")\n"
              : newPath.endsWith(".sh") ? "#!/usr/bin/env bash\nset -euo pipefail\n" : newPath.endsWith(".md") ? `# ${newPath.split("/").pop()?.replace(/\.md$/, "")}\n\n` : "";
            if (await act(() => api.saveSkillFile(name, project, newPath, content))) {
              const p = newPath;
              setNewPath(null);
              await loadFiles();
              void openFile(p);
            }
          }}>Create</button></>}>
        <Field label="Path inside the skill" hint="e.g. references/api.md, scripts/convert.py, assets/template.html. Scripts are made executable.">
          <input className="input font-mono text-[13px]" autoFocus value={newPath ?? ""} onChange={(e) => setNewPath(e.target.value)} />
        </Field>
      </Modal>
    </Modal>
  );
}
