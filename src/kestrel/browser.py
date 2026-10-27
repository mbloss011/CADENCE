"""A shared browser the agent and the user drive together.

One headless Chromium per project (persistent profile under ~/.kestrel/state/browser/<project>, so
logins survive restarts). The web UI streams it live over CDP screencast and forwards the user's clicks
and typing; the agent uses the `browser_*` tools, which work from a numbered list of the page's
interactive elements so even small models can click reliably. Either side can take over at any time:
the user can log in or solve a captcha, then tell the agent to continue.

Needs `pip install playwright`. It uses Chrome/Chromium already on the system when it can find one,
otherwise run `playwright install chromium` once.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .tools import Tool, ToolError, _schema, truncate

log = logging.getLogger(__name__)
Publish = Callable[[dict[str, Any]], Awaitable[None]]

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-kid]').forEach(e => e.removeAttribute('data-kid'));
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],[role=tab],' +
              '[role=menuitem],[role=checkbox],[role=option],[contenteditable=""],[contenteditable=true],[onclick]';
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight * 1.5) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    i++; el.setAttribute('data-kid', String(i));
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.title ||
                   el.getAttribute('alt') || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({id: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
              label, href: el.tagName === 'A' ? el.getAttribute('href') || '' : ''});
    if (i >= 120) break;
  }
  const text = document.body ? document.body.innerText.replace(/\n{3,}/g, '\n\n') : '';
  return {title: document.title, url: location.href, elements: out, text: text.slice(0, 6000)};
}
"""


class BrowserError(ToolError):
    pass


def find_chrome(explicit: str = "") -> str:
    if explicit:
        return explicit
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        path = shutil.which(name)
        if path:
            return path
    return ""  # fall back to Playwright's own download


class BrowserSession:
    def __init__(self, project: str, profile: Path, settings: dict[str, Any], publish: Publish | None):
        self.project = project
        self.profile = profile
        self.settings = settings
        self.publish = publish
        self.width = int(settings.get("width", 1280))
        self.height = int(settings.get("height", 800))
        self.watchers = 0
        self.driver = ""  # "agent" | "user": who acted last, shown in the UI
        self._pw: Any = None
        self._ctx: Any = None
        self.page: Any = None
        self._cdp: Any = None
        self._lock = asyncio.Lock()
        self._casting = False

    @property
    def started(self) -> bool:
        return self._ctx is not None

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError("the browser needs Playwright: pip install playwright") from exc
        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        exe = find_chrome(str(self.settings.get("executable", "")))
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile), headless=bool(self.settings.get("headless", True)),
                executable_path=exe or None, viewport={"width": self.width, "height": self.height},
                args=["--disable-blink-features=AutomationControlled"])
        except Exception as exc:
            await self._pw.stop()
            self._pw = None
            hint = "" if exe else " (no Chrome found; run `playwright install chromium`)"
            raise BrowserError(f"cannot start the browser{hint}: {exc}") from exc
        self._ctx.on("page", lambda p: asyncio.ensure_future(self._adopt(p)))
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._wire(self.page)
        await self.resume_cast()
        await self._state()

    async def close(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx = self._pw = self.page = self._cdp = None
        self._casting = False
        if ctx:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if pw:
            await pw.stop()
        await self._emit({"kind": "browser_state", "project": self.project, "started": False})

    async def _adopt(self, page: Any) -> None:
        """A popup or target=_blank link: follow it, as a person would."""
        self.page = page
        await self._wire(page)
        await self._restart_cast()
        await self._state()

    async def _wire(self, page: Any) -> None:
        page.on("framenavigated", lambda f: asyncio.ensure_future(self._state()) if f == page.main_frame else None)
        page.on("load", lambda _p: asyncio.ensure_future(self._state()))
        page.on("close", lambda _p: asyncio.ensure_future(self._on_close(page)))

    async def _on_close(self, page: Any) -> None:
        if self._ctx and page is self.page:
            self.page = self._ctx.pages[-1] if self._ctx.pages else await self._ctx.new_page()
            await self._restart_cast()
            await self._state()

    async def _emit(self, msg: dict[str, Any]) -> None:
        if self.publish:
            await self.publish(msg)

    def state(self) -> dict[str, Any]:
        page = self.page
        return {"kind": "browser_state", "project": self.project, "started": self.started,
                "url": page.url if page else "", "width": self.width, "height": self.height, "driver": self.driver}

    async def _state(self) -> None:
        msg = self.state()
        try:
            msg["title"] = await self.page.title() if self.page else ""
        except Exception:  # noqa: BLE001 — mid-navigation
            msg["title"] = ""
        await self._emit(msg)

    # ------------------------------------------------------------ screencast
    async def watch(self, on: bool) -> None:
        self.watchers = max(0, self.watchers + (1 if on else -1))
        if self.watchers and self.started:
            await self._start_cast()
        elif not self.watchers:
            await self._stop_cast()

    async def resume_cast(self) -> None:
        """Start streaming if someone is watching (used when the browser starts after they opened the panel)."""
        if self.watchers:
            await self._start_cast()

    async def _start_cast(self) -> None:
        if self._casting or not self.page:
            return
        self._cdp = await self.page.context.new_cdp_session(self.page)
        cdp = self._cdp

        async def on_frame(ev: dict[str, Any]) -> None:
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            except Exception:  # noqa: BLE001
                return
            meta = ev.get("metadata", {})
            await self._emit({"kind": "browser_frame", "project": self.project, "data": ev["data"],
                              "w": meta.get("deviceWidth", self.width), "h": meta.get("deviceHeight", self.height)})

        cdp.on("Page.screencastFrame", lambda ev: asyncio.ensure_future(on_frame(ev)))
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": int(self.settings.get("quality", 65)),
                                                "maxWidth": self.width, "maxHeight": self.height, "everyNthFrame": 1})
        self._casting = True

    async def _stop_cast(self) -> None:
        if self._cdp and self._casting:
            try:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
            except Exception:  # noqa: BLE001
                pass
        self._cdp, self._casting = None, False

    async def _restart_cast(self) -> None:
        if self.watchers:
            await self._stop_cast()
            await self._start_cast()

    async def frame(self) -> dict[str, Any] | None:
        """One still frame, for a viewer that just connected (screencast only sends on change)."""
        if not self.page:
            return None
        png = await self.page.screenshot(type="jpeg", quality=65)
        return {"kind": "browser_frame", "project": self.project, "data": base64.b64encode(png).decode(),
                "w": self.width, "h": self.height}

    # --------------------------------------------------------------- actions
    async def act(self, who: str, action: str, **a: Any) -> None:
        """Low-level input shared by the user (from the UI) and the agent's tools."""
        await self.start()
        async with self._lock:
            self.driver = who
            p = self.page
            if action == "goto":
                url = str(a.get("url", "")).strip()
                if url and "://" not in url and not url.startswith(("about:", "data:")):
                    url = ("https://" + url) if "." in url.split("/")[0] and " " not in url else \
                        f"https://duckduckgo.com/?q={url}"
                await p.goto(url or "about:blank", wait_until="domcontentloaded", timeout=30000)
            elif action == "back":
                await p.go_back(wait_until="domcontentloaded", timeout=15000)
            elif action == "forward":
                await p.go_forward(wait_until="domcontentloaded", timeout=15000)
            elif action == "reload":
                await p.reload(wait_until="domcontentloaded", timeout=30000)
            elif action == "click":
                await p.mouse.click(float(a["x"]), float(a["y"]))
            elif action == "move":
                await p.mouse.move(float(a["x"]), float(a["y"]))
            elif action == "scroll":
                await p.mouse.wheel(float(a.get("dx", 0)), float(a.get("dy", 0)))
            elif action == "type":
                await p.keyboard.type(str(a.get("text", "")))
            elif action == "key":
                await p.keyboard.press(str(a.get("key", "")))
            else:
                raise BrowserError(f"unknown browser action {action!r}")
        await self._state()

    async def snapshot(self) -> dict[str, Any]:
        await self.start()
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        return await self.page.evaluate(SNAPSHOT_JS)

    async def element(self, kid: int) -> Any:
        loc = self.page.locator(f'[data-kid="{int(kid)}"]')
        if await loc.count() == 0:
            raise BrowserError(f"no element [{kid}] on the page now; call browser_snapshot for fresh numbers")
        return loc.first


class BrowserManager:
    def __init__(self, cfg: Any, publish: Publish | None = None):
        self.cfg = cfg
        self.publish = publish
        self.sessions: dict[str, BrowserSession] = {}

    def get(self, project: str) -> BrowserSession:
        s = self.sessions.get(project)
        if s is None:
            s = self.sessions[project] = BrowserSession(project, self.cfg.state_dir / "browser" / (project or "default"),
                                                        self.cfg.browser, self._publish)
        return s

    async def _publish(self, msg: dict[str, Any]) -> None:
        if self.publish:
            await self.publish(msg)

    async def close(self) -> None:
        for s in list(self.sessions.values()):
            await s.close()
        self.sessions.clear()


def render_snapshot(snap: dict[str, Any], text_limit: int = 2500) -> str:
    lines = [f"Page: {snap.get('title') or '(untitled)'}\nURL: {snap.get('url')}", "", "Interactive elements:"]
    for e in snap.get("elements", []):
        kind = e["tag"] + (f":{e['type']}" if e.get("type") else "")
        extra = f" → {e['href'][:80]}" if e.get("href") else ""
        lines.append(f"[{e['id']}] {kind} \"{e['label']}\"{extra}")
    if not snap.get("elements"):
        lines.append("(none visible: scroll, or the page is still loading)")
    lines += ["", "Visible text:", truncate(snap.get("text", ""), text_limit)]
    return "\n".join(lines)


def browser_tools(manager: BrowserManager, project: str, vision: Any = None) -> list[Tool]:
    def session() -> BrowserSession:
        return manager.get(project)

    async def opened() -> None:
        if manager.publish:
            await manager.publish({"kind": "browser_open", "project": project})

    async def after(action_note: str) -> str:
        await asyncio.sleep(0.6)  # let the page react before reading it back
        return f"{action_note}\n\n{render_snapshot(await session().snapshot())}"

    async def browser_open(args: dict[str, Any]) -> str:
        await opened()
        await session().act("agent", "goto", url=str(args.get("url", "")))
        return await after(f"opened {args.get('url')}")

    async def browser_snapshot(_args: dict[str, Any]) -> str:
        await opened()
        return render_snapshot(await session().snapshot(), 5000)

    async def browser_click(args: dict[str, Any]) -> str:
        s = session()
        await s.start()
        el = await s.element(int(args.get("element", 0)))
        s.driver = "agent"
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=10000)
        return await after(f"clicked [{args.get('element')}]")

    async def browser_type(args: dict[str, Any]) -> str:
        s = session()
        await s.start()
        el = await s.element(int(args.get("element", 0)))
        s.driver = "agent"
        await el.fill(str(args.get("text", "")), timeout=10000)
        if args.get("submit"):
            await el.press("Enter")
        return await after(f"typed into [{args.get('element')}]" + (" and pressed Enter" if args.get("submit") else ""))

    async def browser_scroll(args: dict[str, Any]) -> str:
        dy = {"down": 700, "up": -700}.get(str(args.get("direction", "down")), 700)
        await session().act("agent", "scroll", dy=dy)
        return await after(f"scrolled {args.get('direction', 'down')}")

    async def browser_back(_args: dict[str, Any]) -> str:
        await session().act("agent", "back")
        return await after("went back")

    async def browser_key(args: dict[str, Any]) -> str:
        await session().act("agent", "key", key=str(args.get("key", "Enter")))
        return await after(f"pressed {args.get('key')}")

    async def browser_look(args: dict[str, Any]) -> str:
        s = session()
        await s.start()
        png = await s.page.screenshot(type="png")
        question = str(args.get("question", "") or "Describe this web page: layout, key content, and anything that looks wrong.")
        comp = await vision.complete([{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}],
            max_tokens=700, temperature=0.1)
        return comp.content.strip() or "(the vision model returned nothing)"

    num = {"type": "integer", "description": "element number from the latest snapshot"}
    tools = [
        Tool("browser_open", "Open a URL (or search terms) in the shared browser the user can watch and use with you. "
             "Returns the page's interactive elements as [n] numbers plus visible text.",
             _schema({"url": {"type": "string"}}, ["url"]), browser_open, source="browser"),
        Tool("browser_snapshot", "Re-read the current browser page: numbered interactive elements and visible text. "
             "Use after the user says they did something in the browser.", _schema({}, []), browser_snapshot,
             source="browser", read_only=True),
        Tool("browser_click", "Click element [n] from the latest snapshot.", _schema({"element": num}, ["element"]),
             browser_click, source="browser"),
        Tool("browser_type", "Fill text into input element [n]; submit=true presses Enter.",
             _schema({"element": num, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["element", "text"]),
             browser_type, source="browser"),
        Tool("browser_scroll", "Scroll the page.", _schema({"direction": {"type": "string", "enum": ["down", "up"]}}, []),
             browser_scroll, source="browser"),
        Tool("browser_back", "Go back one page.", _schema({}, []), browser_back, source="browser"),
        Tool("browser_key", "Press a key (Enter, Escape, Tab, ArrowDown, …).", _schema({"key": {"type": "string"}}, ["key"]),
             browser_key, source="browser"),
    ]
    if vision is not None:
        tools.append(Tool("browser_look", "Look at the browser page with the vision model (layout, images, visual bugs).",
                          _schema({"question": {"type": "string"}}, []), browser_look, source="browser", read_only=True))
    return tools
