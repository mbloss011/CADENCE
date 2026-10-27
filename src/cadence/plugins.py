"""Python plugins: add tools and hooks without touching Cadence's source.

A plugin is `<plugin_dir>/<name>.py` or `<plugin_dir>/<name>/__init__.py` (or an installed package
exposing a `cadence.plugins` entry point) with a `register(api)` function:

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
    hooks: int = 0


class PluginManager:
    def __init__(self, dirs: list[Path], disabled: list[str] | None = None):
        self.dirs = [Path(d) for d in dirs]
        self.disabled = set(disabled or [])
        self.loaded: dict[str, LoadedPlugin] = {}
        self.errors: list[str] = []
        self._ctx: tuple[Any, ...] = ()

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
            for ep in [*entry_points(group="cadence.plugins"), *entry_points(group="kestrel.plugins")]:  # + pre-rename group
                found.append((ep.name, f"entry point {ep.value}", ep.load))
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"entry points: {exc}")
        return found

    def load(self, registry: ToolRegistry, hooks: Hooks, cfg: Any, memory: Any = None) -> None:
        self._ctx = (registry, hooks, cfg, memory)
        for name, origin, loader in self.discover():
            if name not in self.disabled and name not in self.loaded:
                self._load_one(name, origin, loader)

    def _load_one(self, name: str, origin: str, loader: Callable[[], Any]) -> bool:
        registry, hooks, cfg, memory = self._ctx
        self.errors = [e for e in self.errors if not e.startswith(f"{name} (")]
        before = len(hooks.py)
        try:
            obj = loader()
            register = getattr(obj, "register", obj if callable(obj) else None)
            if not callable(register):
                raise TypeError("plugin has no register(api) function")
            api = PluginAPI(name, registry, hooks, cfg, memory)
            register(api)
            self.loaded[name] = LoadedPlugin(name, origin, api.tools, len(hooks.py) - before)
            return True
        except Exception as exc:  # noqa: BLE001 — one broken plugin must not stop startup
            registry.unregister_source(f"plugin:{name}")  # drop whatever it registered before failing
            hooks.remove_owner(name)
            self.errors.append(f"{name} ({origin}): {exc.__class__.__name__}: {exc}")
            log.warning("plugin %s failed to load: %s", name, exc)
            return False

    def unload(self, name: str) -> None:
        registry, hooks, _cfg, _memory = self._ctx
        registry.unregister_source(f"plugin:{name}")
        hooks.remove_owner(name)
        self.loaded.pop(name, None)
        sys.modules.pop(f"cadence_plugin_{name}", None)

    def reload(self, name: str) -> bool:
        """Re-import a plugin from disk (after an edit). False if it is unknown, disabled or fails."""
        self.unload(name)
        for n, origin, loader in self.discover():
            if n == name and n not in self.disabled:
                return self._load_one(n, origin, loader)
        return False

    def sync(self, disabled: list[str]) -> None:
        """Apply a new disabled list: unload newly disabled plugins, load newly enabled ones."""
        self.disabled = set(disabled)
        for name in [n for n in self.loaded if n in self.disabled]:
            self.unload(name)
        for name, origin, loader in self.discover():
            if name not in self.disabled and name not in self.loaded:
                self._load_one(name, origin, loader)

    def path_of(self, name: str) -> Path | None:
        """The editable source file of a directory plugin (None for entry points)."""
        for d in self.dirs:
            for cand in (d / f"{name}.py", d / name / "__init__.py"):
                if cand.is_file():
                    return cand
        return None


def _import_file(name: str, path: Path) -> Any:
    mod_name = f"cadence_plugin_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
