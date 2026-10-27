"""WhatsApp Business Cloud API channel (Meta webhook + Graph API).

Meta delivers messages to a public HTTPS webhook, so expose the local listener through a tunnel
(e.g. `cloudflared tunnel --url http://localhost:8790`) and register `<tunnel>/whatsapp` as the
webhook URL in the Meta app dashboard with the same verify token.

    channels:
      whatsapp:
        access_token: $WHATSAPP_TOKEN          # permanent system-user token
        phone_number_id: "1234567890"
        app_secret: $WHATSAPP_APP_SECRET       # verifies X-Hub-Signature-256 on every POST
        verify_token: $WHATSAPP_VERIFY_TOKEN   # any string; must match the dashboard
        allowed_numbers: ["15551234567"]       # E.164 without '+'
        host: 127.0.0.1
        port: 8790
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import expand_env
from .base import Bridge, chunk

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"
MAX_MESSAGE = 4000
MAX_BODY = 2 * 1024 * 1024
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, bridge: Bridge, *, access_token: str, phone_number_id: str, app_secret: str, verify_token: str,
                 allowed_numbers: set[str], host: str = "127.0.0.1", port: int = 8790, path: str = "/whatsapp",
                 notify: bool = False, graph_base: str = GRAPH, transport: httpx.AsyncBaseTransport | None = None):
        missing = [k for k, v in (("access_token", access_token), ("phone_number_id", phone_number_id),
                                  ("app_secret", app_secret), ("verify_token", verify_token)) if not v]
        if missing:
            raise ValueError(f"whatsapp: missing {', '.join(missing)}")
        if not allowed_numbers:
            raise ValueError("whatsapp: 'allowed_numbers' is required — the agent can run shell commands on this machine")
        self.bridge = bridge
        self.phone_number_id = phone_number_id
        self.app_secret = app_secret.encode()
        self.verify_token = verify_token
        self.allowed = allowed_numbers
        self.host, self.port, self.path = host, port, path
        self.notify = notify
        self.graph = graph_base.rstrip("/")
        self._client = httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"},
                                         timeout=httpx.Timeout(30, connect=10.0), transport=transport)
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._seen: list[str] = []  # Meta retries deliveries; dedupe by message id

    @classmethod
    def from_config(cls, bridge: Bridge, spec: dict[str, Any]) -> "WhatsAppChannel":
        e = lambda k, d="": str(expand_env(spec.get(k, d)) or "")  # noqa: E731
        return cls(bridge, access_token=e("access_token"), phone_number_id=e("phone_number_id"),
                   app_secret=e("app_secret"), verify_token=e("verify_token"),
                   allowed_numbers={str(n).lstrip("+") for n in spec.get("allowed_numbers") or []},
                   host=e("host", "127.0.0.1"), port=int(spec.get("port", 8790)), path=e("path", "/whatsapp"),
                   notify=bool(spec.get("notify", False)), graph_base=e("graph_base", GRAPH))

    # ---------------------------------------------------------------- server
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port 0 to the bound port
        log.info("whatsapp: webhook listening on http://%s:%s%s", self.host, self.port, self.path)

    async def run(self, stop: asyncio.Event) -> None:
        if self._server is None:
            await self.start()
        try:
            await stop.wait()
        finally:
            assert self._server is not None
            self._server.close()
            await self._server.wait_closed()
            for t in list(self._tasks):
                t.cancel()
            await self._client.aclose()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, body = await self._dispatch(reader)
        except (asyncio.IncompleteReadError, ValueError, asyncio.TimeoutError):
            status, body = 400, "bad request"
        except Exception:  # noqa: BLE001
            log.exception("whatsapp webhook error")
            status, body = 500, "error"
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                  413: "Payload Too Large", 500: "Internal Server Error"}.get(status, "OK")
        data = body.encode()
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _dispatch(self, reader: asyncio.StreamReader) -> tuple[int, str]:
        request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").strip()
        method, target, _ = request_line.split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        url = urlsplit(target)
        if url.path != self.path:
            return 404, "not found"
        if method == "GET":  # webhook verification handshake
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), self.verify_token):
                return 200, q.get("hub.challenge", "")
            return 403, "forbidden"
        if method != "POST":
            return 405, "method not allowed"
        length = int(headers.get("content-length", "0"))
        if length > MAX_BODY:
            return 413, "too large"
        raw = await asyncio.wait_for(reader.readexactly(length), 15)
        expected = "sha256=" + hmac.new(self.app_secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("x-hub-signature-256", ""), expected):
            log.warning("whatsapp: rejected POST with bad signature")
            return 403, "bad signature"
        for msg in self._messages(json.loads(raw)):
            task = asyncio.create_task(self._on_message(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return 200, "ok"  # ack fast; Meta retries slow webhooks

    def _messages(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in (change.get("value") or {}).get("messages", []) or []:
                    mid = msg.get("id", "")
                    if mid and mid in self._seen:
                        continue
                    self._seen = (self._seen + [mid])[-500:]
                    out.append(msg)
        return out

    # -------------------------------------------------------------- messages
    async def _on_message(self, msg: dict[str, Any]) -> None:
        sender = str(msg.get("from", "")).lstrip("+")
        if sender not in self.allowed:
            log.warning("whatsapp: ignoring message from %s (not in allowed_numbers)", sender)
            return
        kind = msg.get("type")
        text, images = "", []
        if kind == "text":
            text = msg.get("text", {}).get("body", "")
        elif kind == "image":
            text = msg.get("image", {}).get("caption", "")
            try:
                images.append(await self._download(msg["image"]["id"]))
            except (httpx.HTTPError, KeyError, OSError) as exc:
                await self.send(sender, f"could not download the image: {exc}")
                return
        else:
            await self.send(sender, f"unsupported message type '{kind}' — send text or an image")
            return
        await self.bridge.handle(sender, text, lambda reply: self.send(sender, reply), images)

    async def _download(self, media_id: str) -> Path:
        meta = await self._client.get(f"{self.graph}/{media_id}")
        meta.raise_for_status()
        info = meta.json()
        resp = await self._client.get(info["url"])
        resp.raise_for_status()
        dest = self.bridge.inbox / f"{media_id}{EXT.get(info.get('mime_type', ''), '.jpg')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    async def send(self, to: str, text: str) -> None:
        for part in chunk(text, MAX_MESSAGE):
            try:
                resp = await self._client.post(f"{self.graph}/{self.phone_number_id}/messages", json={
                    "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": part}})
                if resp.status_code >= 400:
                    log.warning("whatsapp send to %s failed: HTTP %s %s", to, resp.status_code, resp.text[:200])
                    return
            except httpx.HTTPError as exc:
                log.warning("whatsapp send to %s failed: %s", to, exc)
                return

    async def broadcast(self, text: str) -> None:
        if self.notify:
            for n in self.allowed:
                await self.send(n, text)
