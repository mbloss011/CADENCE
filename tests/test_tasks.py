from __future__ import annotations

import json

from conftest import tool_call

from kestrel.agent import Agent
from kestrel.tasks import Task, TaskRunner, TaskStore, extract_json
from kestrel.llm import Completion
from kestrel.memory import MemoryStore
from kestrel.skills import SkillRegistry
from kestrel.tools import ToolRegistry, Workspace, builtin_tools


def make_runner(cfg, router):
    reg = ToolRegistry()
    for t in builtin_tools(Workspace(cfg.workspace), allow_shell=True, output_limit=4000):
        reg.register(t)
    skills = SkillRegistry([])
    memory = MemoryStore(cfg.state_dir / "m.jsonl")
    store = TaskStore(cfg.state_dir / "tasks.json")
    runner = TaskRunner(cfg, router, store, memory, lambda name, project="": Agent(cfg, router, reg, skills, memory, name=name))
    return runner, store, memory


def reflection(verdict, **extra):
    return Completion(content=json.dumps({"verdict": verdict, "critique": "c", "next_steps": ["n"], "lessons": extra.get("lessons", [])}))


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('sure!\n```json\n{"a": 1}\n```\nbye') == {"a": 1}
    assert extract_json("no json here") is None


async def test_retry_then_done_with_objective_verification(cfg, make_router):
    # attempt 1 does nothing useful → verify fails → retry; attempt 2 creates the file → verify passes
    router = make_router(big_script=[
        Completion(content="I think it is done"),
        reflection("done"),                                   # model is over-optimistic; verify must overrule it
        tool_call("write_file", path="ok.txt", content="x"),
        Completion(content="created ok.txt"),
        reflection("retry", lessons=["verify before claiming done"]),  # model pessimistic; verify overrules → done
    ])
    runner, store, memory = make_runner(cfg, router)
    goal = store.add(Task(title="create ok.txt", verify_command="test -f ok.txt"))
    await runner.run(goal)
    assert goal.status == "done" and goal.attempts == 2
    assert goal.reflections[0]["verdict"] == "retry" and "verification failed" in goal.reflections[0]["critique"]
    assert any("verify before claiming" in m.text for m in memory.search("verify claiming done"))
    assert any(m.kind == "episodic" for m in memory.recent(5))


async def test_gives_up_after_max_reflections(cfg, make_router):
    cfg.max_reflections_per_goal = 1
    router = make_router(big_script=[Completion(content="no"), reflection("retry"), Completion(content="still no"), reflection("retry")])
    runner, store, _ = make_runner(cfg, router)
    goal = store.add(Task(title="impossible", verify_command="false"))
    await runner.run(goal)
    assert goal.status == "failed" and goal.attempts == 2


async def test_blocked_verdict_stops_early(cfg, make_router):
    router = make_router(big_script=[Completion(content="need api key"), reflection("blocked")])
    runner, store, _ = make_runner(cfg, router)
    goal = store.add(Task(title="needs creds"))
    await runner.run(goal)
    assert goal.status == "blocked" and goal.attempts == 1


async def test_unparseable_reflection_falls_back_to_verification(cfg, make_router):
    router = make_router(big_script=[Completion(content="done"), Completion(content="blah blah not json")])
    runner, store, _ = make_runner(cfg, router)
    goal = store.add(Task(title="t", verify_command="true"))
    await runner.run(goal)
    assert goal.status == "done"


async def test_ideation_fills_the_inbox_without_running_anything(cfg, make_router):
    from kestrel.ideas import IdeaStore, ideate, promote_to_task

    raw = json.dumps([
        {"title": "Add unit tests for parser", "description": "d", "acceptance": ["tests pass"], "verify_command": "pytest -q"},
        {"title": "Add unit tests for the parser module", "description": "dup"},
        {"title": "Ship a plugin marketplace", "size": "goal"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    _runner, store, memory = make_runner(cfg, router)
    ideas = IdeaStore(cfg.state_dir / "ideas.json")
    added = await ideate(router, ideas, store, memory, project="p1", workspace=cfg.workspace, n=5)
    assert [i.title for i in added] == ["Add unit tests for parser", "Ship a plugin marketplace"]
    assert [i.size for i in added] == ["task", "goal"]
    assert all(i.status == "new" and i.source == "auto" and i.project == "p1" for i in added)
    assert store.list() == []  # nothing runs until an idea is promoted
    task = promote_to_task(ideas, store, added[0])
    assert task.verify_command == "pytest -q" and task.project == "p1" and task.source == "idea"
    assert ideas.get(added[0].id).promoted_to == f"task:{task.id}"


async def test_store_is_persistent_and_recovers_active_goals(cfg):
    store = TaskStore(cfg.state_dir / "g.json")
    g = store.add(Task(title="persist me"))
    store.update(g, status="active")
    reloaded = TaskStore(cfg.state_dir / "g.json")
    assert reloaded.recover_interrupted() == 1
    assert reloaded.get(g.id).status == "pending"


def test_extract_json_prefers_outermost_array():
    assert extract_json('[{"title": "a"}]') == [{"title": "a"}]
    assert extract_json('here: {"k": [1, 2]} done') == {"k": [1, 2]}
