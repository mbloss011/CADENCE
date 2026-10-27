"""Routes for settings, model discovery and extensions (skills, plugins, MCP servers, hooks).

Every change is written to ~/.cadence/settings.json (same keys as config.yaml) and applied to the
running process, so the UI and the next restart always agree. Skill and plugin *files* live in
~/.cadence/skills and ~/.cadence/plugins (or the project's .cadence/ directory for project scope).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from aiohttp import web

from ..config import ENV_LOCKS, HookConfig, ModelConfig, read_settings
from ..hooks import EVENTS, Hooks
from ..llm import discover_models
from ..sandbox import MODES
from ..skills import SkillRegistry

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .server import Hub

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SKILL_BYTES = 256 * 1024

PLUGIN_TEMPLATE = '''"""{name}: a Cadence plugin. Edit and save; it reloads immediately."""


def register(api):
    @api.tool("{name}_hello", "Say hello (replace me with something useful).",
              {{"type": "object", "properties": {{"who": {{"type": "string"}}}}}}, read_only=True)
    def hello(args):
        return f"hello, {{args.get('who', 'world')}} from {name}"

    # Hooks: return a string to add context, or HookResult(block=True, reason=...) to refuse.
    # from cadence.plugins import HookResult
    # @api.on("pre_tool", matcher="bash")
    # def guard(payload):
    #     if "rm -rf /" in payload["arguments"].get("command", ""):
    #         return HookResult(block=True, reason="not on my watch")
'''

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

Describe when to use this skill and the steps to follow.
"""


def _model_dict(m: ModelConfig | None) -> dict[str, Any] | None:
    if m is None:
        return None
    d = asdict(m)
    d["has_key"] = bool(d.pop("api_key", ""))
    return d


def routes(r: web.RouteTableDef, rt: "Runtime", hub: "Hub") -> None:
    from .server import body

    cfg = rt.cfg

    async def changed(what: str) -> None:
        await hub.broadcast({"kind": "changed", "what": what})

    # ─────────────────────────────────────────────────────────── settings
    def settings_view() -> dict[str, Any]:
        return {
            "big": _model_dict(cfg.big), "small": _model_dict(cfg.small), "vision": _model_dict(cfg.vision),
            "memory": asdict(cfg.memory), "sandbox": asdict(cfg.sandbox),
            "agent": {k: getattr(cfg, k) for k in ("max_iterations", "tool_timeout", "allow_shell", "tool_output_limit",
                                                     "compact_trigger", "keep_recent_messages")},
            "daemon": {k: getattr(cfg, k) for k in ("reflection_cycles", "auto_approve_ideas", "max_parallel_goals",
                                                      "goal_daily_tasks", "max_reflections_per_goal")},
            "providers": [{"name": p.name, "kind": p.kind, "url": p.url, "has_key": bool(p.api_key)} for p in cfg.providers],
            "browser": {"enabled": True, "headless": True, "executable": "", **cfg.browser},
            "ui": read_settings(cfg).get("ui", {}),
            "locked": {path: env for env, path in ENV_LOCKS.items() if os.environ.get(env)},
            "restart_required": sorted(rt.restart_required),
            "paths": {"settings": str(cfg.settings_path), "config": str(cfg.home / "config.yaml")},
        }

    @r.get("/api/settings")
    async def get_settings(_req: web.Request) -> web.Response:
        return web.json_response(settings_view())

    @r.patch("/api/settings")
    async def patch_settings(req: web.Request) -> web.Response:
        data = await body(req)
        patch: dict[str, Any] = {}
        model_keys = {f.name for f in fields(ModelConfig)}
        for role in ("big", "small", "vision"):
            if role in data:
                if role == "vision" and not data[role]:
                    patch["vision"] = None
                    continue
                m = {k: v for k, v in (data[role] or {}).items() if k in model_keys}
                if "base_url" in m:
                    url = str(m["base_url"]).rstrip("/")
                    m["base_url"] = url if url.endswith("/v1") else f"{url}/v1"
                for k in ("temperature", "timeout"):
                    if k in m:
                        m[k] = float(m[k])
                for k in ("max_tokens", "context_window"):
                    if k in m:
                        m[k] = int(m[k])
                if "name" in m and not str(m["name"]).strip():
                    raise web.HTTPBadRequest(text=f"{role} model name cannot be empty")
                patch[role] = m
        if "memory" in data:
            mem = dict(data["memory"] or {})
            if mem.get("backend", cfg.memory.backend) not in ("local", "qdrant", "keyword"):
                raise web.HTTPBadRequest(text="memory.backend must be local, qdrant or keyword")
            patch["memory"] = mem
        if "sandbox" in data:
            sb = dict(data["sandbox"] or {})
            if sb.get("mode", cfg.sandbox.mode) not in MODES:
                raise web.HTTPBadRequest(text=f"sandbox.mode must be one of {', '.join(MODES)}")
            patch["sandbox"] = sb
        for group in ("agent", "daemon"):
            for k, v in (data.get(group) or {}).items():
                cur = getattr(cfg, k, None)
                if cur is None:
                    continue
                patch[k] = bool(v) if isinstance(cur, bool) else type(cur)(v)
        if "providers" in data:
            old = {p.url: p.api_key for p in cfg.providers}
            provs = []
            for p in data["providers"] or []:
                if str(p.get("url", "")).strip():
                    url = str(p["url"]).strip().rstrip("/")
                    provs.append({"name": str(p.get("name") or p.get("kind") or "server"), "kind": str(p.get("kind", "openai")),
                                  "url": url, "api_key": str(p.get("api_key") or old.get(url, ""))})
            patch["providers"] = provs
        if "browser" in data:
            patch["browser"] = dict(data["browser"] or {})
        restart = await rt.apply_settings(patch) if patch else set(rt.restart_required)
        if "ui" in data:  # UI preferences: stored, never applied to the agent
            from ..config import write_settings
            stored = read_settings(cfg)
            stored["ui"] = {**stored.get("ui", {}), **(data["ui"] or {})}
            write_settings(cfg, stored)
        if patch:
            await changed("settings")
        return web.json_response({**settings_view(), "restart_required": sorted(restart)})

    # ──────────────────────────────────────────────────────────── models
    @r.get("/api/models")
    async def models(_req: web.Request) -> web.Response:
        found = await discover_models(cfg.providers)
        return web.json_response({"providers": found, "current": {
            "big": {"name": cfg.big.name, "base_url": cfg.big.base_url},
            "small": {"name": cfg.small.name, "base_url": cfg.small.base_url},
            "vision": {"name": cfg.vision.name, "base_url": cfg.vision.base_url} if cfg.vision else None,
            "embed": {"name": cfg.memory.embed_model, "base_url": cfg.memory.embed_url or cfg.big.base_url}}})

    # ──────────────────────────────────────────────────────────── skills
    def user_skill_dir() -> Path:
        return cfg.home / "skills"

    def scope_skills(req: web.Request) -> tuple[SkillRegistry, Path]:
        sc = rt.scope(req.query.get("project", ""))
        sc.skills.scan()
        return sc.skills, sc.project.root / ".cadence" / "skills"

    def writable(path: Path, project_dir: Path) -> bool:
        root = path.resolve()
        return any(base.resolve() in root.parents for base in (user_skill_dir(), project_dir))

    def skill_dict(s: Any, reg: SkillRegistry, project_dir: Path) -> dict[str, Any]:
        where = "project" if project_dir.resolve() in s.path.resolve().parents else \
            "user" if user_skill_dir().resolve() in s.path.resolve().parents else "other"
        return {"name": s.name, "description": s.description, "path": str(s.path), "scope": where,
                "enabled": s.name not in cfg.disabled_skills, "editable": writable(s.path, project_dir), "files": s.files}

    @r.get("/api/skills")
    async def skills(req: web.Request) -> web.Response:
        reg, pdir = scope_skills(req)
        return web.json_response({"skills": [skill_dict(s, reg, pdir) for s in reg.found.values()], "errors": reg.errors,
                                  "dirs": [str(d) for d in reg.dirs]})

    @r.get("/api/skills/{name}")
    async def skill_source(req: web.Request) -> web.Response:
        reg, pdir = scope_skills(req)
        s = reg.found.get(req.match_info["name"])
        if not s:
            raise web.HTTPNotFound(text="no such skill")
        return web.json_response({**skill_dict(s, reg, pdir), "content": s.path.read_text(encoding="utf-8")})

    def check_skill(path: Path) -> str:
        try:
            SkillRegistry._parse(path)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return ""

    @r.post("/api/skills")
    async def create_skill(req: web.Request) -> web.Response:
        data = await body(req)
        name = str(data.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise web.HTTPBadRequest(text="name: lowercase letters, digits, - and _ (max 64)")
        description = str(data.get("description", "")).strip().replace("\n", " ")
        if not description:
            raise web.HTTPBadRequest(text="a description is required: it is how the agent decides to use the skill")
        base = user_skill_dir() if data.get("scope", "user") == "user" else rt.scope(str(data.get("project", ""))).project.root / ".cadence" / "skills"
        target = base / name / "SKILL.md"
        if target.exists():
            raise web.HTTPConflict(text=f"{target} already exists")
        content = str(data.get("content", "")) or SKILL_TEMPLATE.format(name=name, description=description, title=name.replace("-", " ").title())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if err := check_skill(target):
            shutil.rmtree(target.parent)
            raise web.HTTPBadRequest(text=f"invalid SKILL.md: {err}")
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True, "path": str(target)})

    @r.put("/api/skills/{name}")
    async def save_skill(req: web.Request) -> web.Response:
        reg, pdir = scope_skills(req)
        s = reg.found.get(req.match_info["name"])
        if not s:
            raise web.HTTPNotFound(text="no such skill")
        if not writable(s.path, pdir):
            raise web.HTTPForbidden(text=f"{s.path} is outside the editable skill folders")
        content = str((await body(req)).get("content", ""))
        if len(content.encode()) > MAX_SKILL_BYTES:
            raise web.HTTPBadRequest(text="SKILL.md is too large")
        old = s.path.read_text(encoding="utf-8")
        s.path.write_text(content, encoding="utf-8")
        if err := check_skill(s.path):
            s.path.write_text(old, encoding="utf-8")
            raise web.HTTPBadRequest(text=f"not saved, invalid SKILL.md: {err}")
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True})

    @r.delete("/api/skills/{name}")
    async def delete_skill(req: web.Request) -> web.Response:
        reg, pdir = scope_skills(req)
        s = reg.found.get(req.match_info["name"])
        if not s:
            raise web.HTTPNotFound(text="no such skill")
        if not writable(s.path, pdir):
            raise web.HTTPForbidden(text=f"{s.path} is outside the editable skill folders; disable it instead")
        shutil.rmtree(s.path.parent)
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True})

    @r.post("/api/skills/{name}/toggle")
    async def toggle_skill(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        enabled = bool((await body(req)).get("enabled", True))
        disabled = [n for n in cfg.disabled_skills if n != name] + ([] if enabled else [name])
        await rt.apply_settings({"skills": {"disabled": disabled}})
        await changed("skills")
        return web.json_response({"ok": True, "disabled": disabled})

    @r.post("/api/skills-import")
    async def import_skill(req: web.Request) -> web.Response:
        """From a local folder containing SKILL.md, or an http(s) URL of a raw SKILL.md."""
        data = await body(req)
        src = str(data.get("source", "")).strip()
        base = user_skill_dir() if data.get("scope", "user") == "user" else rt.scope(str(data.get("project", ""))).project.root / ".cadence" / "skills"
        tmp = base / f".import-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            if src.startswith(("http://", "https://")):
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    resp = await client.get(src)
                    resp.raise_for_status()
                    if len(resp.content) > MAX_SKILL_BYTES:
                        raise web.HTTPBadRequest(text="that file is too large to be a SKILL.md")
                tmp.mkdir(parents=True)
                (tmp / "SKILL.md").write_bytes(resp.content)
            else:
                folder = Path(src).expanduser()
                if folder.is_file() and folder.name == "SKILL.md":
                    folder = folder.parent
                if not (folder / "SKILL.md").is_file():
                    raise web.HTTPBadRequest(text=f"no SKILL.md in {folder}")
                shutil.copytree(folder, tmp, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"))
            import yaml
            text = (tmp / "SKILL.md").read_text(encoding="utf-8")
            meta = yaml.safe_load(text.split("---")[1]) if text.startswith("---") else {}
            name = str((meta or {}).get("name", "")).strip()
            if not NAME_RE.match(name):
                raise web.HTTPBadRequest(text="SKILL.md frontmatter needs a valid 'name'")
            dest = base / name
            if dest.exists():
                raise web.HTTPConflict(text=f"a skill named '{name}' already exists in {base}")
            tmp.rename(dest)
            if err := check_skill(dest / "SKILL.md"):
                shutil.rmtree(dest)
                raise web.HTTPBadRequest(text=f"invalid SKILL.md: {err}")
        except httpx.HTTPError as exc:
            raise web.HTTPBadGateway(text=f"download failed: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True, "name": name})

    # ─────────────────────────────────────────────────────────── plugins
    def plugin_dir() -> Path:
        return cfg.home / "plugins"

    def plugin_rows() -> list[dict[str, Any]]:
        rows = []
        for name, origin, _loader in rt.plugins.discover():
            loaded = rt.plugins.loaded.get(name)
            err = next((e for e in rt.plugins.errors if e.startswith(f"{name} (")), "")
            path = rt.plugins.path_of(name)
            rows.append({"name": name, "origin": origin, "enabled": name not in cfg.disabled_plugins,
                         "loaded": loaded is not None, "tools": loaded.tools if loaded else [], "hooks": loaded.hooks if loaded else 0,
                         "error": err, "editable": path is not None, "path": str(path) if path else ""})
        return rows

    @r.get("/api/plugins")
    async def plugins(_req: web.Request) -> web.Response:
        return web.json_response({"plugins": plugin_rows(), "dirs": [str(d) for d in rt.plugins.dirs]})

    @r.post("/api/plugins")
    async def create_plugin(req: web.Request) -> web.Response:
        data = await body(req)
        name = str(data.get("name", "")).strip().lower().replace("-", "_")
        if not NAME_RE.match(name):
            raise web.HTTPBadRequest(text="name: lowercase letters, digits and _")
        if rt.plugins.path_of(name):
            raise web.HTTPConflict(text=f"a plugin named '{name}' already exists")
        path = plugin_dir() / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(data.get("source", "")) or PLUGIN_TEMPLATE.format(name=name), encoding="utf-8")
        if plugin_dir() not in rt.plugins.dirs:
            rt.plugins.dirs.append(plugin_dir())
        ok = rt.plugins.reload(name)
        await changed("plugins")
        return web.json_response({"ok": ok, "plugins": plugin_rows()})

    @r.get("/api/plugins/{name}/source")
    async def plugin_source(req: web.Request) -> web.Response:
        path = rt.plugins.path_of(req.match_info["name"])
        if not path:
            raise web.HTTPNotFound(text="no editable source for that plugin")
        return web.json_response({"path": str(path), "source": path.read_text(encoding="utf-8")})

    @r.put("/api/plugins/{name}/source")
    async def save_plugin(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        path = rt.plugins.path_of(name)
        if not path:
            raise web.HTTPNotFound(text="no editable source for that plugin")
        source = str((await body(req)).get("source", ""))
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            raise web.HTTPBadRequest(text=f"not saved: syntax error on line {exc.lineno}: {exc.msg}")
        path.write_text(source, encoding="utf-8")
        ok = rt.plugins.reload(name) if name not in cfg.disabled_plugins else True
        await changed("plugins")
        row = next((p for p in plugin_rows() if p["name"] == name), {})
        return web.json_response({"ok": ok, "plugin": row})

    @r.post("/api/plugins/{name}/{action}")
    async def plugin_action(req: web.Request) -> web.Response:
        name, action = req.match_info["name"], req.match_info["action"]
        if action in ("enable", "disable"):
            disabled = [n for n in cfg.disabled_plugins if n != name] + ([name] if action == "disable" else [])
            await rt.apply_settings({"plugins": {"disabled": disabled}})
        elif action == "reload":
            rt.plugins.reload(name)
        else:
            raise web.HTTPNotFound(text=f"unknown action {action}")
        await changed("plugins")
        return web.json_response({"plugins": plugin_rows()})

    @r.delete("/api/plugins/{name}")
    async def delete_plugin(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        path = rt.plugins.path_of(name)
        if not path:
            raise web.HTTPNotFound(text="only plugins installed as files can be deleted; disable entry points instead")
        rt.plugins.unload(name)
        if path.name == "__init__.py":
            shutil.rmtree(path.parent)
        else:
            path.unlink()
        await changed("plugins")
        return web.json_response({"plugins": plugin_rows()})

    # ─────────────────────────────────────────────────────────────── MCP
    def mcp_rows() -> list[dict[str, Any]]:
        status = {n: (c, t, e) for n, c, t, e in rt.mcp.status()}
        rows = []
        for s in cfg.mcp_servers:
            connected, ntools, err = status.get(s.name, (False, 0, ""))
            conn = rt.mcp.connections.get(s.name)
            rows.append({"name": s.name, "command": s.command, "args": s.args, "env": {k: "•••" if v else "" for k, v in s.env.items()},
                         "enabled": s.enabled, "connected": connected, "tools": conn.tool_names if conn else [], "error": err})
        return rows

    @r.get("/api/mcp")
    async def mcp(_req: web.Request) -> web.Response:
        return web.json_response(mcp_rows())

    async def save_mcp(servers: list[Any]) -> None:
        await rt.set_mcp(servers)
        await changed("mcp")

    @r.put("/api/mcp/{name}")
    async def put_mcp(req: web.Request) -> web.Response:
        from ..config import McpServerConfig

        name = req.match_info["name"]
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", name):
            raise web.HTTPBadRequest(text="server name: letters, digits, - and _")
        data = await body(req)
        command = str(data.get("command", "")).strip()
        if not command:
            raise web.HTTPBadRequest(text="command required (e.g. npx, uvx, python)")
        old = next((s for s in cfg.mcp_servers if s.name == name), None)
        env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
        if old:  # masked values ("•••") mean "keep the stored secret"
            env = {k: (old.env.get(k, "") if v == "•••" else v) for k, v in env.items()}
        args = data.get("args") or []
        if isinstance(args, str):
            import shlex
            args = shlex.split(args)
        new = McpServerConfig(name=name, command=command, args=[str(a) for a in args], env=env, enabled=bool(data.get("enabled", True)))
        await save_mcp([s for s in cfg.mcp_servers if s.name != name] + [new])
        return web.json_response(mcp_rows())

    @r.delete("/api/mcp/{name}")
    async def delete_mcp(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        await save_mcp([s for s in cfg.mcp_servers if s.name != name])
        return web.json_response(mcp_rows())

    @r.post("/api/mcp/{name}/restart")
    async def restart_mcp(req: web.Request) -> web.Response:
        s = next((s for s in cfg.mcp_servers if s.name == req.match_info["name"]), None)
        if not s:
            raise web.HTTPNotFound(text="no such server")
        await rt.mcp.connect(s)
        await changed("mcp")
        return web.json_response(mcp_rows())

    @r.post("/api/mcp-import")
    async def import_mcp(req: web.Request) -> web.Response:
        """Paste a Claude Desktop / Cursor style {"mcpServers": {...}} block."""
        from ..config import McpServerConfig

        data = await body(req)
        block = data.get("mcpServers", data.get("servers", data))
        if not isinstance(block, dict) or not block:
            raise web.HTTPBadRequest(text='expected {"mcpServers": {"name": {"command": ..., "args": [...]}}}')
        added = []
        servers = list(cfg.mcp_servers)
        for name, spec in block.items():
            if not isinstance(spec, dict) or not spec.get("command"):
                continue
            servers = [s for s in servers if s.name != name] + [McpServerConfig(
                name=str(name), command=str(spec["command"]), args=[str(a) for a in spec.get("args") or []],
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()}, enabled=not spec.get("disabled", False))]
            added.append(name)
        if not added:
            raise web.HTTPBadRequest(text="no servers with a 'command' found (remote URL servers are not supported yet)")
        await save_mcp(servers)
        return web.json_response({"added": added, "servers": mcp_rows()})

    # ───────────────────────────────────────────────────────────── hooks
    def hook_rows() -> dict[str, Any]:
        return {"events": list(EVENTS),
                "shell": [asdict(h) for h in cfg.hooks],
                "plugin": [{"event": h.event, "matcher": h.matcher, "owner": h.owner} for h in rt.hooks.py]}

    def parse_hooks(items: Any) -> list[dict[str, Any]]:
        out = []
        for h in items or []:
            event, command = str(h.get("event", "")), str(h.get("command", "")).strip()
            if event not in EVENTS:
                raise web.HTTPBadRequest(text=f"unknown event {event!r}")
            if not command:
                raise web.HTTPBadRequest(text="every hook needs a command")
            out.append({"event": event, "command": command, "matcher": str(h.get("matcher") or "*"),
                        "timeout": float(h.get("timeout") or 30)})
        return out

    @r.get("/api/hooks")
    async def hooks(_req: web.Request) -> web.Response:
        return web.json_response(hook_rows())

    @r.put("/api/hooks")
    async def put_hooks(req: web.Request) -> web.Response:
        data = await body(req)
        await rt.apply_settings({"hooks": parse_hooks(data.get("hooks"))})
        await changed("hooks")
        return web.json_response(hook_rows())

    @r.post("/api/hooks/test")
    async def test_hook(req: web.Request) -> web.Response:
        """Run one hook (not yet saved) against a sample payload and report what it would do."""
        data = await body(req)
        spec = parse_hooks([data])[0]
        sample = {"pre_tool": {"tool": "bash", "arguments": {"command": "echo hi"}},
                  "post_tool": {"tool": "bash", "arguments": {"command": "echo hi"}, "result": "hi"},
                  "user_prompt": {"prompt": "hello"}, "stop": {"text": "done"},
                  "task_done": {"task_id": "t1", "goal_id": "", "title": "test", "status": "done", "result": "ok"},
                  "goal_achieved": {"goal_id": "g1", "title": "test", "assessment": "ok"}}.get(spec["event"], {})
        h = Hooks([HookConfig(**spec)], rt.project(str(data.get("project", ""))).root)
        res = await h.emit(spec["event"], {"agent": "hook-test", **sample}, key="bash")
        return web.json_response({"blocked": res.block, "reason": res.reason, "context": res.text, "payload": sample})
