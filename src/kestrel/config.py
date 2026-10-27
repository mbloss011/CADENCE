"""Configuration: defaults < ~/.kestrel/config.yaml < ./.kestrel/config.yaml < env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

HOME_DIR = Path(os.environ.get("KESTREL_HOME", Path.home() / ".kestrel"))


@dataclass
class ModelConfig:
    """One OpenAI-compatible model endpoint (LM Studio by default)."""

    name: str
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    temperature: float = 0.2
    max_tokens: int = 4096
    context_window: int = 32768
    timeout: float = 180.0


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class MemoryConfig:
    """Long-term memory retrieval. `local` keeps vectors in a JSONL sidecar; `qdrant` uses a Qdrant server."""

    backend: str = "local"  # local | qdrant | keyword
    embed_model: str = "text-embedding-nomic-embed-text-v1.5"
    embed_url: str = ""     # empty = same endpoint as the big model
    query_prefix: str = "search_query: "      # nomic-style task prefixes; set both to "" for other embedders
    doc_prefix: str = "search_document: "
    qdrant_url: str = "http://localhost:6333"
    collection: str = "kestrel_memory"
    semantic_weight: float = 0.75  # blend of vector similarity vs keyword overlap
    min_similarity: float = 0.55   # cosine floor; nomic-embed scores unrelated text around 0.45-0.5


@dataclass
class SandboxConfig:
    """Isolation for `bash` and goal verify commands."""

    mode: str = "none"  # none | bwrap | docker
    network: bool = True
    hide_home: bool = True  # bwrap: mount an empty tmpfs over $HOME (the workspace is re-bound on top)
    ro_binds: list[str] = field(default_factory=list)
    rw_binds: list[str] = field(default_factory=list)
    image: str = "python:3.12-slim"  # docker mode


@dataclass
class HookConfig:
    event: str
    command: str
    matcher: str = "*"  # glob on the tool name (pre_tool/post_tool); ignored for other events
    timeout: float = 30.0


@dataclass
class Config:
    big: ModelConfig = field(default_factory=lambda: ModelConfig(name="ornith-1.5-9b", context_window=32768))
    small: ModelConfig = field(
        default_factory=lambda: ModelConfig(name="liquid/lfm2-1.2b", temperature=0.1, max_tokens=1024, context_window=32768)
    )
    workspace: Path = field(default_factory=Path.cwd)
    home: Path = HOME_DIR
    skill_dirs: list[Path] = field(default_factory=list)
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    max_iterations: int = 40
    tool_timeout: float = 120.0
    compact_trigger: float = 0.70   # fraction of the context window that triggers compaction
    compact_target: float = 0.40    # fraction we compact down to
    keep_recent_messages: int = 8
    tool_output_limit: int = 8000   # chars kept from any single tool result
    reflection_cycles: int = 6      # autonomous idea/reflection cycles per day
    max_reflections_per_goal: int = 3
    allow_shell: bool = True
    auto_approve_ideas: bool = False  # autonomously generated goals wait for `goals approve` unless true
    max_parallel_goals: int = 2
    vision: ModelConfig | None = None  # image understanding; None = use the big model
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    hooks: list[HookConfig] = field(default_factory=list)
    plugin_dirs: list[Path] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)  # telegram / whatsapp settings

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.home = Path(self.home)
        self.skill_dirs = [Path(p) for p in self.skill_dirs] or [
            self.home / "skills",
            self.workspace / ".kestrel" / "skills",
            self.workspace / ".agents" / "skills",
        ]
        self.plugin_dirs = [Path(p) for p in self.plugin_dirs] or [
            self.home / "plugins",
            self.workspace / ".kestrel" / "plugins",
        ]

    @property
    def state_dir(self) -> Path:
        return self.home / "state"


def _merge_model(base: ModelConfig, data: dict[str, Any]) -> ModelConfig:
    allowed = {f.name for f in fields(ModelConfig)}
    for key, value in data.items():
        if key in allowed:
            setattr(base, key, value)
    return base


def _merge(base: Any, data: dict[str, Any]) -> None:
    allowed = {f.name for f in fields(base)}
    for key, value in (data or {}).items():
        if key in allowed:
            setattr(base, key, value)


def expand_env(value: Any) -> Any:
    """`$NAME` / `${NAME}` strings are read from the environment so secrets stay out of config files."""
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value.strip("${}"), "")
    return value


def _apply(cfg: Config, data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    _merge_model(cfg.big, data.get("big", {}) or {})
    _merge_model(cfg.small, data.get("small", {}) or {})
    scalars = {
        "max_iterations", "tool_timeout", "compact_trigger", "compact_target", "keep_recent_messages",
        "tool_output_limit", "reflection_cycles", "max_reflections_per_goal", "allow_shell",
        "auto_approve_ideas", "max_parallel_goals",
    }
    for key in scalars & data.keys():
        setattr(cfg, key, data[key])
    if "skill_dirs" in data:
        cfg.skill_dirs = [Path(p).expanduser() for p in data["skill_dirs"]]
    if data.get("vision"):
        cfg.vision = _merge_model(ModelConfig(name=cfg.big.name, base_url=cfg.big.base_url, api_key=cfg.big.api_key),
                                  data["vision"])
    _merge(cfg.memory, data.get("memory") or {})
    _merge(cfg.sandbox, data.get("sandbox") or {})
    for spec in data.get("hooks") or []:
        cfg.hooks.append(HookConfig(event=spec["event"], command=spec["command"], matcher=str(spec.get("matcher", "*")),
                                    timeout=float(spec.get("timeout", 30.0))))
    plugins = data.get("plugins") or {}
    if "dirs" in plugins:
        cfg.plugin_dirs = [Path(p).expanduser() for p in plugins["dirs"]]
    cfg.disabled_plugins = list(plugins.get("disabled", cfg.disabled_plugins))
    for name, spec in (data.get("channels") or {}).items():
        cfg.channels[name] = {**cfg.channels.get(name, {}), **(spec or {})}
    for name, spec in (data.get("mcp_servers") or {}).items():
        cfg.mcp_servers = [s for s in cfg.mcp_servers if s.name != name]
        cfg.mcp_servers.append(
            McpServerConfig(
                name=name,
                command=spec["command"],
                args=list(spec.get("args", [])),
                env={k: str(v) for k, v in (spec.get("env") or {}).items()},
                enabled=bool(spec.get("enabled", True)),
            )
        )


def load_config(workspace: Path | None = None) -> Config:
    cfg = Config(workspace=workspace or Path.cwd())
    for path in (cfg.home / "config.yaml", cfg.workspace / ".kestrel" / "config.yaml"):
        if path.is_file():
            _apply(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    env = os.environ
    if env.get("LMSTUDIO_URL"):
        url = env["LMSTUDIO_URL"].rstrip("/")
        url = url if url.endswith("/v1") else f"{url}/v1"
        cfg.big.base_url = cfg.small.base_url = url
    if env.get("KESTREL_BIG_MODEL"):
        cfg.big.name = env["KESTREL_BIG_MODEL"]
    if env.get("KESTREL_SMALL_MODEL"):
        cfg.small.name = env["KESTREL_SMALL_MODEL"]
    if env.get("KESTREL_BIG_URL"):
        cfg.big.base_url = env["KESTREL_BIG_URL"]
    if env.get("KESTREL_SMALL_URL"):
        cfg.small.base_url = env["KESTREL_SMALL_URL"]
    if env.get("KESTREL_SANDBOX"):
        cfg.sandbox.mode = env["KESTREL_SANDBOX"]
    if env.get("KESTREL_MEMORY_BACKEND"):
        cfg.memory.backend = env["KESTREL_MEMORY_BACKEND"]

    # Workspace-local skill dirs are resolved after overrides.
    cfg.__post_init__()
    return cfg
