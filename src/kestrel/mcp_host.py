"""MCP client/host: connects to configured MCP servers over stdio and exposes their tools.

Tools are namespaced ``mcp__<server>__<tool>`` so they cannot collide with built-ins.
Compatible with both mcp 1.x (camelCase attrs) and 2.x (snake_case attrs).
"""

from __future__ import annotations

import logging
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .config import McpServerConfig
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

    @property
    def connected(self) -> bool:
        return self.session is not None


class McpHost:
    def __init__(self, servers: list[McpServerConfig], registry: ToolRegistry):
        self._servers = [s for s in servers if s.enabled]
        self._registry = registry
        self._stack = AsyncExitStack()
        self.connections: dict[str, McpConnection] = {}

    async def start(self) -> None:
        for cfg in self._servers:
            await self._connect(cfg)

    async def _connect(self, cfg: McpServerConfig) -> None:
        conn = McpConnection(cfg)
        self.connections[cfg.name] = conn
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = sys.executable if cfg.command in ("python", "python3") else cfg.command
            params = StdioServerParameters(command=command, args=cfg.args, env=cfg.env or None)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
            conn.session = session
            for t in listing.tools:
                self._register(conn, t)
        except Exception as exc:  # noqa: BLE001 — one bad server must not stop the agent
            conn.error = f"{exc.__class__.__name__}: {exc}"
            log.warning("MCP server '%s' failed to start: %s", cfg.name, conn.error)

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
            self._registry.unregister_source(f"mcp:{name}")
        try:
            await self._stack.aclose()
        except Exception as exc:  # noqa: BLE001
            log.debug("MCP shutdown noise: %s", exc)
        self.connections.clear()

    def status(self) -> list[tuple[str, bool, int, str]]:
        return [(n, c.connected, len(c.tool_names), c.error) for n, c in self.connections.items()]
