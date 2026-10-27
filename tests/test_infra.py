from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from cadence.autonomy import Daemon
from cadence.config import McpServerConfig, load_config
from cadence.tasks import Task
from cadence.llm import Completion, OpenAICompatLLM
from cadence.mcp_host import McpHost
from cadence.memory import MemoryStore
from cadence.tools import ToolRegistry

MCP_SERVER = '''
from mcp.server.mcpserver import MCPServer
m = MCPServer("demo")
@m.tool()
def add(a: int, b: int) -> int:
    """Add two ints."""
    return a + b
m.run()
'''


async def test_mcp_host_registers_namespaced_tools_and_calls_them(tmp_path: Path):
    script = tmp_path / "srv.py"
    script.write_text(MCP_SERVER)
    reg = ToolRegistry()
    host = McpHost([McpServerConfig(name="demo", command=sys.executable, args=[str(script)])], reg)
    await host.start()
    try:
        assert "mcp__demo__add" in reg.tools, host.status()
        assert (await reg.call("mcp__demo__add", {"a": 2, "b": 5}, 20)).strip() == "7"
    finally:
        await host.stop()
    assert "mcp__demo__add" not in reg.tools


async def test_bad_mcp_server_does_not_break_startup(tmp_path: Path):
    reg = ToolRegistry()
    host = McpHost([McpServerConfig(name="bad", command="definitely-not-a-binary")], reg)
    await host.start()
    assert host.status()[0][1] is False and host.status()[0][3]
    await host.stop()


def test_memory_three_kinds_search_and_persistence(tmp_path: Path):
    m = MemoryStore(tmp_path / "m.jsonl")
    m.add("semantic", "The API uses FastAPI with pydantic v2")
    m.add("procedural", "Run ruff before committing")
    m.add("episodic", "Fixed flaky test in auth module")
    assert m.search("fastapi pydantic")[0].kind == "semantic"
    assert len(MemoryStore(tmp_path / "m.jsonl")) == 3
    assert m.add("semantic", "The API uses FastAPI with pydantic v2").id  # idempotent
    assert len(m) == 3


def test_default_models_and_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CADENCE_HOME", str(tmp_path))
    cfg = load_config(tmp_path)
    assert cfg.big.name == "ornith-1.5-9b" and cfg.small.name == "liquid/lfm2-1.2b"
    monkeypatch.setenv("LMSTUDIO_URL", "http://box:9999")
    monkeypatch.setenv("CADENCE_SMALL_MODEL", "other-small")
    cfg = load_config(tmp_path)
    assert cfg.big.base_url == "http://box:9999/v1" and cfg.small.name == "other-small"


def test_yaml_config_declares_mcp_servers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CADENCE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("big: {name: custom-big}\nmcp_servers:\n  fs: {command: npx, args: [-y, srv]}\n")
    import importlib, cadence.config as c
    importlib.reload(c)
    cfg = c.load_config(tmp_path)
    assert cfg.big.name == "custom-big" and cfg.mcp_servers[0].name == "fs"


async def test_llm_stream_parser_assembles_tool_calls_from_sse(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo", "reasoning_content": "hm"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "bash", "arguments": '{"comm'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'and":"ls"}'}}]}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    from cadence.config import ModelConfig
    llm = OpenAICompatLLM(ModelConfig(name="m"))
    llm._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    seen = []

    async def on_delta(d):
        seen.append((d.kind, d.text))

    comp = await llm.complete([{"role": "user", "content": "hi"}], tools=[], on_delta=on_delta)
    assert comp.content == "Hello" and comp.reasoning == "hm"
    assert comp.tool_calls[0].parsed() == {"command": "ls"} and comp.finish_reason == "tool_calls"
    assert ("reasoning", "hm") in seen


async def test_daemon_runs_pending_goal_and_heartbeat_cycle(cfg, make_router):
    from test_tasks import make_runner, reflection

    cfg.reflection_cycles = 6
    ideas = json.dumps([{"title": "Add docs for CLI"}])
    router = make_router(big_script=[
        Completion(content="did the thing"), reflection("done"),   # the user goal
        Completion(content=ideas),                                 # then an idea cycle
    ], small_script=[Completion(content="Always write docs for new CLI flags")])
    runner, store, memory = make_runner(cfg, router)
    notes: list[str] = []

    async def notify(kind, msg):
        notes.append(f"{kind}:{msg}")

    from cadence.ideas import IdeaStore, ideate
    ideas = IdeaStore(cfg.state_dir / "ideas.json")

    async def cycle() -> int:
        return len(await ideate(router, ideas, store, memory, project="", workspace=cfg.workspace))

    daemon = Daemon(cfg, runner, store, notify, ideate=cycle)
    goal = daemon.submit(Task(title="do the thing"))
    task = asyncio.create_task(daemon.run_forever())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if store.get(goal.id).status == "done" and daemon.cycles_today >= 1 and len(ideas.list()) >= 1:
            break
    daemon.stop()
    await asyncio.wait_for(task, 5)
    assert store.get(goal.id).status == "done"
    assert daemon.cycles_today == 1 and any(n.startswith("cycle:") for n in notes)
    assert [i.title for i in ideas.list()] == ["Add docs for CLI"] and ideas.list()[0].status == "new"
    assert len(store.list()) == 1  # ideas wait in the inbox; they do not become tasks by default
