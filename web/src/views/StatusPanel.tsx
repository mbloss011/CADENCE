import clsx from "clsx";
import { Box, Brain, Cpu, Eye, Puzzle, RefreshCw, Shield, Webhook } from "lucide-react";
import type { ReactNode } from "react";
import { Dot } from "../components/ui";
import type { Status } from "../lib/api";

export function StatusPanel({ status, refresh }: { status: Status | null; refresh: () => void }) {
  if (!status) return <div className="p-6 text-sm text-faint">Loading…</div>;
  const s = status;
  const sources = Array.from(new Set(s.tools.map((t) => t.source)));
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between text-[13px] text-muted">
        <span>Kestrel {s.version} · <span className="font-mono">{s.workspace}</span></span>
        <button className="btn-outline" onClick={refresh}><RefreshCw size={14} /> Refresh</button>
      </div>
      {s.restart_required.length > 0 && (
        <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
          Restart Kestrel to apply: {s.restart_required.join(", ")}. Saved settings are kept.
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Health icon={<Cpu size={16} />} title="Big model" ok={s.models.big.ok} value={s.models.big.name} detail={s.models.big.error || s.models.big.url} />
        <Health icon={<Cpu size={16} />} title="Small model" ok={s.models.small.ok} value={s.models.small.name} detail={s.models.small.error || "compaction · naming · lessons"} />
        <Health icon={<Eye size={16} />} title="Vision" ok value={s.vision} detail="view_image · attachments · browser_look" />
        <Health icon={<Brain size={16} />} title="Memory" ok={!s.memory.error} value={`${s.memory.backend} · ${s.memory.total} items`} detail={s.memory.error || s.memory.embedder} />
        <Health icon={<Shield size={16} />} title="Sandbox" ok={s.sandbox.mode !== "none" && !s.sandbox.problem} warn={s.sandbox.mode === "none"} value={s.sandbox.mode}
          detail={s.sandbox.problem || (s.sandbox.mode === "none" ? "bash runs unsandboxed" : s.sandbox.network ? "project-only writes · network on" : "project-only writes · no network")} />
        <Health icon={<Webhook size={16} />} title="Channels" ok={s.channels.length > 0} warn={s.channels.length === 0} value={s.channels.join(", ") || "none"} detail="Telegram / WhatsApp via config.yaml" />
        <Health icon={<Box size={16} />} title="Extensions" ok={!s.plugin_errors.length} value={`${s.skills.length} skills · ${s.plugins.length} plugins · ${s.mcp.length} MCP`} detail={s.plugin_errors[0] || `${s.hooks.length} hooks`} />
        <Health icon={<Puzzle size={16} />} title="Daemon" ok={!!s.daemon && !s.daemon.paused} warn={!!s.daemon?.paused} value={s.daemon ? (s.daemon.paused ? "paused" : "running") : "off"}
          detail={s.daemon ? `${s.daemon.running.length} task(s) · ${s.daemon.planning.length ? "planning" : "idle planner"} · ${s.daemon.cycles_today}/${s.daemon.cycles_per_day} idea cycles` : "start `kestrel web` without --no-daemon"} />
      </div>

      <Section title="Tools in this project" count={s.tools.length}>
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
