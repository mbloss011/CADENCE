import clsx from "clsx";
import { Check, ChevronDown, CornerDownRight, Pause, Play, Plus, RotateCcw, Square, Target, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Empty, GoalBadge, PageHeader, ago } from "../components/ui";
import { api, type Goal, type Status } from "../lib/api";
import { useStore } from "../lib/store";

const FILTERS = ["all", "active", "proposed", "pending", "done", "failed"] as const;

export function GoalsView({ status, refreshStatus }: { status: Status | null; refreshStatus: () => void }) {
  const { liveTick, notices } = useStore();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => api.goals().then(setGoals).catch((e) => setError(e.message)), []);
  useEffect(() => {
    void load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => void load(), [liveTick, load]);

  const act = async (g: Goal, action: Parameters<typeof api.goalAction>[1], text?: string) => {
    setError("");
    try {
      await api.goalAction(g.id, action, text);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const d = status?.daemon;
  const shown = goals.filter((g) => filter === "all" || g.status === filter || (filter === "failed" && g.status === "blocked"));
  const count = (s: string) => goals.filter((g) => g.status === s || (s === "failed" && g.status === "blocked")).length;

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader
        title="Goals"
        subtitle={
          d ? (
            <span>
              Daemon {d.paused ? <b className="text-warn">paused</b> : "running"} · heartbeat every {d.interval_h.toFixed(1)}h ·{" "}
              {d.cycles_today}/{d.cycles_per_day} idea cycles today · {d.running.length}/{d.max_parallel} goals active
              {d.auto_approve ? " · ideas auto-approved" : " · ideas wait for approval"}
            </span>
          ) : (
            "Daemon is off — goals queue until `kestrel web` runs with the daemon."
          )
        }
        actions={
          <>
            {d && (
              <button className="btn-outline" onClick={() => api.daemon(d.paused ? "resume" : "pause").then(refreshStatus)}>
                {d.paused ? <><Play size={14} /> Resume</> : <><Pause size={14} /> Pause</>}
              </button>
            )}
            <button className="btn-primary" onClick={() => setShowForm((s) => !s)}>
              <Plus size={15} /> New goal
            </button>
          </>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 px-6 py-5">
          {showForm && <GoalForm onDone={() => { setShowForm(false); void load(); }} />}
          {error && <div className="rounded-lg bg-bad/10 px-3 py-2 text-sm text-bad">{error}</div>}

          <div className="flex flex-wrap gap-1.5">
            {FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={clsx("rounded-full border px-3 py-1 text-xs capitalize transition",
                  filter === f ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted hover:text-ink")}
              >
                {f} {f !== "all" && <span className="text-faint">{count(f)}</span>}
              </button>
            ))}
          </div>

          {shown.length === 0 ? (
            <Empty icon={<Target size={18} />} title={filter === "all" ? "No goals yet" : `No ${filter} goals`}>
              Goals run in the background with act → verify → reflect → retry. Add one, or let the daemon propose ideas.
            </Empty>
          ) : (
            <div className="space-y-2.5">
              {shown.map((g) => <GoalCard key={g.id} g={g} act={act} />)}
            </div>
          )}

          {notices.length > 0 && (
            <div className="card p-4">
              <div className="label">Activity</div>
              <ul className="space-y-1 font-mono text-[12px] text-muted">
                {notices.slice(-12).reverse().map((n, i) => (
                  <li key={i}><span className="text-faint">{new Date(n.at * 1000).toLocaleTimeString()}</span> {n.message}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function GoalCard({ g, act }: { g: Goal; act: (g: Goal, a: Parameters<typeof api.goalAction>[1], text?: string) => Promise<void> }) {
  const [open, setOpen] = useState(g.status === "active");
  const [steer, setSteer] = useState("");
  return (
    <div className={clsx("card overflow-hidden", g.status === "active" && "border-brand/40 shadow-glow")}>
      <div className="flex items-start gap-3 p-4">
        <button onClick={() => setOpen((o) => !o)} className="mt-0.5 text-faint hover:text-ink" aria-label="expand">
          <ChevronDown size={16} className={clsx("transition", !open && "-rotate-90")} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <GoalBadge status={g.status} />
            {g.source === "auto" && <span className="chip border-line text-faint">idea</span>}
            <span className="font-mono text-[11px] text-faint">{g.id}</span>
            <span className="text-[11px] text-faint">· {ago(g.updated)} · {g.attempts} attempt{g.attempts === 1 ? "" : "s"}</span>
          </div>
          <div className="mt-1.5 text-[14.5px] font-medium leading-snug">{g.title}</div>
          {g.status === "active" && g.activity && (
            <div className="mt-1 truncate font-mono text-[12px] text-brand/90">⚙ {g.activity.text}</div>
          )}
        </div>
        <div className="flex shrink-0 gap-1.5">
          {g.status === "proposed" && (
            <>
              <button className="btn-outline px-2.5 py-1 text-ok" onClick={() => act(g, "approve")}><Check size={14} /> Approve</button>
              <button className="btn-ghost px-2" onClick={() => act(g, "reject")} title="Reject"><X size={14} /></button>
            </>
          )}
          {g.status === "active" && (
            <button className="btn-danger px-2.5 py-1" onClick={() => act(g, "interrupt")}><Square size={11} fill="currentColor" /> Stop</button>
          )}
          {["failed", "blocked", "done"].includes(g.status) && (
            <button className="btn-ghost px-2.5 py-1" onClick={() => act(g, "retry")} title="Run again"><RotateCcw size={13} /> Retry</button>
          )}
        </div>
      </div>
      {open && (
        <div className="space-y-3 border-t border-line bg-bg/40 px-4 py-3 pl-11 text-[13px]">
          {g.description && g.description !== g.title && <p className="whitespace-pre-wrap text-muted">{g.description}</p>}
          {g.acceptance.length > 0 && (
            <div>
              <div className="label">Acceptance</div>
              <ul className="list-disc space-y-0.5 pl-4 text-muted">{g.acceptance.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </div>
          )}
          {g.verify_command && (
            <div>
              <div className="label">Verify</div>
              <code className="rounded bg-raised px-1.5 py-0.5 font-mono text-[12px]">{g.verify_command}</code>
            </div>
          )}
          {g.status === "active" && (
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (steer.trim()) void act(g, "steer", steer.trim()).then(() => setSteer(""));
              }}
            >
              <input className="input" placeholder="Steer this goal…" value={steer} onChange={(e) => setSteer(e.target.value)} />
              <button className="btn-outline shrink-0" disabled={!steer.trim()}><CornerDownRight size={14} /> Steer</button>
            </form>
          )}
          {g.reflections.length > 0 && (
            <div>
              <div className="label">Reflections</div>
              <ol className="space-y-1.5">
                {g.reflections.map((r) => (
                  <li key={r.attempt} className="flex gap-2">
                    <span className={clsx("chip shrink-0", r.verdict === "done" ? "border-ok/40 text-ok" : r.verdict === "blocked" ? "border-flame/40 text-flame" : "border-warn/40 text-warn")}>
                      #{r.attempt} {r.verdict}
                    </span>
                    <span className="text-muted">
                      {r.verify && <span className={clsx("mr-1.5 font-mono text-[11px]", r.verify.exit === 0 ? "text-ok" : "text-bad")}>verify exit {r.verify.exit}</span>}
                      {r.critique}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {g.result && g.status !== "active" && (
            <div>
              <div className="label">Result</div>
              <p className="whitespace-pre-wrap text-muted">{g.result}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function GoalForm({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [verify, setVerify] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  return (
    <form
      className="card space-y-3 p-4"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await api.addGoal({ title, description: description || title, verify_command: verify });
          onDone();
        } catch (x) {
          setErr((x as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <div>
        <label className="label">Goal</label>
        <input className="input" autoFocus placeholder="Fix the flaky auth test" value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div>
        <label className="label">Details (optional)</label>
        <textarea className="input min-h-[70px]" placeholder="Context, constraints, acceptance criteria…" value={description} onChange={(e) => setDescription(e.target.value)} />
      </div>
      <div>
        <label className="label">Verify command (optional — must exit 0 when done)</label>
        <input className="input font-mono" placeholder="pytest -q tests/test_auth.py" value={verify} onChange={(e) => setVerify(e.target.value)} />
      </div>
      {err && <div className="text-sm text-bad">{err}</div>}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-ghost" onClick={onDone}>Cancel</button>
        <button className="btn-primary" disabled={!title.trim() || busy}>Queue goal</button>
      </div>
    </form>
  );
}
