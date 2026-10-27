"""Runtime: wires config, models, tools, MCP, skills, memory, hooks, plugins, goals and the daemon together."""

from __future__ import annotations

from .agent import Agent
from .autonomy import Daemon, Notify
from .config import Config
from .goals import GoalRunner, GoalStore
from .hooks import Hooks
from .llm import ModelRouter
from .mcp_host import McpHost
from .memory import MemoryStore, build_memory
from .plugins import PluginManager
from .sandbox import Sandbox
from .skills import SkillRegistry
from .tools import ToolRegistry, Workspace, builtin_tools


class Runtime:
    def __init__(self, cfg: Config, router: ModelRouter | None = None, memory: MemoryStore | None = None):
        self.cfg = cfg
        self.router = router or ModelRouter.from_config(cfg)
        self.registry = ToolRegistry()
        self.skills = SkillRegistry(cfg.skill_dirs)
        self.memory = memory or build_memory(cfg)
        self.goals = GoalStore(cfg.state_dir / "goals.json")
        self.mcp = McpHost(cfg.mcp_servers, self.registry)
        self.sandbox = Sandbox(cfg.sandbox, cfg.workspace)
        self.hooks = Hooks(cfg.hooks, cfg.workspace)
        self.plugins = PluginManager(cfg.plugin_dirs, cfg.disabled_plugins)
        self.runner = GoalRunner(cfg, self.router, self.goals, self.memory, self.new_agent, self.sandbox, self.hooks)
        self.daemon: Daemon | None = None

    async def start(self) -> "Runtime":
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        ws = Workspace(self.cfg.workspace)
        for tool in builtin_tools(ws, allow_shell=self.cfg.allow_shell, output_limit=self.cfg.tool_output_limit,
                                  sandbox=self.sandbox, vision=self.router.vision):
            self.registry.register(tool)
        self.skills.scan()
        await self.mcp.start()
        self.plugins.load(self.registry, self.hooks, self.cfg, self.memory)  # last, so plugins may wrap anything
        return self

    def new_agent(self, name: str = "main") -> Agent:
        return Agent(self.cfg, self.router, self.registry, self.skills, self.memory, name=name, hooks=self.hooks)

    def make_daemon(self, notify: Notify | None = None, event_sink=None) -> Daemon:
        self.daemon = Daemon(self.cfg, self.runner, self.goals, notify, event_sink)
        return self.daemon

    async def close(self) -> None:
        await self.mcp.stop()
        await self.memory.aclose()
        await self.router.aclose()
