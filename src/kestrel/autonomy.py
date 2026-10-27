"""Heartbeat + event-driven daemon.

* Event-driven: `submit()` / `wake()` interrupt the sleep immediately.
* Heartbeat: every `24h / reflection_cycles` it runs an idea cycle (propose goals) and an
  improvement cycle (consolidate lessons) when nothing else is queued.
* Concurrency: up to `max_parallel_goals` goals run as independent asyncio tasks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Awaitable, Callable

from .agent import Event
from .config import Config
from .goals import Goal, GoalRunner, GoalStore

log = logging.getLogger(__name__)
Notify = Callable[[str, str], Awaitable[None]]  # (kind, message)


class Daemon:
    def __init__(self, cfg: Config, runner: GoalRunner, store: GoalStore, notify: Notify | None = None,
                 event_sink: Callable[[str, Event], Awaitable[None]] | None = None):
        self.cfg, self.runner, self.store = cfg, runner, store
        self.notify = notify
        self.event_sink = event_sink
        self.paused = False
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[Goal]] = {}
        self._state_path = cfg.state_dir / "daemon.json"
        self.last_tick = ""
        self.cycles_today = 0
        self._day = dt.date.today().isoformat()
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if data.get("day") == self._day:
                self.cycles_today = int(data.get("cycles", 0))
        except (OSError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"day": self._day, "cycles": self.cycles_today}), encoding="utf-8")

    def _roll_day(self) -> None:
        today = dt.date.today().isoformat()
        if today != self._day:
            self._day, self.cycles_today = today, 0
            self._save_state()

    @property
    def interval(self) -> float:
        return 86400.0 / max(1, self.cfg.reflection_cycles)

    @property
    def running(self) -> list[str]:
        return list(self._tasks)

    # ------------------------------------------------------------------- API
    def submit(self, goal: Goal) -> Goal:
        self.store.add(goal)
        self._wake.set()
        return goal

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        recovered = self.store.recover_interrupted()
        if recovered:
            await self._say("info", f"re-queued {recovered} goal(s) interrupted by a previous shutdown")
        next_cycle = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self._roll_day()
            self._reap()
            if not self.paused:
                self._launch_ready()
                if not self._tasks and loop.time() >= next_cycle and self.cycles_today < self.cfg.reflection_cycles:
                    await self._cycle()
                    next_cycle = loop.time() + self.interval
            self.last_tick = dt.datetime.now().strftime("%H:%M:%S")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(30.0, self.interval))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # -------------------------------------------------------------- internals
    def _launch_ready(self) -> None:
        free = self.cfg.max_parallel_goals - len(self._tasks)
        if free <= 0:
            return
        for goal in self.store.list("pending")[:free]:
            if goal.id not in self._tasks:
                self._tasks[goal.id] = asyncio.create_task(self._run_goal(goal), name=f"goal-{goal.id}")

    async def _run_goal(self, goal: Goal) -> Goal:
        await self._say("goal", f"▶ {goal.title}")
        result = await self.runner.run(goal, self.event_sink)
        await self._say("goal", f"{'✔' if result.status == 'done' else '✖'} {goal.title} → {result.status}")
        self._wake.set()
        return result

    def _reap(self) -> None:
        for gid, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[gid]
                if not task.cancelled() and task.exception():
                    log.error("goal %s crashed: %s", gid, task.exception())

    async def _cycle(self) -> None:
        self.cycles_today += 1
        self._save_state()
        added = await self.runner.propose(3)
        learned = await self.runner.consolidate_lessons()
        await self._say("cycle", f"cycle {self.cycles_today}/{self.cfg.reflection_cycles}: {len(added)} idea(s), {learned} lesson(s) consolidated")
        if added and self.cfg.auto_approve_ideas:
            self._wake.set()

    async def _say(self, kind: str, message: str) -> None:
        if self.notify:
            await self.notify(kind, message)
