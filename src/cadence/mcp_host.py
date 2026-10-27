"""MCP client/host: connects to configured MCP servers over stdio and exposes their tools.

Tools are namespaced ``mcp__<server>__<tool>`` so they cannot collide with built-ins.
Compatible with both mcp 1.x (camelCase attrs) and 2.x (snake_case attrs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .config import McpServerConfig, expand_env
from .tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


def _attr(obj: Any, snake: str, camel: str, default: Any = None) -> Any:
    if hasattr(obj, snake):
        return getattr(obj, snake)
    return getattr(obj, camel, default)


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


@dataclass
class McpConnection:
    cfg: McpServerConfig
    session: Any = None
    tool_names: list[str] = field(default_factory=list)
    error: str = ""
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def connected(self) -> bool:
        return self.session is not None


class McpHost:
    """Each server lives in its own asyncio task, which enters and exits the stdio client itself: the
    MCP SDK's anyio cancel scopes must be closed by the task that opened them, and servers are now
    added, restarted and removed from web requests (other tasks) at any time."""

    def __init__(self, servers: list[McpServerConfig], registry: ToolRegistry):
        self._servers = servers
        self._registry = registry
        self.connections: dict[str, McpConnection] = {}

    async def start(self) -> None:
        await asyncio.gather(*(self.connect(cfg) for cfg in self._servers if cfg.enabled))

    async def connect(self, cfg: McpServerConfig, timeout: float = 30.0) -> McpConnection:
        await self.disconnect(cfg.name)
        conn = McpConnection(cfg)
        self.connections[cfg.name] = conn
        ready = asyncio.Event()
        conn.task = asyncio.create_task(self._serve(conn, ready), name=f"mcp-{cfg.name}")
        try:
            await asyncio.wait_for(ready.wait(), timeout)
        except asyncio.TimeoutError:
            conn.error = f"timed out after {timeout:.0f}s waiting for the server to initialize"
            await self.disconnect(cfg.name, keep=True)
        return conn

    async def _serve(self, conn: McpConnection, ready: asyncio.Event) -> None:
        cfg = conn.cfg
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = sys.executable if cfg.command in ("python", "python3") else cfg.command
            env = {k: str(expand_env(v)) for k, v in cfg.env.items()}  # $NAME values come from Cadence's environment
            params = StdioServerParameters(command=command, args=cfg.args, env={**os.environ, **env} if env else None)
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listing = await session.list_tools()
                conn.session = session
                for t in listing.tools:
                    self._register(conn, t)
                ready.set()
                await conn.stop.wait()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:  # noqa: BLE001 — one bad server must not stop the agent (incl. ExceptionGroup)
            conn.error = f"{exc.__class__.__name__}: {exc}"
            log.warning("MCP server '%s' failed: %s", cfg.name, conn.error)
        finally:
            conn.session = None
            self._registry.unregister_source(f"mcp:{cfg.name}")
            conn.tool_names.clear()
            ready.set()

    async def disconnect(self, name: str, *, keep: bool = False) -> None:
        conn = self.connections.get(name) if keep else self.connections.pop(name, None)
        if not conn or not conn.task:
            return
        conn.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(conn.task), 10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            conn.task.cancel()
            await asyncio.gather(conn.task, return_exceptions=True)

    async def sync(self, servers: list[McpServerConfig]) -> None:
        """Bring connections in line with `servers`: start new/changed enabled ones, stop the rest."""
        self._servers = servers
        wanted = {s.name: s for s in servers if s.enabled}
        for name in list(self.connections):
            if name not in wanted:
                await self.disconnect(name)
        for name, cfg in wanted.items():
            conn = self.connections.get(name)
            if conn is None or conn.cfg != cfg or not conn.connected:
                await self.connect(cfg)

    def _register(self, conn: McpConnection, mcp_tool: Any) -> None:
        raw_name = mcp_tool.name
        full = f"mcp__{_safe(conn.cfg.name)}__{_safe(raw_name)}"
        schema = _attr(mcp_tool, "input_schema", "inputSchema", None) or {"type": "object", "properties": {}}
        session = conn.session

        async def handler(args: dict[str, Any], _s: Any = session, _n: str = raw_name) -> str:
            result = await _s.call_tool(_n, args)
            parts: list[str] = []
            for block in result.content or []:
                text = getattr(block, "text", None)
                parts.append(text if text is not None else f"[{getattr(block, 'type', 'content')} block]")
            body = "\n".join(parts) or "(empty result)"
            if _attr(result, "is_error", "isError", False):
                return f"ERROR: {body}"
            return body

        self._registry.register(
            Tool(
                name=full,
                description=f"[{conn.cfg.name}] {mcp_tool.description or raw_name}",
                parameters=schema,
                handler=handler,
                source=f"mcp:{conn.cfg.name}",
            )
        )
        conn.tool_names.append(full)

    async def stop(self) -> None:
        for name in list(self.connections):
            await self.disconnect(name)

    def status(self) -> list[tuple[str, bool, int, str]]:
        return [(n, c.connected, len(c.tool_names), c.error) for n, c in self.connections.items()]
