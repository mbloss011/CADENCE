"""Cadence command-line interface (Rich + prompt_toolkit). Installed as both `cadence` and `cadence`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .agent import Agent, render_todos
from .config import Config, load_config
from .goals import Goal
from .llm import LLMError, OpenAICompatLLM
from .runtime import Runtime
from .tasks import Task
from .ui import ACCENT, Dashboard, TurnRenderer, banner, console, goals_table, ideas_table, long_goals_table, pressure_bar

HELP = [
    ("/help", "show this help"),
    ("/task <text>", "run the text now as a task with verify → reflect → retry"),
    ("/goal <text>", "set a long-horizon goal for the daemon's planner to work toward"),
    ("/goals /tasks /ideas", "list goals, tasks, the idea inbox   /approve <id>  promote an idea / approve a task"),
    ("/tools /skills /mcp", "inspect tools, skills, MCP servers"),
    ("/todos", "show the agent's task list"),
    ("/context", "context usage         /compact  force small-model compaction"),
    ("/memory <query>", "search long-term memory (semantic + keyword)"),
    ("/image <path> [prompt]", "send an image (screenshot, diagram, photo) with your message"),
    ("/plugins  /hooks", "loaded plugins and lifecycle hooks"),
    ("/new  /resume <id>", "fresh session / resume a saved one"),
    ("/quit", "exit (Ctrl+C interrupts a running turn but keeps context)"),
]


# ───────────────────────────────────────────────────────────────────── chat
async def chat(rt: Runtime, resume: str | None) -> int:
    agent = rt.new_agent("main")
    if resume:
        try:
            agent.load_session(resume)
        except (OSError, ValueError) as exc:
            console.print(f"[red]cannot resume '{resume}': {exc}[/red]")
            return 1
    banner(rt.cfg.big.name, rt.cfg.small.name, str(rt.cfg.workspace))
    _print_startup(rt)
    console.print("[dim]/help for commands · Ctrl+C interrupts a turn[/dim]\n")
    session = _prompt_session(rt.cfg)

    while True:
        try:
            line = (await session.prompt_async("❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.startswith("/"):
            agent, keep_going = await _command(rt, agent, line)
            if not keep_going:
                break
            continue
        await _run_turn(rt, agent, line)
    agent.save_session()
    console.print(f"[dim]session {agent.session_id} saved · resume with: cadence chat --resume {agent.session_id}[/dim]")
    return 0


async def _run_turn(rt: Runtime, agent: Agent, text: str, images: list[Path] | None = None) -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, agent.interrupt)
    try:
        with TurnRenderer(rt.cfg.big.name, rt.cfg.small.name) as view:
            async for ev in agent.run(text, images):
                view.handle(ev)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
    console.print()


async def _command(rt: Runtime, agent: Agent, line: str) -> tuple[Agent, bool]:
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    if cmd in ("/quit", "/exit"):
        return agent, False
    if cmd == "/help":
        table = Table(show_header=False, box=None, padding=(0, 2))
        for k, v in HELP:
            table.add_row(f"[bold {ACCENT}]{k}[/]", v)
        console.print(table)
    elif cmd == "/tools":
        table = Table(title="tools", border_style="grey35")
        table.add_column("name", style="cyan")
        table.add_column("source", style="dim")
        table.add_column("description")
        for t in agent.registry.all().values():
            table.add_row(t.name, t.source, t.description[:80])
        console.print(table)
    elif cmd == "/skills":
        if not rt.skills.skills:
            console.print("[dim]no skills found in: " + ", ".join(str(d) for d in rt.cfg.skill_dirs) + "[/dim]")
        for s in rt.skills.skills.values():
            console.print(f"[bold cyan]{s.name}[/] — {s.description}")
        for err in rt.skills.errors:
            console.print(f"[red]skill error:[/red] {err}")
    elif cmd == "/mcp":
        _print_mcp(rt)
    elif cmd == "/todos":
        console.print(render_todos(agent.todos) if agent.todos else "[dim]no tasks yet[/dim]")
    elif cmd == "/context":
        sys_prompt = agent.system_prompt()
        console.print(pressure_bar(agent.ctx.usage(sys_prompt), agent.ctx.window))
        console.print(f"[dim]{len(agent.ctx.messages)} messages · summary {len(agent.ctx.summary)} chars · {agent.ctx.compactions} compactions[/dim]")
    elif cmd == "/compact":
        events = await _force_compact(agent)
        for ev in events:
            console.print(f"[cyan]⟲ {ev.stage}: {ev.detail} ({ev.tokens_before / 1000:.1f}k → {ev.tokens_after / 1000:.1f}k)[/cyan]")
        if not events:
            console.print("[dim]nothing to compact[/dim]")
    elif cmd == "/memory":
        _print_memory_hits(rt, await rt.memory.asearch(arg, k=8))
    elif cmd == "/image":
        path_s, _, prompt = arg.partition(" ")
        if not path_s:
            console.print("[red]usage: /image <path> [prompt][/red]")
        else:
            path = Path(path_s).expanduser()
            path = path if path.is_absolute() else rt.cfg.workspace / path
            await _run_turn(rt, agent, prompt.strip() or "Describe this image.", [path])
    elif cmd == "/plugins":
        _print_plugins(rt)
    elif cmd == "/hooks":
        _print_hooks(rt)
    elif cmd == "/new":
        agent.save_session()
        agent = rt.new_agent("main")
        console.print("[green]new session[/green]")
    elif cmd == "/resume":
        fresh = rt.new_agent("main")
        try:
            fresh.load_session(arg)
            agent = fresh
            console.print(f"[green]resumed {arg}[/green]")
        except (OSError, ValueError):
            console.print(f"[red]no such session: {arg}[/red]")
    elif cmd == "/tasks":
        console.print(goals_table(rt.tasks.list(), "tasks"))
    elif cmd == "/goals":
        console.print(long_goals_table(rt.goals.all()))
    elif cmd == "/ideas":
        console.print(ideas_table(rt.ideas.list("new")))
    elif cmd == "/approve":
        console.print(_approve(rt, arg))
    elif cmd == "/goal":
        if not arg:
            console.print("[red]usage: /goal <what you want to achieve>[/red]")
        else:
            g = rt.goals.add(Goal(title=arg[:160], description=arg, project=agent.project))
            console.print(f"[green]goal {g.id} set[/green] — `cadence web` or `cadence daemon` plans and works toward it")
    elif cmd == "/task":
        if not arg:
            console.print("[red]usage: /task <description>[/red]")
        else:
            await _run_goal_live(rt, Task(title=arg[:80], description=arg, project=agent.project))
    else:
        console.print(f"[red]unknown command {cmd}[/red] — /help")
    return agent, True


async def _force_compact(agent: Agent):
    saved = agent.cfg.compact_trigger, agent.cfg.compact_target
    agent.cfg.compact_trigger, agent.cfg.compact_target = 0.0, 0.05
    try:
        return await agent.ctx.maybe_compact(agent.system_prompt())
    finally:
        agent.cfg.compact_trigger, agent.cfg.compact_target = saved


async def _run_goal_live(rt: Runtime, goal: Task) -> None:
    rt.tasks.add(goal)
    renderer_holder: dict[str, TurnRenderer] = {}

    async def sink(_gid: str, ev) -> None:
        view = renderer_holder.get("v")
        if view:
            view.handle(ev)

    console.print(Panel(f"[bold]{goal.title}[/bold]", title=f"goal {goal.id}", border_style=ACCENT))
    loop = asyncio.get_running_loop()

    def interrupt_goal() -> None:
        live_agent = rt.runner.agents.get(goal.id)
        if live_agent:
            live_agent.interrupt()

    with TurnRenderer(rt.cfg.big.name, rt.cfg.small.name) as view:
        renderer_holder["v"] = view
        loop.add_signal_handler(signal.SIGINT, interrupt_goal)
        try:
            await rt.runner.run(goal, sink)
        finally:
            loop.remove_signal_handler(signal.SIGINT)
    _print_goal_result(goal)


def _approve(rt: Runtime, ref: str) -> str:
    """Promote an idea to a task, or approve a proposed task."""
    from .ideas import promote_to_task

    idea = rt.ideas.get(ref)
    if idea and idea.status == "new":
        task = promote_to_task(rt.ideas, rt.tasks, idea)
        return f"[green]idea {idea.id} → task {task.id}[/green]"
    task = rt.tasks.get(ref)
    if task and task.status == "proposed":
        rt.tasks.update(task, status="pending")
        return f"[green]approved {task.id}[/green]"
    return "[red]no such new idea or proposed task[/red]"


def _print_goal_result(goal: Task) -> None:
    color = {"done": "green", "failed": "red", "blocked": "dark_orange"}.get(goal.status, "yellow")
    console.print()
    console.print(Panel(Text(goal.result or "(no result)", style="white"), title=f"{goal.status} · {goal.attempts} attempt(s)", border_style=color))
    for r in goal.reflections:
        console.print(f"[dim]reflection {r['attempt']}: {r['verdict']} — {r.get('critique', '')[:160]}[/dim]")


# ─────────────────────────────────────────────────────────────── subcommands
async def cmd_run(rt: Runtime, args: argparse.Namespace) -> int:
    goal = Task(title=args.goal[:80], description=args.goal, acceptance=args.accept or [], verify_command=args.verify or "")
    banner(rt.cfg.big.name, rt.cfg.small.name, str(rt.cfg.workspace))
    if args.image:
        # Images are understood up front and handed to the goal as text, so every retry can use them.
        from .vision import ImageError, describe
        notes = []
        for p in args.image:
            try:
                notes.append(f"Image {p}:\n{await describe(rt.router.vision, p.expanduser().resolve(), goal.description)}")
            except (ImageError, LLMError) as exc:
                console.print(f"[red]cannot use image {p}: {exc}[/red]")
                return 1
        goal.description += "\n\nAttached images (described by the vision model):\n" + "\n\n".join(notes)
    await _run_goal_live(rt, goal)
    return 0 if goal.status == "done" else 1


async def cmd_daemon(rt: Runtime, args: argparse.Namespace) -> int:
    from .channels import build_channels

    dash = Dashboard(rt)
    try:
        channels = [] if args.no_channels else build_channels(rt)
    except ValueError as exc:
        console.print(f"[red]channel config error: {exc}[/red]")
        return 1

    async def notify(kind: str, message: str) -> None:
        dash.note(kind, message)
        if kind in ("task", "goal") and message[:1] in "✔✖★?":
            for ch in channels:
                await ch.broadcast(message)

    async def sink(gid: str, ev) -> None:
        if ev.type == "tool_call":
            dash.note("tool", f"{gid[-8:]} ⚙ {ev.data['name']}")
        elif ev.type == "compaction":
            dash.note("info", f"{gid[-8:]} ⟲ {ev.data['stage']}: {ev.data['detail']}")

    daemon = rt.make_daemon(notify, sink)
    banner(rt.cfg.big.name, rt.cfg.small.name, str(rt.cfg.workspace))
    console.print(f"[dim]heartbeat every {daemon.interval / 3600:.1f}h · {rt.cfg.reflection_cycles} idea cycles/day · "
                  f"{'ideas auto-approved' if rt.cfg.auto_approve_ideas else 'ideas need approval'} · Ctrl+C to stop[/dim]\n")
    loop = asyncio.get_running_loop()
    stop_channels = asyncio.Event()

    def stop_all() -> None:
        daemon.stop()
        stop_channels.set()

    loop.add_signal_handler(signal.SIGINT, stop_all)
    loop.add_signal_handler(signal.SIGTERM, stop_all)
    channel_tasks = [asyncio.create_task(_run_channel(ch, stop_channels, dash), name=f"channel-{ch.name}") for ch in channels]
    with dash.live:
        ticker = asyncio.create_task(_refresh(dash, daemon))
        try:
            await daemon.run_forever()
        finally:
            ticker.cancel()
            stop_channels.set()
            await asyncio.gather(*channel_tasks, return_exceptions=True)
            for ch in channels:
                ch.bridge.save_all()
    console.print("[dim]daemon stopped[/dim]")
    return 0


async def _run_channel(ch, stop: asyncio.Event, dash: Dashboard) -> None:
    dash.note("info", f"channel {ch.name} starting")
    try:
        await ch.run(stop)
    except Exception as exc:  # noqa: BLE001 — a dead channel must not stop the daemon
        dash.note("goal", f"✖ channel {ch.name} stopped: {exc.__class__.__name__}: {exc}")


async def _refresh(dash: Dashboard, daemon) -> None:
    while True:
        dash.live.update(dash.render())
        await asyncio.sleep(1.0)


async def cmd_tasks(rt: Runtime, args: argparse.Namespace) -> int:
    if args.action == "add":
        task = rt.tasks.add(Task(title=args.text[:80], description=args.text, acceptance=args.accept or [], verify_command=args.verify or ""))
        console.print(f"[green]queued[/green] {task.id} — start `cadence daemon` (or `cadence web`) to run it")
    elif args.action == "approve":
        console.print(_approve(rt, args.text))
    elif args.action == "show":
        task = rt.tasks.get(args.text)
        if not task:
            console.print("[red]no such task[/red]")
            return 1
        _print_goal_result(task)
    else:
        console.print(goals_table(rt.tasks.list(), "tasks"))
    return 0


async def cmd_goals(rt: Runtime, args: argparse.Namespace) -> int:
    if args.action == "add":
        if not args.text:
            console.print("[red]usage: cadence goals add \"what you want to achieve\" [--success CRITERION ...][/red]")
            return 1
        g = rt.goals.add(Goal(title=args.text[:160], description=args.text, success=args.success or []))
        rt.goals.log(g, "created", "goal set from the CLI")
        console.print(f"[green]goal {g.id} set[/green] — the daemon plans tasks toward it")
    elif args.action in ("pause", "resume", "archive"):
        g = rt.goals.get(args.text)
        if not g:
            console.print("[red]no such goal[/red]")
            return 1
        rt.goals.update(g, status={"pause": "paused", "resume": "active", "archive": "archived"}[args.action], question="")
        console.print(f"{g.id} → {g.status}")
    elif args.action == "show":
        g = rt.goals.get(args.text)
        if not g:
            console.print("[red]no such goal[/red]")
            return 1
        console.print(Panel(f"{g.description}\n\n[bold]Success:[/bold] " + "; ".join(g.success) + f"\n\n{g.assessment}",
                            title=f"{g.title} · {g.status} · {g.progress}%", border_style=ACCENT))
        for e in g.journal[-15:]:
            console.print(f"[dim]{e['kind']:>8}[/dim] {e['text'][:200]}")
        console.print(goals_table(rt.tasks.list(goal=g.id), "tasks for this goal"))
    else:
        console.print(long_goals_table(rt.goals.all()))
    return 0


async def cmd_ideas(rt: Runtime, args: argparse.Namespace) -> int:
    from .ideas import Idea

    if args.action == "add":
        i = rt.ideas.add(Idea(title=args.text[:160], description=args.text))
        console.print(f"[green]idea {i.id} added[/green]")
    elif args.action == "promote":
        console.print(_approve(rt, args.text))
    elif args.action == "dismiss":
        i = rt.ideas.get(args.text)
        if not i:
            console.print("[red]no such idea[/red]")
            return 1
        rt.ideas.update(i, status="dismissed")
        console.print(f"dismissed {i.id}")
    else:
        console.print(ideas_table(rt.ideas.list("new")))
    return 0


async def cmd_web(rt: Runtime, args: argparse.Namespace) -> int:
    from .web.server import STATIC, serve

    def ready(url: str, token: str | None) -> None:
        banner(rt.cfg.big.name, rt.cfg.small.name, str(rt.cfg.workspace))
        console.print(Panel(Text(url, style=f"bold {ACCENT}"), title="web UI", border_style=ACCENT, expand=False))
        bits = ["daemon on" if not args.no_daemon else "daemon off", f"sandbox {rt.sandbox.mode}",
                f"{len(rt.cfg.channels)} channel(s)" if rt.cfg.channels and not args.no_channels else "no channels"]
        console.print("[dim]" + " · ".join(bits) + " · Ctrl+C to stop[/dim]")
        if not (STATIC / "index.html").is_file():
            console.print("[yellow]UI not built yet: cd web && npm install && npm run build[/yellow]")
        if args.open:
            import webbrowser
            webbrowser.open(url)

    try:
        await serve(rt, args.host, args.port, daemon=not args.no_daemon, channels=not args.no_channels,
                    token=args.token, on_ready=ready)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except OSError as exc:
        console.print(f"[red]cannot listen on {args.host}:{args.port}: {exc}[/red]")
        return 1
    return 0


async def cmd_memory(rt: Runtime, args: argparse.Namespace) -> int:
    if args.action == "search":
        _print_memory_hits(rt, await rt.memory.asearch(args.text, k=args.k))
    elif args.action == "add":
        item = rt.memory.add(args.kind, args.text, [])
        console.print(f"[green]stored[/green] {item.kind} {item.id}")
    elif args.action == "reindex":
        if not rt.memory.semantic:
            console.print("[yellow]memory.backend is 'keyword' — nothing to index[/yellow]")
            return 0
        try:
            n = await rt.memory.sync_index()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]indexing failed: {exc}[/red]")
            return 1
        console.print(f"[green]indexed {n} new item(s)[/green] · {len(rt.memory)} total · backend {rt.cfg.memory.backend}")
    else:
        st = rt.memory.stats()
        console.print(f"{st['total']} memories · episodic {st['episodic']} · semantic {st['semantic']} · procedural {st['procedural']}")
        console.print(f"[dim]backend {rt.cfg.memory.backend} · embedder {rt.cfg.memory.embed_model}[/dim]")
    return 0


async def cmd_doctor(rt: Runtime, args: argparse.Namespace) -> int:
    ok = True
    table = Table(title="cadence doctor", border_style="grey35")
    for col in ("check", "result", "detail"):
        table.add_column(col)
    for role, cfg in (("big", rt.cfg.big), ("small", rt.cfg.small)):
        probe = OpenAICompatLLM(cfg)
        try:
            models = await probe.list_models()
            loaded = cfg.name in models
            ok &= loaded
            table.add_row(f"{role} model", "[green]ok[/]" if loaded else "[yellow]not loaded[/]",
                          f"{cfg.name} @ {cfg.base_url}" + ("" if loaded else f" — available: {', '.join(models[:6]) or 'none'}"))
        except LLMError as exc:
            ok = False
            table.add_row(f"{role} model", "[red]unreachable[/]", str(exc)[:90])
        finally:
            await probe.aclose()
    if rt.memory.semantic:
        try:
            [vec] = await rt.memory.semantic.embedder.embed(["ping"])
            table.add_row("embeddings", "[green]ok[/]", f"{rt.cfg.memory.embed_model} · {len(vec)} dims")
            pending = await rt.memory.sync_index()
            table.add_row("vector memory", "[green]ok[/]", f"{rt.cfg.memory.backend} · {len(rt.memory)} items"
                          + (f" ({pending} newly indexed)" if pending else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            table.add_row("vector memory", "[red]failed[/]", f"{rt.cfg.memory.backend}: {str(exc)[:80]} (keyword fallback active)")
    else:
        table.add_row("vector memory", "[dim]off[/]", "memory.backend: keyword")
    vision = rt.cfg.vision.name if rt.cfg.vision else rt.cfg.big.name
    table.add_row("vision", "[green]ok[/]", f"view_image via {vision}")
    if rt.sandbox.mode == "none":
        table.add_row("sandbox", "[yellow]off[/]", "bash runs unsandboxed — set sandbox.mode: bwrap|docker")
    else:
        from .sandbox import SandboxError
        try:
            res = await rt.sandbox.run("echo ok", timeout=120)
            good = res.exit == 0 and "ok" in res.output
            ok &= good
            table.add_row("sandbox", "[green]ok[/]" if good else "[red]failed[/]",
                          f"{rt.sandbox.mode}" + ("" if rt.cfg.sandbox.network else " · no network")
                          + ("" if good else f": {res.output.strip()[:80]}"))
        except SandboxError as exc:
            ok = False
            table.add_row("sandbox", "[red]failed[/]", str(exc))
    table.add_row("plugins", "[green]ok[/]" if not rt.plugins.errors else "[red]errors[/]",
                  f"{len(rt.plugins.loaded)} loaded" + (f" · {rt.plugins.errors[0][:70]}" if rt.plugins.errors else ""))
    ok &= not rt.plugins.errors
    table.add_row("hooks", "[green]ok[/]", f"{rt.hooks.count()} registered")
    if rt.cfg.channels:
        from .channels import build_channels
        try:
            names = [c.name for c in build_channels(rt)]
            table.add_row("channels", "[green]ok[/]", ", ".join(names) or "none enabled")
        except ValueError as exc:
            ok = False
            table.add_row("channels", "[red]config error[/]", str(exc)[:90])
    for name, connected, n_tools, err in rt.mcp.status():
        table.add_row(f"mcp:{name}", "[green]ok[/]" if connected else "[red]failed[/]", f"{n_tools} tools" if connected else err[:90])
    table.add_row("skills", "[green]ok[/]", f"{len(rt.skills.skills)} found ({len(rt.skills.errors)} errors)")
    table.add_row("workspace", "[green]ok[/]", str(rt.cfg.workspace))
    console.print(table)
    return 0 if ok else 1


async def cmd_inspect(rt: Runtime, args: argparse.Namespace) -> int:
    if args.what == "skills":
        await _command(rt, rt.new_agent(), "/skills")
    elif args.what == "mcp":
        _print_mcp(rt)
    elif args.what == "plugins":
        _print_plugins(rt)
    elif args.what == "hooks":
        _print_hooks(rt)
    else:
        await _command(rt, rt.new_agent(), "/tools")
    return 0


# ────────────────────────────────────────────────────────────────── helpers
def _print_memory_hits(rt: Runtime, hits) -> None:
    if rt.memory.last_error:
        console.print(f"[yellow]semantic search unavailable ({rt.memory.last_error[:80]}); keyword results:[/yellow]")
    for h in hits:
        console.print(f"[magenta]{h.kind:<10}[/] {h.text}")
    if not hits:
        console.print("[dim]nothing relevant found[/dim]")


def _print_plugins(rt: Runtime) -> None:
    if not rt.plugins.loaded and not rt.plugins.errors:
        console.print("[dim]no plugins in: " + ", ".join(str(d) for d in rt.cfg.plugin_dirs) + "[/dim]")
    for p in rt.plugins.loaded.values():
        console.print(f"[bold cyan]{p.name}[/] [dim]{p.origin}[/dim]" + (f" — tools: {', '.join(p.tools)}" if p.tools else ""))
    for err in rt.plugins.errors:
        console.print(f"[red]plugin error:[/red] {err}")


def _print_hooks(rt: Runtime) -> None:
    if not rt.hooks.count():
        console.print("[dim]no hooks — add `hooks:` to config.yaml or register them from a plugin[/dim]")
    for h in rt.hooks.shell:
        console.print(f"[bold cyan]{h.event}[/] [dim]{h.matcher}[/dim] $ {h.command}")
    for h in rt.hooks.py:
        console.print(f"[bold cyan]{h.event}[/] [dim]{h.matcher}[/dim] plugin:{h.owner} {getattr(h.fn, '__name__', '')}")


def _print_startup(rt: Runtime) -> None:
    bits = [f"{len(rt.scope().registry.all())} tools", f"{len(rt.skills.skills)} skills", f"{len(rt.memory)} memories"]
    if rt.plugins.loaded:
        bits.append(f"{len(rt.plugins.loaded)} plugins")
    if rt.hooks.count():
        bits.append(f"{rt.hooks.count()} hooks")
    bits.append(f"sandbox {rt.sandbox.mode}")
    ok = sum(1 for _, c, _, _ in rt.mcp.status() if c)
    if rt.mcp.connections:
        bits.append(f"MCP {ok}/{len(rt.mcp.connections)} servers")
    console.print("[dim]" + " · ".join(bits) + "[/dim]")
    for name, connected, _n, err in rt.mcp.status():
        if not connected:
            console.print(f"[yellow]⚠ MCP '{name}' unavailable: {err[:100]}[/yellow]")
    for err in rt.plugins.errors:
        console.print(f"[yellow]⚠ plugin {err[:120]}[/yellow]")


def _print_mcp(rt: Runtime) -> None:
    if not rt.mcp.connections:
        console.print("[dim]no MCP servers configured — add them under `mcp_servers:` in ~/.cadence/config.yaml[/dim]")
    for name, connected, n, err in rt.mcp.status():
        console.print(f"[{'green' if connected else 'red'}]{'●' if connected else '○'}[/] [bold]{name}[/] " + (f"{n} tools" if connected else err))


def _prompt_session(cfg: Config):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return PromptSession(history=FileHistory(str(cfg.state_dir / "history")), auto_suggest=AutoSuggestFromHistory())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cadence", description="Cadence: async, autonomous, local-first coder agent.")
    p.add_argument("--version", action="version", version=f"cadence {__version__}")
    p.add_argument("-w", "--workspace", type=Path, default=None, help="workspace root (default: cwd)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("chat", help="interactive session (default)")
    c.add_argument("--resume", metavar="ID")
    r = sub.add_parser("run", help="run one task now with the reflection loop")
    r.add_argument("goal", metavar="task")
    r.add_argument("--verify", help="shell command that must exit 0 when the task is done")
    r.add_argument("--accept", action="append", help="acceptance criterion (repeatable)")
    r.add_argument("--image", action="append", type=Path, help="image to include with the task (repeatable)")
    d = sub.add_parser("daemon", help="heartbeat + event-driven autonomous mode (+ Telegram/WhatsApp channels)")
    d.add_argument("--no-channels", action="store_true", help="do not start configured messaging channels")
    w = sub.add_parser("web", help="web GUI: projects, chat, canvas, browser, files, goals, settings (+ daemon, channels)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--token", help="require this token (auto-generated when binding beyond localhost)")
    w.add_argument("--no-daemon", action="store_true", help="do not run the autonomous daemon")
    w.add_argument("--no-channels", action="store_true", help="do not start Telegram/WhatsApp channels")
    w.add_argument("--open", action="store_true", help="open the browser")
    m = sub.add_parser("memory", help="inspect, search or reindex long-term memory")
    m.add_argument("action", choices=["stats", "search", "add", "reindex"], nargs="?", default="stats")
    m.add_argument("text", nargs="?", default="")
    m.add_argument("-k", type=int, default=8)
    m.add_argument("--kind", choices=["episodic", "semantic", "procedural"], default="semantic")
    g = sub.add_parser("goals", help="long-horizon goals the planner works toward")
    g.add_argument("action", choices=["list", "add", "show", "pause", "resume", "archive"], nargs="?", default="list")
    g.add_argument("text", nargs="?", default="")
    g.add_argument("--success", action="append", help="success criterion (repeatable)")
    t = sub.add_parser("tasks", help="concrete tasks (run by the daemon)")
    t.add_argument("action", choices=["list", "add", "approve", "show"], nargs="?", default="list")
    t.add_argument("text", nargs="?", default="")
    t.add_argument("--verify")
    t.add_argument("--accept", action="append")
    ide = sub.add_parser("ideas", help="the idea inbox")
    ide.add_argument("action", choices=["list", "add", "promote", "dismiss"], nargs="?", default="list")
    ide.add_argument("text", nargs="?", default="")
    sub.add_parser("doctor", help="check model endpoints, MCP servers, skills")
    i = sub.add_parser("inspect", help="list skills, MCP servers or tools")
    i.add_argument("what", choices=["skills", "mcp", "tools", "plugins", "hooks"])
    return p


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.workspace)
    rt = await Runtime(cfg).start()
    try:
        cmd = args.command or "chat"
        handlers = {"run": cmd_run, "daemon": cmd_daemon, "goals": cmd_goals, "tasks": cmd_tasks, "ideas": cmd_ideas,
                    "doctor": cmd_doctor, "inspect": cmd_inspect, "memory": cmd_memory, "web": cmd_web}
        if cmd == "chat":
            return await chat(rt, getattr(args, "resume", None))
        return await handlers[cmd](rt, args)
    finally:
        await rt.close()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(_amain(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
