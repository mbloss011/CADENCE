from __future__ import annotations

import asyncio
import json

from conftest import tool_call

from cadence.agent import Agent
from cadence.llm import Completion
from cadence.memory import MemoryStore
from cadence.skills import SkillRegistry
from cadence.tools import ToolRegistry, Workspace, builtin_tools


def build_agent(cfg, router) -> Agent:
    reg = ToolRegistry()
    for t in builtin_tools(Workspace(cfg.workspace), allow_shell=True, output_limit=4000):
        reg.register(t)
    skills = SkillRegistry(cfg.skill_dirs)
    skills.scan()
    return Agent(cfg, router, reg, skills, MemoryStore(cfg.state_dir / "m.jsonl"))


async def collect(agent, text):
    return [ev async for ev in agent.run(text)]


async def test_tool_loop_writes_file_then_finishes(cfg, make_router):
    router = make_router(big_script=[
        tool_call("write_file", path="hello.py", content="print('hi')\n"),
        tool_call("bash", command="python3 hello.py"),
        Completion(content="Created and ran hello.py"),
    ])
    agent = build_agent(cfg, router)
    events = await collect(agent, "make hello.py")
    types = [e.type for e in events]
    assert types.count("tool_call") == 2 and types[-1] == "message_done"
    assert (cfg.workspace / "hello.py").read_text() == "print('hi')\n"
    result = next(e for e in events if e.type == "tool_result" and e.data["name"] == "bash")
    assert "hi" in result.data["result"] and "[exit 0]" in result.data["result"]


async def test_workspace_escape_is_blocked(cfg, make_router):
    router = make_router(big_script=[tool_call("read_file", path="/etc/passwd"), Completion(content="done")])
    events = await collect(build_agent(cfg, router), "read it")
    res = next(e for e in events if e.type == "tool_result")
    assert res.data["result"].startswith("ERROR") and "outside the workspace" in res.data["result"]


async def test_invalid_tool_args_are_reported_not_crashing(cfg, make_router):
    from cadence.llm import ToolCall
    bad = Completion(tool_calls=[ToolCall(id="x", name="bash", arguments="{not json")])
    router = make_router(big_script=[bad, Completion(content="recovered")])
    events = await collect(build_agent(cfg, router), "go")
    assert any("invalid JSON" in e.data.get("result", "") for e in events if e.type == "tool_result")
    assert events[-1].type == "message_done"


async def test_steering_reaches_the_model(cfg, make_router):
    router = make_router(big_script=[tool_call("list_files"), Completion(content="ok")])
    agent = build_agent(cfg, router)
    agent.steer("use pytest, not unittest")
    await collect(agent, "write tests")
    assert any("use pytest" in str(m.get("content")) for m in router.big.calls[0])


async def test_interrupt_during_tool_kills_command_and_keeps_context_valid(cfg, make_router):
    router = make_router(big_script=[tool_call("bash", command="sleep 30", timeout=60)])
    agent = build_agent(cfg, router)

    async def run():
        return [e async for e in agent.run("sleep")]

    task = asyncio.create_task(run())
    await asyncio.sleep(0.5)
    agent.interrupt()
    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1].type == "interrupted"
    # every tool_call must have a matching tool message so the next turn is valid
    calls = [tc["id"] for m in agent.ctx.messages if m.get("tool_calls") for tc in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in agent.ctx.messages if m["role"] == "tool"]
    assert calls == answered


async def test_todo_scratchpad_memory_tools(cfg, make_router):
    router = make_router(big_script=[
        tool_call("todo_write", todos=[{"content": "a", "status": "in_progress"}]),
        tool_call("scratchpad_set", key="plan", value="step1"),
        tool_call("memory_add", kind="procedural", text="always run pytest -q before finishing"),
        Completion(content="done"),
    ])
    agent = build_agent(cfg, router)
    await collect(agent, "x")
    assert agent.todos[0]["content"] == "a"
    assert agent.scratchpad.notes["plan"] == "step1"
    assert any("pytest" in h.text for h in agent.memory.search("pytest finishing"))
    assert "[~] a" in agent.system_prompt()


async def test_skill_progressive_disclosure(cfg, make_router):
    d = cfg.home / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: Use for demo tasks\n---\nSTEP ONE: be demo.\n")
    router = make_router(big_script=[tool_call("load_skill", name="demo"), Completion(content="ok")])
    agent = build_agent(cfg, router)
    prompt = agent.system_prompt()
    assert "demo: Use for demo tasks" in prompt and "STEP ONE" not in prompt  # body NOT preloaded
    events = await collect(agent, "demo please")
    assert "STEP ONE" in next(e for e in events if e.type == "tool_result").data["result"]


async def test_subagent_runs_with_fresh_context(cfg, make_router):
    router = make_router(big_script=[
        tool_call("spawn_subagent", task="count files"),
        Completion(content="3 files"),          # subagent final
        Completion(content="parent done: 3 files"),
    ])
    agent = build_agent(cfg, router)
    events = await collect(agent, "delegate")
    res = next(e for e in events if e.type == "tool_result")
    assert res.data["result"] == "3 files" and events[-1].data["text"].startswith("parent done")


async def test_iteration_limit_stops_runaway_loop(cfg, make_router):
    cfg.max_iterations = 3
    router = make_router(big_script=[tool_call("list_files") for _ in range(10)])
    events = await collect(build_agent(cfg, router), "loop")
    assert events[-1].type == "message_done" and "iteration limit" in events[-1].data["text"]


def test_tool_result_json_roundtrip():
    assert json.loads(json.dumps({"ok": 1}))
