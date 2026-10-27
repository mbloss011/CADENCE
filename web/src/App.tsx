import { useCallback, useEffect, useState } from "react";
import { Sidebar, VIEWS, type View } from "./components/Sidebar";
import { Toaster } from "./components/ui";
import { api, type Status } from "./lib/api";
import { useLive, useStore } from "./lib/store";
import { ChatView } from "./views/ChatView";
import { ExtensionsView } from "./views/ExtensionsView";
import { FilesView } from "./views/FilesView";
import { GoalsView } from "./views/GoalsView";
import { IdeasView } from "./views/IdeasView";
import { MemoryView } from "./views/MemoryView";
import { SettingsView } from "./views/SettingsView";
import { TasksView } from "./views/TasksView";

// Routes live in the hash so refresh / back work without server routing: #/chat/<id>, #/goals, …
function parseHash(): { view: View; sid: string | null; sub: string | null } {
  const [, view = "chat", sub = null] = location.hash.replace(/^#/, "").split("/");
  const v = (VIEWS.includes(view as View) ? view : "chat") as View;
  return { view: v, sid: v === "chat" ? sub : null, sub };
}

function initialTheme(): "dark" | "light" {
  try {
    const saved = localStorage.getItem("cadence.theme");
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
  const [badges, setBadges] = useState<Partial<Record<View, number>>>({});

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Theme: applied instantly from localStorage, then synced with the server so every browser agrees.
  useEffect(() => {
    api.settings().then((s) => {
      const t = s.ui?.theme;
      if (t === "dark" || t === "light") setTheme(t);
    }).catch(() => undefined);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("cadence.theme", theme);
    } catch {
      /* storage unavailable */
    }
  }, [theme]);
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    api.patchSettings({ ui: { theme: next } }).catch(() => undefined);
  };

  const refreshStatus = useCallback(() => {
    if (store.project) api.status(store.project).then(setStatus).catch(() => undefined);
  }, [store.project]);
  useEffect(() => {
    refreshStatus();
    const t = setInterval(refreshStatus, 20000);
    return () => clearInterval(t);
  }, [refreshStatus]);

  const refreshBadges = useCallback(() => {
    if (!store.project) return;
    Promise.all([api.ideas(store.project), api.goals(store.project), api.tasks(store.project)]).then(([ideas, goals, tasks]) =>
      setBadges({
        ideas: ideas.filter((i) => i.status === "new").length,
        goals: goals.filter((g) => g.status === "paused" && g.question).length,
        tasks: tasks.filter((t) => t.status === "proposed").length,
      }),
    ).catch(() => undefined);
  }, [store.project]);
  useEffect(refreshBadges, [refreshBadges, store.liveTick]);
  useLive(["changed"], (m) => m.what === "settings" && refreshStatus());

  const go = (view: View, sid?: string | null) => {
    location.hash = view === "chat" && sid ? `/chat/${sid}` : `/${view}`;
  };

  // Open the project's most recent conversation by default; drop a chat that belongs to another project.
  useEffect(() => {
    if (route.view !== "chat") return;
    // a chat created this session (store.chats) counts even before its first message puts it in the list
    const known = store.sessions.some((s) => s.id === route.sid) || (!!route.sid && store.chats[route.sid] !== undefined);
    if ((!route.sid || (!known && store.sessions.length)) && store.sessions.length) go("chat", store.sessions[0].id);
  }, [route, store.sessions, store.chats]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (route.view === "chat" && route.sid && store.chats[route.sid] === undefined && !store.sessions.some((s) => s.id === route.sid)) {
      // switching projects: the old chat is not in this project's list
      if (store.sessions.length === 0) location.hash = "/chat";
    }
  }, [store.project]); // eslint-disable-line react-hooks/exhaustive-deps

  const newChat = async (firstMessage?: string) => {
    const sid = await store.newSession();
    go("chat", sid);
    if (firstMessage) store.send(sid, firstMessage, []);
  };

  const discuss = (text: string) => {
    store.setDraft(text);
    void newChat();
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
        toggleTheme={toggleTheme}
        badges={badges}
      />
      <main className="flex min-w-0 flex-1" key={store.project}>
        {route.view === "chat" && <ChatView key={route.sid ?? "none"} sid={route.sid} status={status} onNew={(t) => void newChat(t)} />}
        {route.view === "files" && <FilesView onDiscuss={discuss} />}
        {route.view === "goals" && <GoalsView status={status} refreshStatus={refreshStatus} onOpenSession={(sid) => go("chat", sid)} />}
        {route.view === "tasks" && <TasksView status={status} refreshStatus={refreshStatus} onOpenSession={(sid) => go("chat", sid)} />}
        {route.view === "ideas" && <IdeasView onGo={(v) => go(v)} />}
        {route.view === "memory" && <MemoryView />}
        {route.view === "extensions" && <ExtensionsView />}
        {route.view === "settings" && <SettingsView status={status} refreshStatus={refreshStatus} />}
      </main>
      <Toaster />
    </div>
  );
}
