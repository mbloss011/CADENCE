"""Routes for settings, model discovery and extensions (skills, plugins, MCP servers, hooks).

Every change is written to ~/.cadence/settings.json (same keys as config.yaml) and applied to the
running process, so the UI and the next restart always agree. Skill and plugin *files* live in
~/.cadence/skills and ~/.cadence/plugins (or the project's .cadence/ directory for project scope).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from aiohttp import web

from ..config import ENV_LOCKS, HookConfig, ModelConfig, read_settings
from ..hooks import EVENTS, Hooks
from ..llm import discover_models
from ..sandbox import MODES
from ..skills import NAME_RE as SKILL_NAME_RE, RESOURCE_DIRS, SKIP_NAMES as SKIP_SKILL_NAMES, Skill, SkillError, SkillRegistry, \
    _safe_rel, export_zip, github_zip_url, parse_skill, read_dir, read_zip, subtree, unpack

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .server import Hub

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SKILL_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

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

## When to use
Describe the situations where this skill applies.

## Steps
1. …

## Resources
- `references/`: background docs. Read with `read_skill_file` only when a step needs them.
- `scripts/`: helpers. Run with `run_skill_script` instead of rewriting them.
- `assets/`: templates and files for outputs. Copy into the project with `use_skill_asset`.
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

    def skill_base(scope: str, project: str) -> Path:
        return user_skill_dir() if scope != "project" else rt.scope(project).project.root / ".cadence" / "skills"

    def scope_skills(req: web.Request) -> tuple[SkillRegistry, Path]:
        sc = rt.scope(req.query.get("project", ""))
        sc.skills.scan()
        return sc.skills, sc.project.root / ".cadence" / "skills"

    def writable(path: Path, project_dir: Path) -> bool:
        root = path.resolve()
        return any(base.resolve() in root.parents for base in (user_skill_dir(), project_dir))

    def skill_dict(s: Skill, reg: SkillRegistry, project_dir: Path) -> dict[str, Any]:
        where = "project" if project_dir.resolve() in s.path.resolve().parents else \
            "user" if user_skill_dir().resolve() in s.path.resolve().parents else "other"
        g = s.grouped()
        return {"name": s.name, "description": s.description, "path": str(s.path), "root": str(s.root), "scope": where,
                "enabled": s.name not in cfg.disabled_skills, "editable": writable(s.path, project_dir), "files": s.files,
                "counts": {k: len(v) for k, v in g.items()}, "meta": {k: str(v) for k, v in s.meta.items()}, "warnings": s.warnings}

    def editable_skill(req: web.Request) -> tuple[Skill, SkillRegistry, Path]:
        reg, pdir = scope_skills(req)
        s = reg.found.get(req.match_info["name"])
        if not s:
            raise web.HTTPNotFound(text="no such skill")
        return s, reg, pdir

    def must_write(s: Skill, pdir: Path) -> None:
        if not writable(s.path, pdir):
            raise web.HTTPForbidden(text=f"{s.root} is outside the editable skill folders (~/.cadence/skills, <project>/.cadence/skills)")

    def inside(s: Skill, rel: str) -> Path:
        try:
            return s.resolve(rel)
        except SkillError as exc:
            raise web.HTTPForbidden(text=str(exc))

    async def installed(names: list[str], base: Path) -> web.Response:
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True, "installed": names, "base": str(base)})

    @r.get("/api/skills")
    async def skills(req: web.Request) -> web.Response:
        reg, pdir = scope_skills(req)
        return web.json_response({"skills": [skill_dict(s, reg, pdir) for s in reg.found.values()], "errors": reg.errors,
                                  "dirs": [str(d) for d in reg.dirs]})

    @r.get("/api/skills/{name}")
    async def skill_source(req: web.Request) -> web.Response:
        s, reg, pdir = editable_skill(req)
        return web.json_response({**skill_dict(s, reg, pdir), "content": s.path.read_text(encoding="utf-8")})

    def check_skill(path: Path) -> str:
        try:
            parse_skill(path)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return ""

    @r.post("/api/skills")
    async def create_skill(req: web.Request) -> web.Response:
        data = await body(req)
        name = str(data.get("name", "")).strip().lower()
        if not SKILL_NAME_RE.match(name) or len(name) > 64:
            raise web.HTTPBadRequest(text="name: lowercase letters, digits and single hyphens (max 64), e.g. pdf-forms")
        description = str(data.get("description", "")).strip().replace("\n", " ")
        if not description:
            raise web.HTTPBadRequest(text="a description is required: it is how the agent decides to use the skill")
        base = skill_base(str(data.get("scope", "user")), str(data.get("project", "")))
        root = base / name
        if root.exists():
            raise web.HTTPConflict(text=f"{root} already exists")
        content = str(data.get("content", "")) or SKILL_TEMPLATE.format(name=name, description=description, title=name.replace("-", " ").title())
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(content, encoding="utf-8")
        for folder in data.get("folders", RESOURCE_DIRS) or []:
            if folder in RESOURCE_DIRS:
                (root / folder).mkdir(exist_ok=True)
        if err := check_skill(root / "SKILL.md"):
            shutil.rmtree(root)
            raise web.HTTPBadRequest(text=f"invalid SKILL.md: {err}")
        return await installed([name], base)

    @r.put("/api/skills/{name}")
    async def save_skill(req: web.Request) -> web.Response:
        s, _reg, pdir = editable_skill(req)
        must_write(s, pdir)
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
        s, _reg, pdir = editable_skill(req)
        if not writable(s.path, pdir):
            raise web.HTTPForbidden(text=f"{s.path} is outside the editable skill folders; disable it instead")
        shutil.rmtree(s.root)
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

    # files inside a skill: references/, scripts/, assets/ …
    @r.get("/api/skills/{name}/files")
    async def skill_files(req: web.Request) -> web.Response:
        s, _reg, _pdir = editable_skill(req)
        out = []
        for p in sorted(s.root.rglob("*")):
            rel = p.relative_to(s.root)
            if any(part in SKIP_SKILL_NAMES for part in rel.parts):
                continue
            out.append({"path": rel.as_posix(), "dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0,
                        "exec": p.is_file() and os.access(p, os.X_OK)})
        return web.json_response(out)

    @r.get("/api/skills/{name}/file")
    async def skill_file(req: web.Request) -> web.StreamResponse:
        s, _reg, _pdir = editable_skill(req)
        p = inside(s, req.query.get("path", ""))
        if not p.is_file():
            raise web.HTTPNotFound(text="no such file")
        if req.query.get("raw"):
            return web.FileResponse(p, headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
                                                "Content-Disposition": f'attachment; filename="{p.name}"'})
        raw = p.read_bytes()
        from ..documents import is_text
        if len(raw) > MAX_SKILL_BYTES * 8 or not is_text(raw):
            return web.json_response({"path": req.query.get("path"), "binary": True, "size": len(raw)})
        return web.json_response({"path": req.query.get("path"), "binary": False, "size": len(raw),
                                  "content": raw.decode("utf-8", "replace")})

    @r.put("/api/skills/{name}/file")
    async def save_skill_file(req: web.Request) -> web.Response:
        s, _reg, pdir = editable_skill(req)
        must_write(s, pdir)
        data = await body(req)
        rel = str(data.get("path", "")).strip()
        p = inside(s, rel)
        if p == s.root or p.is_dir():
            raise web.HTTPBadRequest(text="give a file path, e.g. references/api.md")
        content = str(data.get("content", ""))
        if p == s.path:
            old = p.read_text(encoding="utf-8")
            p.write_text(content, encoding="utf-8")
            if err := check_skill(p):
                p.write_text(old, encoding="utf-8")
                raise web.HTTPBadRequest(text=f"not saved, invalid SKILL.md: {err}")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            if p.relative_to(s.root).parts[0] == "scripts" or content.startswith("#!"):
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True})

    @r.delete("/api/skills/{name}/file")
    async def delete_skill_file(req: web.Request) -> web.Response:
        s, _reg, pdir = editable_skill(req)
        must_write(s, pdir)
        p = inside(s, req.query.get("path", ""))
        if p in (s.root, s.path):
            raise web.HTTPBadRequest(text="SKILL.md and the skill folder itself cannot be deleted here")
        if not p.exists():
            raise web.HTTPNotFound(text="no such file")
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True})

    @r.post("/api/skills/{name}/upload")
    async def upload_skill_files(req: web.Request) -> web.Response:
        """Add files to a skill folder (e.g. dir=assets). Relative paths in the filenames are kept."""
        s, _reg, pdir = editable_skill(req)
        must_write(s, pdir)
        folder = req.query.get("dir", "")
        saved = []
        reader = await req.multipart()
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            rel = _safe_rel(part.filename)
            if rel is None:
                continue
            p = inside(s, f"{folder}/{rel.as_posix()}" if folder else rel.as_posix())
            data = await part.read(decode=False)
            if len(data) > MAX_UPLOAD_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=len(data))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            if p.relative_to(s.root).parts[0] == "scripts" or data[:2] == b"#!":
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
            saved.append(p.relative_to(s.root).as_posix())
        rt.reload_skills()
        await changed("skills")
        return web.json_response({"ok": True, "saved": saved})

    @r.get("/api/skills/{name}/export")
    async def export_skill(req: web.Request) -> web.Response:
        s, _reg, _pdir = editable_skill(req)
        return web.Response(body=export_zip(s), content_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{s.name}.zip"'})

    # installing whole skills
    @r.post("/api/skills-upload")
    async def upload_skill(req: web.Request) -> web.Response:
        """A .zip / .skill archive, or the files of a folder (filenames carry their relative paths)."""
        base = skill_base(req.query.get("scope", "user"), req.query.get("project", ""))
        files: dict[Path, bytes] = {}
        reader = await req.multipart()
        total = 0
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            data = await part.read(decode=False)
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=total)
            if part.filename.lower().endswith((".zip", ".skill")):
                files.update(read_zip(data))
            elif (rel := _safe_rel(part.filename)) is not None:
                files[rel] = data
        base.mkdir(parents=True, exist_ok=True)
        try:
            names = unpack(files, base, overwrite=req.query.get("overwrite") == "1")
        except SkillError as exc:
            raise web.HTTPConflict(text=str(exc)) if "already exists" in str(exc) else web.HTTPBadRequest(text=str(exc))
        return await installed(names, base)

    @r.post("/api/skills-import")
    async def import_skill(req: web.Request) -> web.Response:
        """From a local folder (or SKILL.md / .zip in it), a GitHub folder URL, or an https URL to a .zip or SKILL.md."""
        data = await body(req)
        src = str(data.get("source", "")).strip()
        base = skill_base(str(data.get("scope", "user")), str(data.get("project", "")))
        base.mkdir(parents=True, exist_ok=True)
        try:
            if src.startswith(("http://", "https://")):
                gh = github_zip_url(src) if "github.com/" in src and not src.endswith((".zip", ".skill")) else None
                url = gh[0] if gh else src
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                content = resp.content
                if len(content) > MAX_UPLOAD_BYTES:
                    raise web.HTTPBadRequest(text="download too large to be a skill")
                if gh or url.lower().endswith((".zip", ".skill")) or content[:2] == b"PK":
                    files = subtree(read_zip(content), gh[1]) if gh else read_zip(content)
                else:
                    files = {Path("SKILL.md"): content}
            else:
                path = Path(src).expanduser()
                if path.is_file() and path.suffix.lower() in (".zip", ".skill"):
                    files = read_zip(path.read_bytes())
                else:
                    folder = path.parent if path.is_file() and path.name == "SKILL.md" else path
                    if not folder.is_dir():
                        raise web.HTTPBadRequest(text=f"{folder} is not a folder, .zip or SKILL.md")
                    files = read_dir(folder)
            names = unpack(files, base, overwrite=bool(data.get("overwrite")))
        except httpx.HTTPError as exc:
            raise web.HTTPBadGateway(text=f"download failed: {exc}")
        except SkillError as exc:
            raise web.HTTPConflict(text=str(exc)) if "already exists" in str(exc) else web.HTTPBadRequest(text=str(exc))
        return await installed(names, base)

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
