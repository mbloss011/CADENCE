"""Rich rendering: brand banner, live turn renderer, status bar, dashboards."""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .agent import Event
from .goals import Goal

console = Console(highlight=False)

AMBER, ORANGE, SLATE = "#ffb000", "#ff6a00", "grey58"
STATUS_STYLE = {"proposed": "magenta", "pending": "yellow", "active": "cyan", "done": "green", "failed": "red", "blocked": "dark_orange"}


def banner(big: str, small: str, workspace: str) -> None:
    title = Text()
    title.append("◢◤ ", style=f"bold {ORANGE}")
    title.append("KESTREL", style="bold bright_white")
    body = Text(justify="center")
    body.append("async · autonomous · local-first coder agent\n", style=SLATE)
    body.append("big ", style=SLATE)
    body.append(big, style=f"bold {AMBER}")
    body.append("   small ", style=SLATE)
    body.append(small, style="bold cyan")
    body.append(f"\n{workspace}", style="dim")
    console.print(Panel(Align.center(body), title=title, border_style=ORANGE, padding=(1, 4)))


def pressure_bar(used: int, window: int, width: int = 16) -> Text:
    frac = min(1.0, used / max(1, window))
    filled = int(frac * width)
    color = "green" if frac < 0.5 else "yellow" if frac < 0.75 else "red"
    t = Text("ctx ", style=SLATE)
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="grey30")
    t.append(f" {frac:>4.0%} · {used / 1000:.1f}k/{window / 1000:.0f}k", style=SLATE)
    return t


def _fmt_args(raw: str, limit: int = 300) -> str:
    try:
        data = json.loads(raw or "{}")
        text = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:120]}" for k, v in data.items())
    except (json.JSONDecodeError, AttributeError):
        text = raw
    return text if len(text) <= limit else text[:limit] + "…"


class TurnRenderer:
    """Live-updating panels for one agent turn: thinking, tool calls/results, compaction, response."""

    def __init__(self, big: str, small: str):
        self._thinking = ""
        self._response = ""
        self._blocks: list[RenderableType] = []
        self._pending: dict[str, dict[str, Any]] = {}
        self._notes: list[Text] = []
        self._status = Text()
        self._error = ""
        self._interrupted = False
        self.big, self.small = big, small
        self._live = Live(self._render(), console=console, refresh_per_second=12, transient=False, vertical_overflow="visible")

    def __enter__(self) -> "TurnRenderer":
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._live.stop()
        return False

    def handle(self, ev: Event) -> None:
        d, t = ev.data, ev.type
        if t == "token":
            self._response += d["text"]
        elif t == "thinking":
            self._thinking += d["text"]
        elif t == "status":
            self._status = pressure_bar(d["tokens"], d["window"])
            self._status.append(f" · step {d['iteration']}", style=SLATE)
        elif t == "compaction":
            note = Text("⟲ ", style="cyan")
            note.append(f"small model · {d['stage']}: {d['detail']} ", style="cyan")
            note.append(f"({d['before'] / 1000:.1f}k → {d['after'] / 1000:.1f}k tokens)", style=SLATE)
            self._notes.append(note)
        elif t == "tool_call":
            self._flush_response()
            self._pending[d["id"]] = {"name": d["name"], "args": _fmt_args(d["arguments"])}
        elif t == "tool_result":
            call = self._pending.pop(d["id"], {"name": d["name"], "args": ""})
            self._blocks.append(self._tool_panel(call, d["result"]))
        elif t == "error":
            self._error = d["message"]
        elif t == "interrupted":
            self._interrupted = True
        self._live.update(self._render())

    def _flush_response(self) -> None:
        if self._response.strip():
            self._blocks.append(Panel(Markdown(self._response.strip()), border_style="green", title="✦", title_align="left", padding=(0, 1)))
        self._response = ""
        self._thinking = ""

    @staticmethod
    def _tool_panel(call: dict[str, Any], result: str) -> RenderableType:
        failed = result.startswith("ERROR")
        shown = result if len(result) <= 700 else result[:700] + f"\n… [{len(result) - 700} more chars]"
        head = Text()
        head.append(call["name"], style="bold cyan")
        head.append(f"({call['args']})", style=SLATE)
        body = Group(head, Text(shown, style="red" if failed else "grey70"))
        return Panel(body, title="✖ tool" if failed else "⚙ tool", title_align="left",
                     border_style="red" if failed else "grey35", padding=(0, 1))

    def _render(self) -> RenderableType:
        parts: list[RenderableType] = list(self._blocks)
        for call in self._pending.values():
            parts.append(Panel(Syntax(f"{call['name']}({call['args']})", "python", theme="ansi_dark", word_wrap=True),
                               title="⚙ running…", title_align="left", border_style=AMBER, padding=(0, 1)))
        if self._thinking.strip() and not self._response.strip():
            tail = self._thinking.strip()[-600:]
            parts.append(Panel(Text(tail, style="italic grey50"), title="◌ thinking", title_align="left", border_style="grey30", padding=(0, 1)))
        if self._response.strip():
            parts.append(Panel(Markdown(self._response.strip()), border_style="green", title="✦", title_align="left", padding=(0, 1)))
        parts.extend(self._notes)
        if self._error:
            parts.append(Panel(Text(self._error, style="red"), title="✖ error", border_style="red", padding=(0, 1)))
        if self._interrupted:
            parts.append(Text("⏹ interrupted — context kept; type to steer or continue", style=f"bold {AMBER}"))
        if self._status.plain:
            parts.append(self._status)
        if not parts:
            parts.append(Text("◌ waiting for model…", style="dim italic"))
        return Group(*parts)


def goals_table(goals: list[Goal], title: str = "goals") -> Table:
    table = Table(title=title, title_style=f"bold {AMBER}", border_style="grey35", show_lines=False)
    for col, style in (("id", "cyan"), ("status", None), ("src", "dim"), ("tries", "dim"), ("title", None)):
        table.add_column(col, style=style, no_wrap=col != "title")
    for g in goals:
        table.add_row(g.id, Text(g.status, style=STATUS_STYLE.get(g.status, "white")), g.source, str(g.attempts), g.title)
    return table


class Dashboard:
    """Live daemon view: goal table + rolling activity log."""

    def __init__(self, runtime: Any):
        self.rt = runtime
        self.log: deque[Text] = deque(maxlen=14)
        self.live = Live(self.render(), console=console, refresh_per_second=4, screen=False)

    def note(self, kind: str, message: str) -> None:
        colors = {"goal": "cyan", "cycle": "magenta", "info": SLATE, "tool": "grey50"}
        t = Text(f"{__import__('time').strftime('%H:%M:%S')} ", style="dim")
        t.append(message, style=colors.get(kind, "white"))
        self.log.append(t)
        self.live.update(self.render())

    def render(self) -> RenderableType:
        d = self.rt.daemon
        status = Text()
        if d:
            status.append(f"heartbeat {d.last_tick or '—'} · cycles {d.cycles_today}/{self.rt.cfg.reflection_cycles} · "
                          f"running {len(d.running)}/{self.rt.cfg.max_parallel_goals}", style=SLATE)
            if d.paused:
                status.append(" · PAUSED", style="bold red")
        return Group(goals_table(self.rt.goals.list()[-12:]), Panel(Group(*self.log) if self.log else Text("quiet", style="dim"),
                     title="activity", title_align="left", border_style="grey35"), status)
