"""Tool registry and built-in coding tools (workspace-sandboxed)."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

Handler = Callable[[dict[str, Any]], Awaitable[str]]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


class ToolError(Exception):
    """Expected, model-recoverable tool failure. The message is shown to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    source: str = "builtin"
    read_only: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description[:1024], "parameters": self.parameters},
        }


@dataclass
class ToolRegistry:
    """Tools by name. A fork layers its own tools over a live view of its parent, so tools that
    MCP servers or plugins add later (from the UI) reach agents that already exist."""

    tools: dict[str, Tool] = field(default_factory=dict)
    parent: "ToolRegistry | None" = None

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def unregister_source(self, source: str) -> None:
        for name in [n for n, t in self.tools.items() if t.source == source]:
            del self.tools[name]

    def fork(self) -> "ToolRegistry":
        """Child registry so an agent can add its own stateful tools without leaking them."""
        return ToolRegistry(parent=self)

    def all(self) -> dict[str, Tool]:
        merged = self.parent.all() if self.parent else {}
        merged.update(self.tools)
        return merged

    def get(self, name: str) -> Tool | None:
        tool = self.tools.get(name)
        return tool if tool is not None or self.parent is None else self.parent.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.all().values()]

    def names(self) -> list[str]:
        return sorted(self.all())

    async def call(self, name: str, args: dict[str, Any], timeout: float) -> str:
        tool = self.get(name)
        if tool is None:
            close = [n for n in self.all() if name.lower() in n.lower()][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"ERROR: unknown tool '{name}'.{hint}"
        try:
            return await asyncio.wait_for(tool.handler(args), timeout=timeout)
        except asyncio.TimeoutError:
            return f"ERROR: tool '{name}' timed out after {timeout:.0f}s"
        except ToolError as exc:
            return f"ERROR: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the loop on a tool bug
            return f"ERROR: {exc.__class__.__name__}: {exc}"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} chars truncated]…\n{text[-tail:]}"


class Workspace:
    """Resolves and confines paths to the workspace root."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        p = (self.root / p) if not p.is_absolute() else p
        p = p.resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ToolError(f"path '{path}' is outside the workspace {self.root}") from None
        return p

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def builtin_tools(ws: Workspace, *, allow_shell: bool, output_limit: int, sandbox: Any = None, vision: Any = None) -> list[Tool]:
    """`sandbox` is a `kestrel.sandbox.Sandbox` (None = unsandboxed); `vision` an LLM enabling `view_image`."""
    from .sandbox import Sandbox, SandboxError
    from .config import SandboxConfig

    shell = sandbox or Sandbox(SandboxConfig(mode="none"), ws.root)

    async def read_file(args: dict[str, Any]) -> str:
        path = ws.resolve(str(args.get("path", "")))
        if not path.is_file():
            raise ToolError(f"not a file: {ws.rel(path)}")
        raw = path.read_bytes()
        if b"\x00" in raw[:2048]:
            raise ToolError("binary file; refusing to read")
        lines = raw.decode("utf-8", "replace").splitlines()
        start = max(1, int(args.get("offset", 1) or 1))
        limit = int(args.get("limit", 400) or 400)
        chunk = lines[start - 1 : start - 1 + limit]
        body = "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(chunk, start))
        more = f"\n…[{len(lines) - (start - 1 + len(chunk))} more lines; use offset]" if start - 1 + len(chunk) < len(lines) else ""
        return truncate(body + more, output_limit)

    async def write_file(args: dict[str, Any]) -> str:
        path = ws.resolve(str(args.get("path", "")))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("'content' must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {ws.rel(path)}"

    async def edit_file(args: dict[str, Any]) -> str:
        path = ws.resolve(str(args.get("path", "")))
        old, new = args.get("old_string"), args.get("new_string")
        if not path.is_file():
            raise ToolError(f"not a file: {ws.rel(path)}")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ToolError("'old_string' and 'new_string' must be strings and old_string non-empty")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ToolError("old_string not found; re-read the file and match it exactly")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string matches {count} places; add context or set replace_all=true")
        path.write_text(text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1), encoding="utf-8")
        return f"edited {ws.rel(path)} ({count if args.get('replace_all') else 1} replacement)"

    async def list_files(args: dict[str, Any]) -> str:
        base = ws.resolve(str(args.get("path", ".") or "."))
        pattern = str(args.get("pattern", "*") or "*")
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                rel = ws.rel(Path(dirpath) / fn)
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fn, pattern):
                    out.append(rel)
                if len(out) >= 500:
                    break
            if len(out) >= 500:
                break
        return "\n".join(out) or "(no matches)"

    async def grep(args: dict[str, Any]) -> str:
        try:
            rx = re.compile(str(args.get("pattern", "")), re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            raise ToolError(f"bad regex: {exc}") from None
        base = ws.resolve(str(args.get("path", ".") or "."))
        glob = str(args.get("glob", "*") or "*")
        hits: list[str] = []
        for f in [base] if base.is_file() else _walk_files(base, glob):
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{ws.rel(f)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n…[200 match cap]"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    async def bash(args: dict[str, Any]) -> str:
        if not allow_shell:
            raise ToolError("shell execution is disabled in config (allow_shell: false)")
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("'command' is required")
        timeout = float(args.get("timeout", 60) or 60)
        try:
            res = await shell.run(command, timeout, env={"PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb"})
        except SandboxError as exc:
            raise ToolError(f"sandbox unavailable: {exc}") from None
        if res.timed_out:
            return f"ERROR: command timed out after {timeout:.0f}s and was killed"
        text = truncate(res.output, output_limit)
        return f"[exit {res.exit}]\n{text}" if text.strip() else f"[exit {res.exit}] (no output)"

    async def view_image(args: dict[str, Any]) -> str:
        from .vision import ImageError, describe

        path = ws.resolve(str(args.get("path", "")))
        try:
            return truncate(await describe(vision, path, str(args.get("question", "") or "")), output_limit)
        except ImageError as exc:
            raise ToolError(str(exc)) from None

    return [
        Tool("read_file", "Read a text file with line numbers. Use offset/limit for large files.",
             _schema({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
             read_file, read_only=True),
        Tool("write_file", "Create or overwrite a file with the given content.",
             _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), write_file),
        Tool("edit_file", "Replace an exact string in a file. Fails if not found or ambiguous.",
             _schema({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                      "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]), edit_file),
        Tool("list_files", "List files under a directory matching a glob pattern.",
             _schema({"path": {"type": "string"}, "pattern": {"type": "string"}}, []), list_files, read_only=True),
        Tool("grep", "Regex search file contents. Returns path:line: text.",
             _schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
                      "ignore_case": {"type": "boolean"}}, ["pattern"]), grep, read_only=True),
        Tool("bash", "Run a shell command in the workspace (timeout default 60s). Use for tests, git, builds."
             + ("" if shell.mode == "none" else f" Runs inside a {shell.mode} sandbox: only the workspace is writable"
                + ("" if shell.cfg.network else ", no network") + "."),
             _schema({"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]), bash),
    ] + ([
        Tool("view_image", "Look at an image file in the workspace (screenshot, diagram, photo, UI) with the vision "
             "model and get a detailed description. Ask a specific question for best results.",
             _schema({"path": {"type": "string"}, "question": {"type": "string"}}, ["path"]), view_image, read_only=True),
    ] if vision is not None else [])


def _walk_files(base: Path, glob: str):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fnmatch.fnmatch(fn, glob):
                yield Path(dirpath) / fn


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def have(binary: str) -> bool:
    return shutil.which(binary) is not None
