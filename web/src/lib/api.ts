export type Todo = { content: string; status: "pending" | "in_progress" | "done" };

export type Item =
  | { kind: "user"; text: string; images: string[] }
  | { kind: "steer"; text: string }
  | { kind: "assistant"; text: string; streaming?: boolean }
  | { kind: "thinking"; text: string; streaming?: boolean }
  | { kind: "tool"; id: string; name: string; arguments: string; result: string | null }
  | { kind: "note"; tone: "info" | "warn" | "bad"; text: string };

export type SessionSummary = { id: string; title: string; kind?: "chat" | "goal"; updated: number; running: boolean; live: boolean };

export type Usage = { tokens: number; window: number; iteration?: number };

export type SessionDetail = SessionSummary & {
  items: RawItem[];
  todos: Todo[];
  usage: Usage;
  summary: string;
  compactions: number;
};

type RawItem =
  | { role: "user"; text: string; images: string[] }
  | { role: "steer"; text: string }
  | { role: "assistant"; text: string }
  | { role: "tool"; id: string; name: string; arguments: string; result: string | null };

export function fromRaw(raw: RawItem[]): Item[] {
  return raw.map((r) => {
    if (r.role === "tool") return { kind: "tool", id: r.id, name: r.name, arguments: r.arguments, result: r.result };
    if (r.role === "user") return { kind: "user", text: r.text, images: r.images };
    return { kind: r.role, text: r.text } as Item;
  });
}

export type Goal = {
  id: string;
  title: string;
  description: string;
  acceptance: string[];
  verify_command: string;
  status: "proposed" | "pending" | "active" | "done" | "failed" | "blocked";
  source: "user" | "auto";
  attempts: number;
  reflections: { attempt: number; verdict: string; critique: string; verify: { exit: number; output: string } | null; lessons?: string[] }[];
  result: string;
  created: number;
  updated: number;
  activity: { type: string; text: string; at: number } | null;
};

export type MemoryItem = { id: string; kind: "episodic" | "semantic" | "procedural"; text: string; tags: string[]; created: number; hits: number };

export type Notice = { level: string; message: string; at: number };

export type Status = {
  version: string;
  workspace: string;
  models: Record<"big" | "small", { name: string; url: string; ok: boolean; error?: string }>;
  vision: string;
  memory: { total: number; episodic: number; semantic: number; procedural: number; indexed: number | null; backend: string; embedder: string; error: string };
  sandbox: { mode: string; network: boolean };
  plugins: { name: string; origin: string; tools: string[] }[];
  plugin_errors: string[];
  hooks: { event: string; matcher: string; command: string }[];
  mcp: { name: string; connected: boolean; tools: number; error: string }[];
  skills: { name: string; description: string }[];
  tools: { name: string; source: string; description: string; read_only: boolean }[];
  channels: string[];
  daemon: null | {
    running: string[];
    cycles_today: number;
    cycles_per_day: number;
    interval_h: number;
    last_tick: string;
    paused: boolean;
    auto_approve: boolean;
    max_parallel: number;
  };
  notices: Notice[];
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json", ...init?.headers } : init?.headers,
  });
  if (!res.ok) throw new Error((await res.text()) || `${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  status: () => req<Status>("/api/status"),
  sessions: () => req<SessionSummary[]>("/api/sessions"),
  newSession: () => req<SessionSummary>("/api/sessions", { method: "POST" }),
  session: (id: string) => req<SessionDetail>(`/api/sessions/${id}`),
  deleteSession: (id: string) => req<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" }),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req<{ id: string; name: string }>("/api/upload", { method: "POST", body: fd });
  },
  goals: () => req<Goal[]>("/api/goals"),
  addGoal: (g: { title: string; description?: string; verify_command?: string; acceptance?: string[] }) =>
    req<Goal>("/api/goals", { method: "POST", body: JSON.stringify(g) }),
  goalAction: (id: string, action: "approve" | "reject" | "retry" | "interrupt" | "steer", text?: string) =>
    req<Goal>(`/api/goals/${id}/${action}`, { method: "POST", body: JSON.stringify({ text }) }),
  daemon: (action: "pause" | "resume") => req<{ paused: boolean }>(`/api/daemon/${action}`, { method: "POST" }),
  memory: (q: string, kind?: string) =>
    req<{ items: MemoryItem[]; fallback: string; stats: Status["memory"] }>(
      `/api/memory?${new URLSearchParams({ q, ...(kind ? { kind } : {}) })}`,
    ),
  addMemory: (kind: string, text: string) => req<MemoryItem>("/api/memory", { method: "POST", body: JSON.stringify({ kind, text }) }),
};
