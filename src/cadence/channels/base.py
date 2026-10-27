"""Chat bridge shared by all messaging channels.

Each chat gets its own persistent agent session. While a turn is running, further messages from that
chat steer it instead of queueing a second turn. Slash commands manage sessions, tasks, goals and ideas.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from ..goals import Goal
from ..ideas import promote_to_task
from ..tasks import Task
from ..llm import LLMError

log = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[None]]

HELP = """Cadence commands:
/new — start a fresh session
/stop — interrupt the running turn
/task <text> — queue a task for the daemon (verify → reflect → retry)
/goal <text> — set a long-horizon goal the planner works toward
/goals · /tasks · /ideas — list them
/approve <id> — promote an idea to a task (or approve a proposed task)
/status — session and daemon status
Anything else is sent to the agent. Photos are passed to the vision model."""


class Channel(Protocol):
    name: str

    async def run(self, stop: asyncio.Event) -> None: ...

    async def broadcast(self, text: str) -> None: ...


@dataclass
class _ChatState:
    agent: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False


def chunk(text: str, size: int) -> list[str]:
    """Split on paragraph/line boundaries where possible so messages stay readable."""
    text = text.strip() or "(empty reply)"
    out: list[str] = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size))
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


class Bridge:
    def __init__(self, runtime: Any, channel: str):
        self.rt = runtime
        self.channel = channel
        self.chats: dict[str, _ChatState] = {}
        self.inbox = runtime.cfg.workspace / ".cadence" / "inbox" / channel

    def _state(self, chat_id: str) -> _ChatState:
        st = self.chats.get(chat_id)
        if st is None:
            st = _ChatState(self.rt.new_agent(f"{self.channel}:{chat_id}"))
            self.chats[chat_id] = st
        return st

    async def handle(self, chat_id: str, text: str, reply: Reply, images: list[Path] | None = None) -> None:
        text = (text or "").strip()
        if text.startswith("/") and not images:
            await reply(await self._command(chat_id, text))
            return
        st = self._state(chat_id)
        if st.busy:
            st.agent.steer(text or "(see attached image)")
            await reply("↪ steering the running turn")
            return
        async with st.lock:
            st.busy = True
            try:
                final = await st.agent.run_to_completion(text or "Describe the attached image.", images)
            except LLMError as exc:
                final = f"model unavailable: {exc}"
            except Exception as exc:  # noqa: BLE001 — report, never kill the channel loop
                log.exception("turn failed for %s:%s", self.channel, chat_id)
                final = f"error: {exc.__class__.__name__}: {exc}"
            finally:
                st.busy = False
        await reply(final or "(no reply)")

    async def _command(self, chat_id: str, line: str) -> str:
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.split("@", 1)[0].lower(), arg.strip()  # Telegram appends @botname in groups
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/new":
            old = self.chats.pop(chat_id, None)
            if old:
                old.agent.save_session()
            return "fresh session started"
        if cmd == "/stop":
            st = self.chats.get(chat_id)
            if st and st.busy:
                st.agent.interrupt()
                return "⏹ interrupting"
            return "nothing is running"
        if cmd == "/task":
            if not arg:
                return "usage: /task <description>"
            task = Task(title=arg[:80], description=arg)
            if self.rt.daemon:
                self.rt.daemon.submit(task)
                return f"queued task {task.id} — I'll report back when it finishes"
            self.rt.tasks.add(task)
            return f"queued task {task.id} (the daemon is not running, so it will start with `cadence daemon`)"
        if cmd == "/goal":
            if not arg:
                return "usage: /goal <what you want to achieve>"
            goal = self.rt.goals.add(Goal(title=arg[:160], description=arg))
            if self.rt.daemon:
                self.rt.daemon.wake()
            return f"goal {goal.id} set — I'll plan tasks toward it and report progress"
        if cmd == "/goals":
            goals = [g for g in self.rt.goals.all() if g.status in ("active", "paused")][-15:]
            return "\n".join(f"{g.id} [{g.status} {g.progress}%] {g.title}" + (f"\n  ? {g.question}" if g.question else "")
                             for g in goals) or "no goals"
        if cmd == "/tasks":
            tasks = self.rt.tasks.list()[-15:]
            return "\n".join(f"{t.id} [{t.status}] {t.title}" for t in tasks) or "no tasks"
        if cmd == "/ideas":
            ideas = self.rt.ideas.list("new")[-15:]
            return "\n".join(f"{i.id} ({i.size}) {i.title}" for i in ideas) or "no new ideas"
        if cmd == "/approve":
            idea = self.rt.ideas.get(arg)
            if idea and idea.status == "new":
                task = promote_to_task(self.rt.ideas, self.rt.tasks, idea)
                msg = f"idea {idea.id} → task {task.id}"
            else:
                task = self.rt.tasks.get(arg)
                if not task or task.status != "proposed":
                    return "no such new idea or proposed task"
                self.rt.tasks.update(task, status="pending")
                msg = f"approved {task.id}"
            if self.rt.daemon:
                self.rt.daemon.wake()
            return msg
        if cmd == "/status":
            st = self.chats.get(chat_id)
            d = self.rt.daemon
            bits = [f"session {st.agent.session_id} ({'busy' if st.busy else 'idle'})" if st else "no session yet",
                    f"models {self.rt.cfg.big.name} / {self.rt.cfg.small.name}"]
            if d:
                bits.append(f"daemon running {len(d.running)} task(s), cycles {d.cycles_today}/{self.rt.cfg.reflection_cycles}")
            return "\n".join(bits)
        return f"unknown command {cmd} — /help"

    def save_all(self) -> None:
        for st in self.chats.values():
            st.agent.save_session()


def build_channels(runtime: Any) -> list[Channel]:
    """Instantiate every enabled channel from `cfg.channels`. Raises ValueError on bad config."""
    from .telegram import TelegramChannel
    from .whatsapp import WhatsAppChannel

    out: list[Channel] = []
    for name, spec in runtime.cfg.channels.items():
        if not spec.get("enabled", True):
            continue
        if name == "telegram":
            out.append(TelegramChannel.from_config(Bridge(runtime, "telegram"), spec))
        elif name == "whatsapp":
            out.append(WhatsAppChannel.from_config(Bridge(runtime, "whatsapp"), spec))
        else:
            raise ValueError(f"unknown channel '{name}' (supported: telegram, whatsapp)")
    return out
