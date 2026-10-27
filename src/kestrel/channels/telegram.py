"""Telegram Bot API channel (long polling — no public URL needed).

Setup: create a bot with @BotFather, put the token in an env var, and list the chat ids allowed to
use it. Message the bot once from an unlisted chat and it replies with that chat's id.

    channels:
      telegram:
        token: $TELEGRAM_BOT_TOKEN
        allowed_chats: [123456789]
        notify: true        # push daemon goal results to allowed chats
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import expand_env
from .base import Bridge, chunk

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_MESSAGE = 4000  # Telegram's hard limit is 4096


class TelegramChannel:
    name = "telegram"

    def __init__(self, bridge: Bridge, token: str, allowed_chats: set[str], *, notify: bool = False,
                 api_base: str = API, poll_timeout: int = 30, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise ValueError("telegram: 'token' is empty (set it, or the env var it points to)")
        if not allowed_chats:
            raise ValueError("telegram: 'allowed_chats' is required — the bot can run shell commands on this machine")
        self.bridge = bridge
        self.allowed = allowed_chats
        self.notify = notify
        self.poll_timeout = poll_timeout
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._file_base = f"{api_base.rstrip('/')}/file/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(poll_timeout + 15, connect=10.0), transport=transport)
        self._offset = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @classmethod
    def from_config(cls, bridge: Bridge, spec: dict[str, Any]) -> "TelegramChannel":
        return cls(bridge, str(expand_env(spec.get("token", "")) or ""),
                   {str(c) for c in spec.get("allowed_chats") or []}, notify=bool(spec.get("notify", False)),
                   api_base=str(spec.get("api_base", API)))

    async def _call(self, method: str, **params: Any) -> Any:
        resp = await self._client.post(f"{self._base}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description', resp.status_code)}")
        return data["result"]

    async def send(self, chat_id: str, text: str) -> None:
        for part in chunk(text, MAX_MESSAGE):
            try:
                await self._call("sendMessage", chat_id=chat_id, text=part)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("telegram send to %s failed: %s", chat_id, exc)
                return

    async def broadcast(self, text: str) -> None:
        if self.notify:
            for chat in self.allowed:
                await self.send(chat, text)

    async def run(self, stop: asyncio.Event) -> None:
        me = await self._call("getMe")
        log.info("telegram: polling as @%s", me.get("username"))
        while not stop.is_set():
            try:
                poll = asyncio.create_task(self._call("getUpdates", offset=self._offset, timeout=self.poll_timeout,
                                                      allowed_updates=["message"]))
                stopper = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({poll, stopper}, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
                if poll not in done:
                    poll.cancel()
                    break
                updates = poll.result()
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                log.warning("telegram poll failed: %s — retrying in 5s", exc)
                await asyncio.sleep(5)
                continue
            for upd in updates:
                self._offset = max(self._offset, int(upd["update_id"]) + 1)
                msg = upd.get("message")
                if msg:
                    task = asyncio.create_task(self._on_message(msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        for t in list(self._tasks):
            t.cancel()
        await self._client.aclose()

    async def _on_message(self, msg: dict[str, Any]) -> None:
        chat_id = str(msg["chat"]["id"])
        if chat_id not in self.allowed:
            log.warning("telegram: ignoring chat %s (not in allowed_chats)", chat_id)
            await self.send(chat_id, f"Not authorized. Add this chat id to allowed_chats: {chat_id}")
            return
        text = msg.get("text") or msg.get("caption") or ""
        images: list[Path] = []
        if msg.get("photo"):
            try:
                images.append(await self._download(msg["photo"][-1]))  # largest size is last
            except (httpx.HTTPError, RuntimeError, OSError) as exc:
                await self.send(chat_id, f"could not download the photo: {exc}")
                return
        if not text and not images:
            return
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            await self.bridge.handle(chat_id, text, lambda reply: self.send(chat_id, reply), images)
        finally:
            typing.cancel()

    async def _typing(self, chat_id: str) -> None:
        while True:
            try:
                await self._call("sendChatAction", chat_id=chat_id, action="typing")
            except (httpx.HTTPError, RuntimeError):
                pass
            await asyncio.sleep(4.5)

    async def _download(self, photo: dict[str, Any]) -> Path:
        info = await self._call("getFile", file_id=photo["file_id"])
        resp = await self._client.get(f"{self._file_base}/{info['file_path']}")
        resp.raise_for_status()
        suffix = Path(info["file_path"]).suffix or ".jpg"
        dest = self.bridge.inbox / f"{photo.get('file_unique_id', photo['file_id'])}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest
