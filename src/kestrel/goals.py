"""Goals, the reflection loop, and autonomous idea generation.

A goal runs as:  act → verify (objective command, if any) → reflect (big model) → done | retry | blocked.
Lessons from every reflection are written to procedural memory; outcomes to episodic memory.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .agent import Agent, Event
from .config import Config, SandboxConfig
from .hooks import Hooks
from .llm import LLMError, ModelRouter
from .memory import MemoryStore
from .sandbox import Sandbox, SandboxError
from .tools import truncate

STATUSES = ("proposed", "pending", "active", "done", "failed", "blocked")


@dataclass
class Goal:
    title: str
    description: str = ""
    acceptance: list[str] = field(default_factory=list)
    verify_command: str = ""
    status: str = "pending"
    source: str = "user"  # user | auto
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    attempts: int = 0
    reflections: list[dict[str, Any]] = field(default_factory=list)
    result: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


class GoalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._goals: dict[str, Goal] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in raw:
            try:
                self._goals[item["id"]] = Goal(**item)
            except TypeError:
                continue

    def _save(self) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([asdict(g) for g in self._goals.values()], fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file

    def add(self, goal: Goal) -> Goal:
        with self._lock:
            self._goals[goal.id] = goal
            self._save()
        return goal

    def get(self, goal_id: str) -> Goal | None:
        matches = [g for gid, g in self._goals.items() if gid.startswith(goal_id)]
        return matches[0] if len(matches) == 1 else None

    def update(self, goal: Goal, **changes: Any) -> Goal:
        with self._lock:
            for k, v in changes.items():
                setattr(goal, k, v)
            goal.updated = time.time()
            self._save()
        return goal

    def list(self, status: str | None = None) -> list[Goal]:
        goals = sorted(self._goals.values(), key=lambda g: g.created)
        return [g for g in goals if not status or g.status == status]

    def recover_interrupted(self) -> int:
        """Goals left `active` by a crash go back to the queue."""
        n = 0
        for g in self.list("active"):
            self.update(g, status="pending")
            n += 1
        return n


def extract_json(text: str) -> Any | None:
    """Pull the first JSON object/array out of model output (handles fences and chatter)."""
    text = re.sub(r"```(?:json)?", "", text)
    pairs = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1]
    for start, _opener, closer in sorted(pairs):  # outermost (earliest) structure wins
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(re.findall(r"[a-z0-9]{3,}", a.lower())), set(re.findall(r"[a-z0-9]{3,}", b.lower()))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


REFLECT_SYSTEM = (
    "You are the reflection stage of an autonomous coding agent. Judge strictly from the evidence given. "
    'Reply with ONLY JSON: {"verdict":"done|retry|blocked","critique":"...","next_steps":["..."],"lessons":["..."]}. '
    "verdict=done only if every acceptance criterion is demonstrably met. blocked = needs a human or missing access. "
    "lessons = short, reusable, imperative rules worth remembering."
)
IDEA_SYSTEM = (
    "You propose high-value, SMALL, verifiable engineering tasks for a codebase. "
    'Reply with ONLY a JSON array of up to {n} objects: {{"title","description","acceptance":["..."],"verify_command":"shell command or empty"}}. '
    "Prefer: missing tests, bug risks, dead code, docs gaps, performance wins. Never propose destructive or out-of-workspace work."
)

EventSink = Callable[[str, Event], Awaitable[None]]


class GoalRunner:
    def __init__(self, cfg: Config, router: ModelRouter, store: GoalStore, memory: MemoryStore,
                 agent_factory: Callable[[str], Agent], sandbox: Sandbox | None = None, hooks: Hooks | None = None):
        self.cfg, self.router, self.store, self.memory, self.agent_factory = cfg, router, store, memory, agent_factory
        self.sandbox = sandbox or Sandbox(SandboxConfig(mode="none"), cfg.workspace)
        self.hooks = hooks or Hooks(workspace=cfg.workspace)
        self.agents: dict[str, Agent] = {}  # live goal agents, for steering/interrupt

    async def run(self, goal: Goal, sink: EventSink | None = None) -> Goal:
        self.store.update(goal, status="active")
        agent = self.agent_factory(f"goal-{goal.id}")
        self.agents[goal.id] = agent
        prompt = self._initial_prompt(goal)
        try:
            for attempt in range(1, self.cfg.max_reflections_per_goal + 2):
                self.store.update(goal, attempts=attempt)
                final = await self._act(agent, goal, prompt, sink)
                if final is None:  # interrupted
                    self.store.update(goal, status="pending", result="interrupted; will resume")
                    return goal
                verify = await self._verify(goal)
                reflection = await self._reflect(goal, final, verify)
                goal.reflections.append({"attempt": attempt, "verify": verify, **reflection})
                for lesson in reflection.get("lessons", []):
                    self.memory.add("procedural", str(lesson), ["lesson", "auto"])
                verdict = reflection["verdict"]
                if verdict == "done":
                    self.store.update(goal, status="done", result=final)
                    break
                if verdict == "blocked":
                    self.store.update(goal, status="blocked", result=reflection.get("critique", final))
                    break
                if attempt > self.cfg.max_reflections_per_goal:
                    self.store.update(goal, status="failed", result=f"gave up after {attempt} attempts: {reflection.get('critique', '')}")
                    break
                steps = "\n".join(f"- {s}" for s in reflection.get("next_steps", []))
                prompt = (f"Reflection on attempt {attempt}: {reflection.get('critique', '')}\n"
                          f"Verification: {verify or 'n/a'}\nNext steps:\n{steps}\nContinue working on the goal until the acceptance criteria are met.")
            self.memory.add("episodic", f"Goal '{goal.title}' → {goal.status} after {goal.attempts} attempt(s). {truncate(goal.result, 300)}", ["goal"])
            self.store.update(goal)
            await self.hooks.emit("goal_done", {"agent": agent.name, "goal_id": goal.id, "title": goal.title,
                                                "status": goal.status, "result": truncate(goal.result, 2000)})
        except LLMError as exc:
            self.store.update(goal, status="pending", result=f"model unavailable: {exc}")
        finally:
            self.agents.pop(goal.id, None)
        return goal

    async def _act(self, agent: Agent, goal: Goal, prompt: str, sink: EventSink | None) -> str | None:
        final = ""
        async for ev in agent.run(prompt):
            if sink:
                await sink(goal.id, ev)
            if ev.type == "message_done":
                final = ev.data["text"]
            elif ev.type == "error":
                raise LLMError(ev.data["message"])
            elif ev.type == "interrupted":
                return None
        return final

    @staticmethod
    def _initial_prompt(goal: Goal) -> str:
        parts = [f"GOAL: {goal.title}"]
        if goal.description:
            parts.append(goal.description)
        if goal.acceptance:
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {a}" for a in goal.acceptance))
        if goal.verify_command:
            parts.append(f"Objective check (must exit 0): `{goal.verify_command}`")
        return "\n\n".join(parts)

    async def _verify(self, goal: Goal) -> dict[str, Any] | None:
        if not goal.verify_command:
            return None
        try:
            res = await self.sandbox.run(goal.verify_command, timeout=300)
        except SandboxError as exc:
            return {"exit": -1, "output": f"sandbox unavailable: {exc}"}
        if res.timed_out:
            return {"exit": -1, "output": "verification timed out"}
        return {"exit": res.exit, "output": truncate(res.output, 1500)}

    async def _reflect(self, goal: Goal, final: str, verify: dict[str, Any] | None) -> dict[str, Any]:
        evidence = (f"GOAL: {goal.title}\n{goal.description}\nAcceptance: {goal.acceptance}\n"
                    f"AGENT FINAL REPORT:\n{truncate(final, 3000)}\nVERIFICATION: {json.dumps(verify) if verify else 'none'}")
        parsed: Any = None
        try:
            comp = await self.router.big.complete(
                [{"role": "system", "content": REFLECT_SYSTEM}, {"role": "user", "content": evidence}],
                max_tokens=700, temperature=0.1)
            parsed = extract_json(comp.content)
        except LLMError:
            parsed = None
        reflection = parsed if isinstance(parsed, dict) else {}
        verdict = reflection.get("verdict")
        if verdict not in ("done", "retry", "blocked"):
            verdict = "done" if (verify and verify["exit"] == 0) else "retry"
            reflection.setdefault("critique", "reflection output unparseable; decided from verification")
        if verify is not None:  # objective evidence outranks model opinion
            if verify["exit"] != 0 and verdict == "done":
                verdict = "retry"
                reflection["critique"] = f"verification failed (exit {verify['exit']}): {verify['output'][:300]}"
            elif verify["exit"] == 0 and verdict == "retry":
                verdict = "done"
        return {
            "verdict": verdict,
            "critique": str(reflection.get("critique", "")),
            "next_steps": [str(s) for s in reflection.get("next_steps", []) if s][:6],
            "lessons": [str(s) for s in reflection.get("lessons", []) if s][:4],
        }

    # --------------------------------------------------------------- ideation
    async def propose(self, n: int = 3) -> list[Goal]:
        tree = _tree_snapshot(self.cfg.workspace)
        recent = "\n".join(f"- {g.title} [{g.status}]" for g in self.store.list()[-15:]) or "(none)"
        lessons = "\n".join(f"- {m.text}" for m in self.memory.recent(8, "procedural")) or "(none)"
        user = f"Workspace files:\n{tree}\n\nExisting goals (do not duplicate):\n{recent}\n\nLessons learned:\n{lessons}"
        try:
            comp = await self.router.big.complete(
                [{"role": "system", "content": IDEA_SYSTEM.format(n=n)}, {"role": "user", "content": user}],
                max_tokens=1200, temperature=0.6)
        except LLMError:
            return []
        items = extract_json(comp.content)
        if not isinstance(items, list):
            return []
        existing = [g.title for g in self.store.list()]
        added: list[Goal] = []
        for it in items[:n]:
            if not isinstance(it, dict) or not str(it.get("title", "")).strip():
                continue
            title = str(it["title"]).strip()
            if any(_jaccard(title, e) >= 0.6 for e in existing):
                continue
            goal = Goal(
                title=title, description=str(it.get("description", "")),
                acceptance=[str(a) for a in it.get("acceptance", [])][:6],
                verify_command=str(it.get("verify_command", "") or ""),
                status="pending" if self.cfg.auto_approve_ideas else "proposed", source="auto")
            self.store.add(goal)
            existing.append(title)
            added.append(goal)
        return added

    async def consolidate_lessons(self) -> int:
        """Improvement cycle: the small model distills recent reflections into durable rules."""
        recent = [r for g in self.store.list()[-10:] for r in g.reflections[-2:] if r.get("critique")]
        if not recent:
            return 0
        text = "\n".join(f"- {r['verdict']}: {r['critique']}" for r in recent)
        try:
            comp = await self.router.small.complete(
                [{"role": "system", "content": "Extract up to 3 short, reusable, imperative engineering rules from these critiques. One per line, no numbering."},
                 {"role": "user", "content": text}], max_tokens=300, temperature=0.1)
        except LLMError:
            return 0
        n = 0
        for line in comp.content.splitlines():
            line = line.strip("-*• \t")
            if 12 <= len(line) <= 240:
                self.memory.add("procedural", line, ["consolidated"])
                n += 1
        return n


def _tree_snapshot(root: Path, limit: int = 80) -> str:
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".kestrel"}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        depth = Path(dirpath).relative_to(root).parts
        if len(depth) > 3:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            out.append(str(Path(dirpath, fn).relative_to(root)))
            if len(out) >= limit:
                return "\n".join(out) + "\n…"
    return "\n".join(out) or "(empty workspace)"
