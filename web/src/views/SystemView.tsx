import clsx from "clsx";
import { Box, Brain, Cpu, Eye, Puzzle, RefreshCw, Shield, Webhook } from "lucide-react";
import type { ReactNode } from "react";
import { Dot, PageHeader } from "../components/ui";
import type { Status } from "../lib/api";

export function SystemView({ status, refresh }: { status: Status | null; refresh: () => void }) {
  if (!status) return <div className="p-6 text-sm text-faint">Loading…</div>;
  const s = status;
  const sources = Array.from(new Set(s.tools.map((t) => t.source)));
  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader title="System" subtitle={<span>Kestrel {s.version} · <span className="font-mono">{s.workspace}</span></span>}
        actions={<button className="btn-outline" onClick={refresh}><RefreshCw size={14} /> Refresh</button>} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-6 px-6 py-5">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Health icon={<Cpu size={16} />} title="Big model" ok={s.models.big.ok} value={s.models.big.name} detail={s.models.big.error || s.models.big.url} />
            <Health icon={<Cpu size={16} />} title="Small model" ok={s.models.small.ok} value={s.models.small.name} detail={s.models.small.error || "compaction · distillation · lessons"} />
            <Health icon={<Eye size={16} />} title="Vision" ok value={s.vision} detail="view_image · attachments" />
            <Health icon={<Brain size={16} />} title="Memory" ok={!s.memory.error} value={`${s.memory.backend} · ${s.memory.total} items`} detail={s.memory.error || s.memory.embedder} />
            <Health icon={<Shield size={16} />} title="Sandbox" ok={s.sandbox.mode !== "none"} warn={s.sandbox.mode === "none"} value={s.sandbox.mode}
              detail={s.sandbox.mode === "none" ? "bash runs unsandboxed" : s.sandbox.network ? "workspace-only writes · network on" : "workspace-only writes · no network"} />
            <Health icon={<Webhook size={16} />} title="Channels" ok={s.channels.length > 0} warn={s.channels.length === 0} value={s.channels.join(", ") || "none"} detail="Telegram / WhatsApp via config.yaml" />
          </div>

          <Section title="Tools" count={s.tools.length}>
            <div className="overflow-hidden rounded-xl border border-line">
              <table className="w-full text-left text-[13px]">
                <tbody>
                  {sources.flatMap((src) => s.tools.filter((t) => t.source === src).map((t) => (
                    <tr key={t.name} className="border-b border-line last:border-0 hover:bg-raised/40">
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[12.5px]">{t.name}</td>
                      <td className="px-3 py-2"><span className="chip border-line text-faint">{t.source}</span>{t.read_only && <span className="chip ml-1 border-ok/30 text-ok">read-only</span>}</td>
                      <td className="px-3 py-2 text-muted">{t.description}</td>
                    </tr>
                  )))}
                </tbody>
              </table>
            </div>
          </Section>

          <div className="grid gap-6 lg:grid-cols-2">
            <Section title="Plugins" count={s.plugins.length}>
              <List empty="Drop a register(api) module in ~/.kestrel/plugins">
                {s.plugins.map((p) => (
                  <Row key={p.name} icon={<Puzzle size={14} />} title={p.name} sub={p.tools.length ? `tools: ${p.tools.join(", ")}` : "hooks only"} />
                ))}
                {s.plugin_errors.map((e) => <div key={e} className="px-3 py-2 text-[12.5px] text-bad">{e}</div>)}
              </List>
            </Section>
            <Section title="Hooks" count={s.hooks.length}>
              <List empty="Add hooks: in config.yaml or from a plugin">
                {s.hooks.map((h, i) => (
                  <Row key={i} icon={<Webhook size={14} />} title={`${h.event}${h.matcher !== "*" ? ` · ${h.matcher}` : ""}`} sub={h.command} mono />
                ))}
              </List>
            </Section>
            <Section title="MCP servers" count={s.mcp.length}>
              <List empty="Add mcp_servers: in config.yaml (e.g. Playwright for browser use)">
                {s.mcp.map((m) => (
                  <Row key={m.name} icon={<Dot tone={m.connected ? "ok" : "bad"} />} title={m.name} sub={m.connected ? `${m.tools} tools` : m.error} />
                ))}
              </List>
            </Section>
            <Section title="Skills" count={s.skills.length}>
              <List empty="Add <dir>/<name>/SKILL.md to ~/.kestrel/skills">
                {s.skills.map((k) => <Row key={k.name} icon={<Box size={14} />} title={k.name} sub={k.description} />)}
              </List>
            </Section>
          </div>
        </div>
      </div>
    </div>
  );
}

function Health({ icon, title, value, detail, ok, warn }: { icon: ReactNode; title: string; value: string; detail?: string; ok?: boolean; warn?: boolean }) {
  return (
    <div className="card p-4">
      <div className="flex items-center gap-2 text-xs font-medium text-muted">
        <span className="text-faint">{icon}</span>
        {title}
        <span className="ml-auto"><Dot tone={warn ? "warn" : ok ? "ok" : "bad"} /></span>
      </div>
      <div className="mt-2 truncate font-mono text-[13px]">{value}</div>
      {detail && <div className={clsx("mt-0.5 truncate text-[12px]", ok || warn ? "text-faint" : "text-bad")}>{detail}</div>}
    </div>
  );
}

function Section({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-center gap-2 text-sm font-medium">{title}<span className="text-xs text-faint">{count}</span></div>
      {children}
    </section>
  );
}

function List({ children, empty }: { children: ReactNode; empty: string }) {
  const has = Array.isArray(children) ? children.flat().some(Boolean) : !!children;
  return <div className="divide-y divide-line overflow-hidden rounded-xl border border-line">{has ? children : <div className="px-3 py-3 text-[13px] text-faint">{empty}</div>}</div>;
}

function Row({ icon, title, sub, mono }: { icon: ReactNode; title: string; sub?: string; mono?: boolean }) {
  return (
    <div className="flex items-start gap-2.5 px-3 py-2.5">
      <span className="mt-0.5 text-faint">{icon}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-medium">{title}</div>
        {sub && <div className={clsx("truncate text-[12px] text-muted", mono && "font-mono")}>{sub}</div>}
      </div>
    </div>
  );
}
