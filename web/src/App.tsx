import { useCallback, useEffect, useState } from "react";
import { Sidebar, type View } from "./components/Sidebar";
import { api, type Status } from "./lib/api";
import { useStore } from "./lib/store";
import { ChatView } from "./views/ChatView";
import { GoalsView } from "./views/GoalsView";
import { MemoryView } from "./views/MemoryView";
import { SystemView } from "./views/SystemView";

// Routes live in the hash so refresh / back work without server routing: #/chat/<id>, #/goals, …
function parseHash(): { view: View; sid: string | null } {
  const [, view = "chat", sid = null] = location.hash.replace(/^#/, "").split("/");
  return { view: (["chat", "goals", "memory", "system"].includes(view) ? view : "chat") as View, sid };
}

function initialTheme(): "dark" | "light" {
  try {
    const saved = localStorage.getItem("kestrel.theme");
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    /* storage unavailable */
  }
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export default function App() {
  const store = useStore();
  const [route, setRoute] = useState(parseHash);
  const [status, setStatus] = useState<Status | null>(null);
  const [theme, setTheme] = useState(initialTheme);
  const [proposed, setProposed] = useState(0);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("kestrel.theme", theme);
    } catch {
      /* storage unavailable */
    }
  }, [theme]);

  const refreshStatus = useCallback(() => {
    api.status().then(setStatus).catch(() => undefined);
    api.goals().then((g) => setProposed(g.filter((x) => x.status === "proposed").length)).catch(() => undefined);
  }, []);
  useEffect(() => {
    refreshStatus();
    const t = setInterval(refreshStatus, 15000);
    return () => clearInterval(t);
  }, [refreshStatus]);
  useEffect(() => {
    if (store.liveTick) api.goals().then((g) => setProposed(g.filter((x) => x.status === "proposed").length)).catch(() => undefined);
  }, [store.liveTick]);

  const go = (view: View, sid?: string | null) => {
    location.hash = view === "chat" && sid ? `/chat/${sid}` : `/${view}`;
  };

  // Open the most recent conversation by default.
  useEffect(() => {
    if (route.view === "chat" && !route.sid && store.sessions.length) go("chat", store.sessions[0].id);
  }, [route, store.sessions]); // eslint-disable-line react-hooks/exhaustive-deps

  const newChat = async (firstMessage?: string) => {
    const sid = await store.newSession();
    go("chat", sid);
    if (firstMessage) store.send(sid, firstMessage, []);
  };

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        view={route.view}
        setView={(v) => go(v, v === "chat" ? route.sid ?? store.sessions[0]?.id : null)}
        active={route.sid}
        onOpen={(sid) => go("chat", sid)}
        onNew={() => void newChat()}
        theme={theme}
        toggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        goalBadge={proposed}
      />
      <main className="flex min-w-0 flex-1">
        {route.view === "chat" && <ChatView key={route.sid ?? "none"} sid={route.sid} status={status} onNew={(t) => void newChat(t)} />}
        {route.view === "goals" && <GoalsView status={status} refreshStatus={refreshStatus} />}
        {route.view === "memory" && <MemoryView />}
        {route.view === "system" && <SystemView status={status} refresh={refreshStatus} />}
      </main>
    </div>
  );
}
