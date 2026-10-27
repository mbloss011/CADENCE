import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, type ReactNode } from "react";
import { api, fromRaw, type Item, type Notice, type SessionSummary, type Todo, type Usage } from "./api";
import { socket, type ServerMsg } from "./socket";

export type Chat = {
  items: Item[];
  running: boolean;
  usage: Usage | null;
  todos: Todo[];
  compactions: number;
  loaded: boolean;
};

type State = {
  sessions: SessionSummary[];
  chats: Record<string, Chat>;
  connected: boolean;
  liveTick: number; // bumps on daemon/goal activity so views can refetch
  notices: Notice[];
};

type AgentEvent = { type: string; data: Record<string, any> };

type Action =
  | { t: "sessions"; sessions: SessionSummary[] }
  | { t: "load"; sid: string; chat: Chat }
  | { t: "send"; sid: string; text: string; images: string[] }
  | { t: "event"; sid: string; ev: AgentEvent }
  | { t: "steer"; sid: string; text: string }
  | { t: "turn"; sid: string; running: boolean; title?: string }
  | { t: "todos"; sid: string; todos: Todo[] }
  | { t: "connected"; v: boolean }
  | { t: "notice"; n: Notice }
  | { t: "tick" }
  | { t: "drop"; sid: string };

const emptyChat = (): Chat => ({ items: [], running: false, usage: null, todos: [], compactions: 0, loaded: false });

function settle(items: Item[]): Item[] {
  // close any streaming text/thinking block
  return items.map((it) => ("streaming" in it && it.streaming ? { ...it, streaming: false } : it));
}

function applyEvent(chat: Chat, ev: AgentEvent): Chat {
  const items = [...chat.items];
  const last = items[items.length - 1];
  const d = ev.data;
  switch (ev.type) {
    case "token":
      if (last?.kind === "assistant" && last.streaming) items[items.length - 1] = { ...last, text: last.text + d.text };
      else {
        const settled = settle(items);
        settled.push({ kind: "assistant", text: d.text, streaming: true });
        return { ...chat, items: settled };
      }
      return { ...chat, items };
    case "thinking":
      if (last?.kind === "thinking" && last.streaming) items[items.length - 1] = { ...last, text: last.text + d.text };
      else items.push({ kind: "thinking", text: d.text, streaming: true });
      return { ...chat, items };
    case "tool_call":
      return { ...chat, items: [...settle(items), { kind: "tool", id: d.id, name: d.name, arguments: d.arguments, result: null }] };
    case "tool_result": {
      const idx = items.findLastIndex((it) => it.kind === "tool" && it.id === d.id && it.result === null);
      if (idx >= 0) items[idx] = { ...(items[idx] as Extract<Item, { kind: "tool" }>), result: d.result };
      return { ...chat, items };
    }
    case "compaction":
      return {
        ...chat,
        compactions: chat.compactions + 1,
        items: [...items, { kind: "note", tone: "info", text: `Context compacted by the small model · ${d.stage}: ${d.detail} (${(d.before / 1000).toFixed(1)}k → ${(d.after / 1000).toFixed(1)}k tokens)` }],
      };
    case "message_done": {
      const settled = settle(items);
      const tail = settled[settled.length - 1];
      if (d.text && !(tail?.kind === "assistant")) settled.push({ kind: "assistant", text: d.text });
      return { ...chat, items: settled };
    }
    case "error":
      return { ...chat, items: [...settle(items), { kind: "note", tone: "bad", text: d.message }] };
    case "interrupted":
      return { ...chat, items: [...settle(items), { kind: "note", tone: "warn", text: "Interrupted — context kept. Send a message to continue or change course." }] };
    case "status":
      return { ...chat, usage: { tokens: d.tokens, window: d.window, iteration: d.iteration } };
    default:
      return chat;
  }
}

function reducer(s: State, a: Action): State {
  const chat = (sid: string) => s.chats[sid] ?? emptyChat();
  const put = (sid: string, c: Chat): State => ({ ...s, chats: { ...s.chats, [sid]: c } });
  switch (a.t) {
    case "sessions":
      return { ...s, sessions: a.sessions };
    case "load":
      return put(a.sid, a.chat);
    case "send": {
      const c = chat(a.sid);
      return put(a.sid, { ...c, running: true, items: [...settle(c.items), { kind: "user", text: a.text, images: a.images }] });
    }
    case "event":
      return put(a.sid, applyEvent(chat(a.sid), a.ev));
    case "steer": {
      const c = chat(a.sid);
      return put(a.sid, { ...c, items: [...settle(c.items), { kind: "steer", text: a.text }] });
    }
    case "turn": {
      const c = chat(a.sid);
      const sessions = s.sessions.some((x) => x.id === a.sid)
        ? s.sessions.map((x) => (x.id === a.sid ? { ...x, running: a.running, title: a.title || x.title, updated: Date.now() / 1000 } : x))
        : [{ id: a.sid, title: a.title || "New chat", updated: Date.now() / 1000, running: a.running, live: true }, ...s.sessions];
      return { ...s, sessions, chats: { ...s.chats, [a.sid]: { ...c, running: a.running, items: a.running ? c.items : settle(c.items) } } };
    }
    case "todos":
      return put(a.sid, { ...chat(a.sid), todos: a.todos });
    case "connected":
      return { ...s, connected: a.v };
    case "notice":
      return { ...s, notices: [...s.notices, a.n].slice(-50), liveTick: s.liveTick + 1 };
    case "tick":
      return { ...s, liveTick: s.liveTick + 1 };
    case "drop": {
      const chats = { ...s.chats };
      delete chats[a.sid];
      return { ...s, chats, sessions: s.sessions.filter((x) => x.id !== a.sid) };
    }
  }
}

type Ctx = State & {
  refreshSessions: () => Promise<void>;
  loadChat: (sid: string) => Promise<void>;
  send: (sid: string, text: string, uploads: { id: string; preview: string }[]) => void;
  steer: (sid: string, text: string) => void;
  interrupt: (sid: string) => void;
  remove: (sid: string) => Promise<void>;
  newSession: () => Promise<string>;
};

const StoreCtx = createContext<Ctx | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, { sessions: [], chats: {}, connected: false, liveTick: 0, notices: [] });

  const refreshSessions = useCallback(async () => {
    dispatch({ t: "sessions", sessions: await api.sessions() });
  }, []);

  useEffect(() => {
    const offState = socket.onState((v) => dispatch({ t: "connected", v }));
    const off = socket.on((m: ServerMsg) => {
      const sid = m.session as string;
      switch (m.kind) {
        case "agent":
          dispatch({ t: "event", sid, ev: m.event as AgentEvent });
          break;
        case "turn":
          dispatch({ t: "turn", sid, running: m.state === "started", title: m.title as string });
          break;
        case "steer":
          dispatch({ t: "steer", sid, text: m.text as string });
          break;
        case "todos":
          dispatch({ t: "todos", sid, todos: m.todos as Todo[] });
          break;
        case "notice":
          dispatch({ t: "notice", n: { level: m.level as string, message: m.message as string, at: m.at as number } });
          break;
        case "goal_activity":
          dispatch({ t: "tick" });
          break;
      }
    });
    socket.connect();
    refreshSessions().catch(() => undefined);
    return () => {
      off();
      offState();
    };
  }, [refreshSessions]);

  const loadChat = useCallback(async (sid: string) => {
    const d = await api.session(sid);
    dispatch({
      t: "load",
      sid,
      chat: { items: fromRaw(d.items), running: d.running, usage: d.usage, todos: d.todos, compactions: d.compactions, loaded: true },
    });
  }, []);

  const value = useMemo<Ctx>(
    () => ({
      ...state,
      refreshSessions,
      loadChat,
      send: (sid, text, uploads) => {
        const running = state.chats[sid]?.running;
        if (!running) dispatch({ t: "send", sid, text, images: uploads.map((u) => u.preview) });
        socket.send({ op: "send", session: sid, text, images: uploads.map((u) => u.id) });
      },
      steer: (sid, text) => socket.send({ op: "steer", session: sid, text }),
      interrupt: (sid) => socket.send({ op: "interrupt", session: sid }),
      remove: async (sid) => {
        await api.deleteSession(sid);
        dispatch({ t: "drop", sid });
      },
      newSession: async () => {
        const s = await api.newSession();
        dispatch({ t: "load", sid: s.id, chat: { ...emptyChat(), loaded: true } });
        return s.id;
      },
    }),
    [state, refreshSessions, loadChat],
  );

  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>;
}

export function useStore() {
  const ctx = useContext(StoreCtx);
  if (!ctx) throw new Error("useStore outside StoreProvider");
  return ctx;
}
