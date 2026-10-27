"""Chat bridge shared by all messaging channels.

Each chat gets its own persistent agent session. While a turn is running, further messages from that
chat steer it instead of queueing a second turn. Slash commands manage sessions and goals.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from ..goals import Goal
from ..llm import LLMError

log = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[None]]

HELP = """Kestrel commands:
/new — start a fresh session
/stop — interrupt the running turn
/goal <text> — queue a goal for the daemon (verify → reflect → retry)
/goals — list goals
/approve <id> — approve a proposed goal
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
        self.inbox = runtime.cfg.workspace / ".kestrel" / "inbox" / channel

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
        if cmd == "/goal":
            if not arg:
                return "usage: /goal <description>"
            goal = Goal(title=arg[:80], description=arg)
            if self.rt.daemon:
                self.rt.daemon.submit(goal)
                return f"queued goal {goal.id} — I'll report back when it finishes"
            self.rt.goals.add(goal)
            return f"queued goal {goal.id} (the daemon is not running, so it will start with `kestrel daemon`)"
        if cmd == "/goals":
            goals = self.rt.goals.list()[-15:]
            return "\n".join(f"{g.id} [{g.status}] {g.title}" for g in goals) or "no goals"
        if cmd == "/approve":
            goal = self.rt.goals.get(arg)
            if not goal or goal.status != "proposed":
                return "no such proposed goal"
            self.rt.goals.update(goal, status="pending")
            if self.rt.daemon:
                self.rt.daemon.wake()
            return f"approved {goal.id}"
        if cmd == "/status":
            st = self.chats.get(chat_id)
            d = self.rt.daemon
            bits = [f"session {st.agent.session_id} ({'busy' if st.busy else 'idle'})" if st else "no session yet",
                    f"models {self.rt.cfg.big.name} / {self.rt.cfg.small.name}"]
            if d:
                bits.append(f"daemon running {len(d.running)} goal(s), cycles {d.cycles_today}/{self.rt.cfg.reflection_cycles}")
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
