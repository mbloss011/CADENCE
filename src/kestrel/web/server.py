"""Web GUI backend: REST for state, one WebSocket for live agent/goal events, static React build.

    kestrel web                 # http://127.0.0.1:8765 — runs chat sessions, the daemon and channels

Security: binds to loopback by default. Every request must carry a loopback (or configured) Host header,
which defeats DNS rebinding, and state-changing requests/WebSockets must come from a matching Origin,
which stops other websites in your browser from driving the agent. Binding to a non-loopback address
requires a token (auto-generated and printed if not given).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import asdict
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from ..agent import Agent, Event
from ..goals import Goal
from ..llm import LLMError, OpenAICompatLLM, content_text
from ..runtime import Runtime
from ..vision import MIME, MAX_IMAGE_BYTES

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
STEER_PREFIX = "[steering from user — adjust course accordingly] "
HOOK_MARK = "\n\n[context from hooks]\n"


# ───────────────────────────────────────────────────────────── transcripts
def transcript(agent: Agent) -> list[dict[str, Any]]:
    """Flatten an agent's context into UI items: user / steer / assistant / tool (call + result)."""
    items: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    for m in agent.ctx.messages:
        role, content = m.get("role"), m.get("content")
        if role == "user":
            text = content_text(content)
            if text.startswith(STEER_PREFIX):
                items.append({"role": "steer", "text": text[len(STEER_PREFIX):]})
                continue
            images = [p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url"] \
                if isinstance(content, list) else []
            text = text.replace("[image]", "").strip()
            items.append({"role": "user", "text": text.split(HOOK_MARK)[0], "images": images})
        elif role == "assistant":
            if content_text(content).strip():
                items.append({"role": "assistant", "text": content_text(content)})
            for tc in m.get("tool_calls") or []:
                item = {"role": "tool", "id": tc["id"], "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", ""), "result": None}
                tools[tc["id"]] = item
                items.append(item)
        elif role == "tool" and m.get("tool_call_id") in tools:
            tools[m["tool_call_id"]]["result"] = content_text(content)
    return items


def _title(agent: Agent) -> str:
    for m in agent.ctx.messages:
        if m.get("role") == "user":
            text = content_text(m.get("content")).replace("[image]", "🖼").strip()
            if not text.startswith(STEER_PREFIX):
                return text.split(HOOK_MARK)[0][:70] or "Image"
    return "New chat"


def _summary(sid: str, title: str, updated: float, running: bool, live: bool) -> dict[str, Any]:
    """Sessions started by background goals are flagged so the UI can label them."""
    kind = "goal" if title.startswith("GOAL: ") else "chat"
    return {"id": sid, "title": title.removeprefix("GOAL: "), "kind": kind, "updated": updated, "running": running, "live": live}


# ──────────────────────────────────────────────────────────────── sessions
class LiveSession:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.task: asyncio.Task[None] | None = None
        self.usage: dict[str, int] = {}
        self.updated = time.time()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def summary(self) -> dict[str, Any]:
        return _summary(self.agent.session_id, _title(self.agent), self.updated, self.running, True)


class Hub:
    """Owns live sessions and fans events out to every connected WebSocket."""

    def __init__(self, rt: Runtime):
        self.rt = rt
        self.sessions: dict[str, LiveSession] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.notices: list[dict[str, Any]] = []
        self.goal_activity: dict[str, dict[str, Any]] = {}
        self.inbox = rt.cfg.workspace / ".kestrel" / "inbox" / "web"

    async def broadcast(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    # ------------------------------------------------------------ sessions
    def session_dir(self) -> Path:
        return self.rt.cfg.state_dir / "sessions"

    def get(self, sid: str) -> LiveSession:
        live = self.sessions.get(sid)
        if live:
            return live
        agent = self.rt.new_agent("web")
        agent.load_session(sid)  # raises OSError/ValueError if unknown
        live = LiveSession(agent)
        live.updated = (self.session_dir() / f"{sid}.json").stat().st_mtime
        self.sessions[sid] = live
        return live

    def create(self) -> LiveSession:
        live = LiveSession(self.rt.new_agent("web"))
        self.sessions[live.agent.session_id] = live
        return live

    def list(self) -> list[dict[str, Any]]:
        out = {sid: s.summary() for sid, s in self.sessions.items()}
        d = self.session_dir()
        if d.is_dir():
            for f in d.glob("*.json"):
                if f.stem in out:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                title = "New chat"
                for m in data.get("messages", []):
                    if m.get("role") == "user":
                        title = content_text(m.get("content")).replace("[image]", "🖼").split(HOOK_MARK)[0].strip()[:70] or "Image"
                        break
                if data.get("messages"):
                    out[f.stem] = _summary(f.stem, title, f.stat().st_mtime, False, False)
        return sorted(out.values(), key=lambda s: s["updated"], reverse=True)

    def delete(self, sid: str) -> None:
        live = self.sessions.pop(sid, None)
        if live and live.running:
            live.agent.interrupt()
        path = self.session_dir() / f"{sid}.json"
        if path.is_file():
            path.unlink()

    # --------------------------------------------------------------- turns
    async def send(self, sid: str, text: str, images: list[Path]) -> str:
        live = self.get(sid)
        if live.running:
            live.agent.steer(text)
            await self.broadcast({"kind": "steer", "session": sid, "text": text})
            return "steered"
        live.task = asyncio.create_task(self._turn(live, text, images), name=f"web-turn-{sid}")
        return "started"

    async def _turn(self, live: LiveSession, text: str, images: list[Path]) -> None:
        sid = live.agent.session_id
        live.updated = time.time()
        await self.broadcast({"kind": "turn", "session": sid, "state": "started", "title": _title(live.agent) if live.agent.ctx.messages else text[:70]})
        try:
            async for ev in live.agent.run(text, images):
                if ev.type == "status":
                    live.usage = {"tokens": ev.data["tokens"], "window": ev.data["window"], "iteration": ev.data["iteration"]}
                await self.broadcast({"kind": "agent", "session": sid, "event": {"type": ev.type, "data": ev.data}})
                if ev.type == "tool_result" and ev.data.get("name") == "todo_write":
                    await self.broadcast({"kind": "todos", "session": sid, "todos": live.agent.todos})
        except Exception as exc:  # noqa: BLE001 — surface, never kill the server
            log.exception("turn crashed")
            await self.broadcast({"kind": "agent", "session": sid, "event": {"type": "error", "data": {"message": f"{exc.__class__.__name__}: {exc}"}}})
        finally:
            live.updated = time.time()
            live.agent.save_session()
            await self.broadcast({"kind": "turn", "session": sid, "state": "finished", "title": _title(live.agent)})

    # --------------------------------------------------------------- daemon
    async def notify(self, level: str, message: str) -> None:
        n = {"kind": "notice", "level": level, "message": message, "at": time.time()}
        self.notices = (self.notices + [n])[-100:]
        await self.broadcast(n)

    async def goal_event(self, gid: str, ev: Event) -> None:
        if ev.type in ("tool_call", "message_done", "error", "compaction"):
            act = {"type": ev.type, "at": time.time(),
                   "text": ev.data.get("name") or ev.data.get("text", "")[:200] or ev.data.get("message", "") or ev.data.get("detail", "")}
            self.goal_activity[gid] = act
            await self.broadcast({"kind": "goal_activity", "goal": gid, "activity": act})


HUB_KEY: web.AppKey["Hub"] = web.AppKey("hub")
CHANNELS_KEY: web.AppKey[list] = web.AppKey("channels")


# ──────────────────────────────────────────────────────────────── security
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "") if value else ""


def security_middleware(allowed_hosts: set[str], token: str | None):
    @web.middleware
    async def mw(request: web.Request, handler):
        host = _hostname(request.headers.get("Host", ""))
        if not (_is_loopback(host) or host in allowed_hosts):
            return web.Response(status=403, text="host not allowed")
        if token:
            supplied = request.cookies.get("kestrel_token") or request.query.get("token") or \
                request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied or "", token):
                return web.Response(status=401, text="token required: open the URL printed by `kestrel web`")
        is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
        if request.method not in ("GET", "HEAD", "OPTIONS") or is_ws:
            origin = request.headers.get("Origin")
            if origin:
                o = urlsplit(origin).hostname or ""
                if not (_is_loopback(o) or o in allowed_hosts or o == host):
                    return web.Response(status=403, text="cross-origin request refused")
        resp = await handler(request)
        if token and request.query.get("token"):
            resp.set_cookie("kestrel_token", token, httponly=True, samesite="Strict")
        return resp
    return mw


# ────────────────────────────────────────────────────────────────── routes
def _goal_dict(g: Goal, hub: Hub) -> dict[str, Any]:
    d = asdict(g)
    d["activity"] = hub.goal_activity.get(g.id)
    return d


def build_app(rt: Runtime, *, allowed_hosts: set[str] | None = None, token: str | None = None) -> web.Application:
    hub = Hub(rt)
    app = web.Application(middlewares=[security_middleware(allowed_hosts or set(), token)],
                          client_max_size=MAX_IMAGE_BYTES + 1024 * 1024)
    app[HUB_KEY] = hub
    r = web.RouteTableDef()

    @r.get("/api/status")
    async def status(_req: web.Request) -> web.Response:
        cfg, d = rt.cfg, rt.daemon
        models = {}
        for role, mc in (("big", cfg.big), ("small", cfg.small)):
            probe = OpenAICompatLLM(mc)
            try:
                models[role] = {"name": mc.name, "url": mc.base_url, "ok": mc.name in await probe.list_models()}
            except LLMError as exc:
                models[role] = {"name": mc.name, "url": mc.base_url, "ok": False, "error": str(exc)[:160]}
            finally:
                await probe.aclose()
        return web.json_response({
            "version": __import__("kestrel").__version__,
            "workspace": str(cfg.workspace),
            "models": models,
            "vision": cfg.vision.name if cfg.vision else cfg.big.name,
            "memory": {**rt.memory.stats(), "backend": cfg.memory.backend, "embedder": cfg.memory.embed_model,
                       "error": rt.memory.last_error},
            "sandbox": {"mode": rt.sandbox.mode, "network": cfg.sandbox.network},
            "plugins": [{"name": p.name, "origin": p.origin, "tools": p.tools} for p in rt.plugins.loaded.values()],
            "plugin_errors": rt.plugins.errors,
            "hooks": [{"event": h.event, "matcher": h.matcher, "command": h.command} for h in rt.hooks.shell]
                     + [{"event": h.event, "matcher": h.matcher, "command": f"plugin:{h.owner}"} for h in rt.hooks.py],
            "mcp": [{"name": n, "connected": c, "tools": t, "error": e} for n, c, t, e in rt.mcp.status()],
            "skills": [{"name": s.name, "description": s.description} for s in rt.skills.skills.values()],
            "tools": [{"name": t.name, "source": t.source, "description": t.description, "read_only": t.read_only}
                      for t in rt.registry.tools.values()],
            "channels": [c.name for c in app.get(CHANNELS_KEY, [])],
            "daemon": None if d is None else {
                "running": d.running, "cycles_today": d.cycles_today, "cycles_per_day": cfg.reflection_cycles,
                "interval_h": d.interval / 3600, "last_tick": d.last_tick, "paused": d.paused,
                "auto_approve": cfg.auto_approve_ideas, "max_parallel": cfg.max_parallel_goals},
            "notices": hub.notices[-30:],
        })

    @r.get("/api/sessions")
    async def sessions(_req: web.Request) -> web.Response:
        return web.json_response(hub.list())

    @r.post("/api/sessions")
    async def new_session(_req: web.Request) -> web.Response:
        return web.json_response(hub.create().summary())

    @r.get("/api/sessions/{sid}")
    async def get_session(req: web.Request) -> web.Response:
        try:
            live = hub.get(req.match_info["sid"])
        except (OSError, ValueError):
            raise web.HTTPNotFound(text="no such session")
        return web.json_response({**live.summary(), "items": transcript(live.agent), "todos": live.agent.todos,
                                  "usage": live.usage or {"tokens": live.agent.ctx.usage(live.agent.system_prompt()),
                                                          "window": live.agent.ctx.window},
                                  "summary": live.agent.ctx.summary, "compactions": live.agent.ctx.compactions})

    @r.delete("/api/sessions/{sid}")
    async def delete_session(req: web.Request) -> web.Response:
        hub.delete(req.match_info["sid"])
        return web.json_response({"ok": True})

    @r.post("/api/upload")
    async def upload(req: web.Request) -> web.Response:
        reader = await req.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            raise web.HTTPBadRequest(text="expected a 'file' field")
        suffix = Path(part.filename or "").suffix.lower()
        if suffix not in MIME:
            raise web.HTTPBadRequest(text=f"unsupported image type '{suffix}'")
        hub.inbox.mkdir(parents=True, exist_ok=True)
        dest = hub.inbox / f"{uuid.uuid4().hex[:12]}{suffix}"
        size = 0
        with dest.open("wb") as fh:
            while chunk := await part.read_chunk():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_IMAGE_BYTES, actual_size=size)
                fh.write(chunk)
        return web.json_response({"id": dest.name, "name": part.filename})

    @r.get("/api/goals")
    async def goals(_req: web.Request) -> web.Response:
        return web.json_response([_goal_dict(g, hub) for g in reversed(rt.goals.list())])

    @r.post("/api/goals")
    async def add_goal(req: web.Request) -> web.Response:
        body = await req.json()
        title = str(body.get("title", "")).strip()
        if not title:
            raise web.HTTPBadRequest(text="title required")
        goal = Goal(title=title[:120], description=str(body.get("description", "") or title),
                    acceptance=[str(a) for a in body.get("acceptance", []) if str(a).strip()],
                    verify_command=str(body.get("verify_command", "") or ""))
        (rt.daemon.submit(goal) if rt.daemon else rt.goals.add(goal))
        return web.json_response(_goal_dict(goal, hub))

    @r.post("/api/goals/{gid}/{action}")
    async def goal_action(req: web.Request) -> web.Response:
        goal = rt.goals.get(req.match_info["gid"])
        if not goal:
            raise web.HTTPNotFound(text="no such goal")
        action = req.match_info["action"]
        if action == "approve" and goal.status == "proposed":
            rt.goals.update(goal, status="pending")
        elif action == "reject" and goal.status in ("proposed", "pending"):
            rt.goals.update(goal, status="failed", result="rejected from the web UI")
        elif action == "retry" and goal.status in ("failed", "blocked", "done"):
            rt.goals.update(goal, status="pending")
        elif action == "interrupt":
            live = rt.runner.agents.get(goal.id)
            if live:
                live.interrupt()
        elif action == "steer":
            live = rt.runner.agents.get(goal.id)
            text = str((await req.json()).get("text", "")).strip()
            if not live or not text:
                raise web.HTTPConflict(text="goal is not running")
            live.steer(text)
        else:
            raise web.HTTPConflict(text=f"cannot {action} a {goal.status} goal")
        if rt.daemon:
            rt.daemon.wake()
        return web.json_response(_goal_dict(goal, hub))

    @r.post("/api/daemon/{action}")
    async def daemon_action(req: web.Request) -> web.Response:
        if not rt.daemon:
            raise web.HTTPConflict(text="daemon not running (start `kestrel web` without --no-daemon)")
        action = req.match_info["action"]
        if action in ("pause", "resume"):
            rt.daemon.paused = action == "pause"
            rt.daemon.wake()
        else:
            raise web.HTTPNotFound()
        return web.json_response({"paused": rt.daemon.paused})

    @r.get("/api/memory")
    async def memory(req: web.Request) -> web.Response:
        q, kind = req.query.get("q", "").strip(), req.query.get("kind") or None
        hits = await rt.memory.asearch(q, k=int(req.query.get("k", 30)), kind=kind) if q else rt.memory.recent(60, kind)[::-1]
        return web.json_response({"items": [asdict(h) for h in hits], "fallback": rt.memory.last_error if q else "",
                                  "stats": rt.memory.stats()})

    @r.post("/api/memory")
    async def add_memory(req: web.Request) -> web.Response:
        body = await req.json()
        try:
            item = rt.memory.add(str(body.get("kind", "semantic")), str(body.get("text", "")), list(body.get("tags") or []))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(asdict(item))

    @r.get("/api/ws")
    async def ws_handler(req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(req)
        hub.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data: Any = {}
                try:
                    data = json.loads(msg.data)
                    await _ws_op(hub, ws, data)
                except (ValueError, KeyError, OSError) as exc:
                    await ws.send_str(json.dumps({"kind": "error", "message": str(exc), "ref": data.get("ref") if isinstance(data, dict) else None}))
        finally:
            hub.clients.discard(ws)
        return ws

    app.add_routes(r)

    async def spa(req: web.Request) -> web.StreamResponse:
        rel = req.match_info.get("path", "")
        target = (STATIC / rel).resolve()
        if rel and target.is_file() and STATIC.resolve() in target.parents:
            return web.FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                    if "/assets/" in f"/{rel}" else {})
        index = STATIC / "index.html"
        if not index.is_file():
            return web.Response(text="Kestrel web UI is not built. Run: cd web && npm install && npm run build", status=503)
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/", spa)
    app.router.add_get("/{path:.*}", spa)
    return app


async def _ws_op(hub: Hub, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
    op, sid = data.get("op"), str(data.get("session", ""))
    ref = data.get("ref")
    if op == "send":
        text = str(data.get("text", "")).strip()
        images = []
        for name in data.get("images") or []:
            p = (hub.inbox / str(name)).resolve()
            if hub.inbox.resolve() not in p.parents or not p.is_file():
                raise ValueError(f"unknown upload {name}")
            images.append(p)
        if not text and not images:
            raise ValueError("empty message")
        state = await hub.send(sid, text or "Describe the attached image.", images)
        await ws.send_str(json.dumps({"kind": "ack", "ref": ref, "session": sid, "state": state}))
    elif op == "steer":
        live = hub.get(sid)
        text = str(data.get("text", "")).strip()
        if live.running and text:
            live.agent.steer(text)
            await hub.broadcast({"kind": "steer", "session": sid, "text": text})
    elif op == "interrupt":
        live = hub.sessions.get(sid)
        if live and live.running:
            live.agent.interrupt()
    else:
        raise ValueError(f"unknown op {op!r}")


# ───────────────────────────────────────────────────────────────── runner
async def serve(rt: Runtime, host: str, port: int, *, daemon: bool = True, channels: bool = True,
                token: str | None = None, on_ready=None) -> None:
    from ..channels import build_channels

    loopback = _is_loopback(host)
    if not loopback and not token:
        token = secrets.token_urlsafe(18)
    allowed = set() if loopback or host in ("0.0.0.0", "::") else {host}
    app = build_app(rt, allowed_hosts=allowed, token=token)
    hub = app[HUB_KEY]

    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    chans = build_channels(rt) if channels else []
    app[CHANNELS_KEY] = chans
    if daemon:
        async def notify(kind: str, message: str) -> None:
            await hub.notify(kind, message)
            if kind == "goal" and message[:1] in "✔✖":
                for ch in chans:
                    await ch.broadcast(message)
        d = rt.make_daemon(notify, hub.goal_event)
        tasks.append(asyncio.create_task(d.run_forever(), name="daemon"))
    for ch in chans:
        async def run_channel(c=ch) -> None:
            try:
                await c.run(stop)
            except Exception as exc:  # noqa: BLE001
                await hub.notify("goal", f"✖ channel {c.name} stopped: {exc}")
        tasks.append(asyncio.create_task(run_channel(), name=f"channel-{ch.name}"))

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    if on_ready:
        on_ready(url, token)
    try:
        await asyncio.Event().wait()
    finally:
        stop.set()
        if rt.daemon:
            rt.daemon.stop()
        for live in hub.sessions.values():
            if live.running:
                live.agent.interrupt()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ch in chans:
            ch.bridge.save_all()
        for ws in list(hub.clients):
            await ws.close()
        await runner.cleanup()
