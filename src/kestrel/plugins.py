"""Python plugins: add tools and hooks without touching Kestrel's source.

A plugin is `<plugin_dir>/<name>.py` or `<plugin_dir>/<name>/__init__.py` (or an installed package
exposing a `kestrel.plugins` entry point) with a `register(api)` function:

    def register(api):
        @api.tool("word_count", "Count words in a workspace file.",
                  {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                  read_only=True)
        async def word_count(args):
            return str(len(api.workspace.joinpath(args["path"]).read_text().split()))

        @api.on("pre_tool", matcher="bash")
        def no_force_push(payload):
            if "push --force" in payload["arguments"].get("command", ""):
                return HookResult(block=True, reason="force-push is not allowed")

Plugins run in-process with your privileges — only install ones you trust.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .hooks import HookResult, Hooks
from .tools import Tool, ToolRegistry

log = logging.getLogger(__name__)

__all__ = ["PluginAPI", "PluginManager", "HookResult"]


class PluginAPI:
    """What a plugin's `register(api)` receives."""

    def __init__(self, name: str, registry: ToolRegistry, hooks: Hooks, cfg: Any, memory: Any = None):
        self.name = name
        self.cfg = cfg
        self.workspace: Path = cfg.workspace
        self.memory = memory
        self._registry = registry
        self._hooks = hooks
        self.tools: list[str] = []

    def add_tool(self, tool: Tool) -> None:
        tool.source = f"plugin:{self.name}"
        self._registry.register(tool)
        self.tools.append(tool.name)

    def tool(self, name: str, description: str, parameters: dict[str, Any] | None = None, *, read_only: bool = False):
        def deco(fn: Callable[[dict[str, Any]], Any]):
            async def handler(args: dict[str, Any]) -> str:
                value = fn(args)
                if inspect.isawaitable(value):
                    value = await value
                return value if isinstance(value, str) else json.dumps(value, default=str)

            self.add_tool(Tool(name, description, parameters or {"type": "object", "properties": {}}, handler, read_only=read_only))
            return fn
        return deco

    def on(self, event: str, matcher: str = "*"):
        def deco(fn: Callable[[dict[str, Any]], Any]):
            self._hooks.on(event, fn, matcher, owner=self.name)
            return fn
        return deco


@dataclass
class LoadedPlugin:
    name: str
    origin: str
    tools: list[str] = field(default_factory=list)


class PluginManager:
    def __init__(self, dirs: list[Path], disabled: list[str] | None = None):
        self.dirs = [Path(d) for d in dirs]
        self.disabled = set(disabled or [])
        self.loaded: dict[str, LoadedPlugin] = {}
        self.errors: list[str] = []

    def discover(self) -> list[tuple[str, str, Callable[[], Any]]]:
        """(name, origin, loader) for every plugin found; loaders import lazily."""
        found: list[tuple[str, str, Callable[[], Any]]] = []
        for d in self.dirs:
            if not d.is_dir():
                continue
            for entry in sorted(d.iterdir()):
                if entry.name.startswith(("_", ".")):
                    continue
                if entry.suffix == ".py" and entry.is_file():
                    found.append((entry.stem, str(entry), lambda p=entry: _import_file(p.stem, p)))
                elif (entry / "__init__.py").is_file():
                    found.append((entry.name, str(entry), lambda p=entry: _import_file(p.name, p / "__init__.py")))
        try:
            for ep in entry_points(group="kestrel.plugins"):
                found.append((ep.name, f"entry point {ep.value}", ep.load))
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"entry points: {exc}")
        return found

    def load(self, registry: ToolRegistry, hooks: Hooks, cfg: Any, memory: Any = None) -> None:
        for name, origin, loader in self.discover():
            if name in self.disabled or name in self.loaded:
                continue
            try:
                obj = loader()
                register = getattr(obj, "register", obj if callable(obj) else None)
                if not callable(register):
                    raise TypeError("plugin has no register(api) function")
                api = PluginAPI(name, registry, hooks, cfg, memory)
                register(api)
                self.loaded[name] = LoadedPlugin(name, origin, api.tools)
            except Exception as exc:  # noqa: BLE001 — one broken plugin must not stop startup
                self.errors.append(f"{name} ({origin}): {exc.__class__.__name__}: {exc}")
                log.warning("plugin %s failed to load: %s", name, exc)


def _import_file(name: str, path: Path) -> Any:
    mod_name = f"kestrel_plugin_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
