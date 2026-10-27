"""Routes for the file explorer, the canvas and the shared browser.

Every path is resolved inside the project's directory (the same confinement the agent's tools use).
Raw files are served with `Content-Security-Policy: sandbox`, so an HTML file the agent wrote runs in
an opaque origin and cannot call this API with the user's session.
"""

from __future__ import annotations

import mimetypes
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..canvas import FORMATS, CanvasDoc
from ..tools import SKIP_DIRS, ToolError, Workspace

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .server import Hub

MAX_TEXT = 2 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024
SANDBOX_CSP = "sandbox allow-scripts; default-src 'self' data: blob: 'unsafe-inline'"


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start > len(chunk) - 4  # a multi-byte char cut at the buffer edge is still text
    return True


def routes(r: web.RouteTableDef, rt: "Runtime", hub: "Hub") -> None:
    from .server import body

    def ws_of(project: str) -> tuple[Workspace, str]:
        p = rt.project(project)
        if not p.root.is_dir():
            raise web.HTTPNotFound(text=f"project folder {p.path} is missing")
        return Workspace(p.root), p.id

    def resolve(ws: Workspace, rel: str) -> Path:
        try:
            return ws.resolve(rel or ".")
        except ToolError as exc:
            raise web.HTTPForbidden(text=str(exc))

    def entry(ws: Workspace, p: Path) -> dict[str, Any]:
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        return {"name": p.name, "path": ws.rel(p) if p != ws.root else "", "dir": p.is_dir(), "size": size, "mtime": mtime,
                "hidden": p.name.startswith("."), "heavy": p.name in SKIP_DIRS}

    async def touched(pid: str, rel: str) -> None:
        await hub.broadcast({"kind": "file_changed", "project": pid, "path": rel, "by": "user"})

    # ───────────────────────────────────────────────────────────── files
    @r.get("/api/files")
    async def list_dir(req: web.Request) -> web.Response:
        ws, pid = ws_of(req.query.get("project", ""))
        d = resolve(ws, req.query.get("path", ""))
        if not d.is_dir():
            raise web.HTTPBadRequest(text="not a directory")
        try:
            children = sorted(d.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except PermissionError:
            raise web.HTTPForbidden(text="permission denied")
        return web.json_response({"project": pid, "root": str(ws.root), "path": ws.rel(d) if d != ws.root else "",
                                  "entries": [entry(ws, c) for c in children[:2000]], "truncated": len(children) > 2000})

    @r.get("/api/files/search")
    async def search(req: web.Request) -> web.Response:
        ws, _pid = ws_of(req.query.get("project", ""))
        q = req.query.get("q", "").strip().lower()
        if not q:
            return web.json_response([])
        hits = []
        for p in ws.root.rglob("*"):
            if any(part in SKIP_DIRS for part in p.relative_to(ws.root).parts):
                continue
            if q in p.name.lower():
                hits.append(entry(ws, p))
                if len(hits) >= 200:
                    break
        return web.json_response(hits)

    @r.get("/api/files/read")
    async def read(req: web.Request) -> web.Response:
        ws, _pid = ws_of(req.query.get("project", ""))
        f = resolve(ws, req.query.get("path", ""))
        if not f.is_file():
            raise web.HTTPNotFound(text="no such file")
        size = f.stat().st_size
        mime = mimetypes.guess_type(f.name)[0] or ""
        if size > MAX_TEXT or not _is_text(f):
            return web.json_response({"path": ws.rel(f), "binary": True, "size": size, "mime": mime})
        return web.json_response({"path": ws.rel(f), "binary": False, "size": size, "mime": mime,
                                  "mtime": f.stat().st_mtime, "content": f.read_text(encoding="utf-8", errors="replace")})

    @r.get("/api/files/raw")
    async def raw(req: web.Request) -> web.StreamResponse:
        ws, _pid = ws_of(req.query.get("project", ""))
        f = resolve(ws, req.query.get("path", ""))
        if not f.is_file():
            raise web.HTTPNotFound(text="no such file")
        headers = {"Content-Security-Policy": SANDBOX_CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
        if req.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{f.name}"'
        return web.FileResponse(f, headers=headers)

    @r.put("/api/files/write")
    async def write(req: web.Request) -> web.Response:
        data = await body(req)
        ws, pid = ws_of(str(data.get("project", "")))
        f = resolve(ws, str(data.get("path", "")))
        if f.is_dir():
            raise web.HTTPBadRequest(text="that is a directory")
        expected = data.get("mtime")
        if expected and f.is_file() and abs(f.stat().st_mtime - float(expected)) > 1e-3 and not data.get("force"):
            raise web.HTTPConflict(text="the file changed on disk since you opened it (the agent may have edited it)")
        content = str(data.get("content", ""))
        if len(content.encode()) > MAX_TEXT:
            raise web.HTTPBadRequest(text="file too large for the editor")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        await touched(pid, ws.rel(f))
        return web.json_response(entry(ws, f))

    @r.post("/api/files/mkdir")
    async def mkdir(req: web.Request) -> web.Response:
        data = await body(req)
        ws, pid = ws_of(str(data.get("project", "")))
        d = resolve(ws, str(data.get("path", "")))
        if d.exists():
            raise web.HTTPConflict(text="already exists")
        d.mkdir(parents=True)
        await touched(pid, ws.rel(d))
        return web.json_response(entry(ws, d))

    @r.post("/api/files/rename")
    async def rename(req: web.Request) -> web.Response:
        data = await body(req)
        ws, pid = ws_of(str(data.get("project", "")))
        src, dst = resolve(ws, str(data.get("from", ""))), resolve(ws, str(data.get("to", "")))
        if src == ws.root:
            raise web.HTTPForbidden(text="cannot move the project root")
        if not src.exists():
            raise web.HTTPNotFound(text="no such file")
        if dst.exists():
            raise web.HTTPConflict(text=f"{ws.rel(dst)} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        await touched(pid, ws.rel(dst))
        return web.json_response(entry(ws, dst))

    @r.delete("/api/files")
    async def delete(req: web.Request) -> web.Response:
        ws, pid = ws_of(req.query.get("project", ""))
        p = resolve(ws, req.query.get("path", ""))
        if p == ws.root:
            raise web.HTTPForbidden(text="cannot delete the project root")
        if not p.exists() and not p.is_symlink():
            raise web.HTTPNotFound(text="no such file")
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        await touched(pid, ws.rel(p))
        return web.json_response({"ok": True})

    @r.post("/api/files/upload")
    async def upload(req: web.Request) -> web.Response:
        ws, pid = ws_of(req.query.get("project", ""))
        d = resolve(ws, req.query.get("dir", ""))
        if not d.is_dir():
            raise web.HTTPBadRequest(text="target is not a directory")
        reader = await req.multipart()
        saved = []
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            dest = resolve(ws, ws.rel(d / Path(part.filename).name))
            size = 0
            with dest.open("wb") as fh:
                while chunk := await part.read_chunk():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=size)
                    fh.write(chunk)
            saved.append(entry(ws, dest))
            await touched(pid, ws.rel(dest))
        return web.json_response(saved)

    # ──────────────────────────────────────────────────────────── canvas
    def doc_or_404(cid: str) -> CanvasDoc:
        doc = rt.canvas.get(cid)
        if not doc:
            raise web.HTTPNotFound(text="no such document")
        return doc

    async def announce(doc: CanvasDoc, focus: bool = False) -> None:
        await hub.broadcast({"kind": "canvas", "project": doc.project, "doc": doc.summary(), "focus": focus})

    @r.get("/api/canvas")
    async def canvas_list(req: web.Request) -> web.Response:
        pid = rt.project(req.query.get("project", "")).id
        return web.json_response([d.summary() for d in rt.canvas.list(pid)])

    @r.get("/api/canvas/{cid}")
    async def canvas_get(req: web.Request) -> web.Response:
        return web.json_response(asdict(doc_or_404(req.match_info["cid"])))

    @r.post("/api/canvas")
    async def canvas_new(req: web.Request) -> web.Response:
        data = await body(req)
        fmt = str(data.get("format", "markdown"))
        if fmt not in FORMATS:
            raise web.HTTPBadRequest(text=f"format must be one of {', '.join(FORMATS)}")
        doc = rt.canvas.add(CanvasDoc(title=str(data.get("title", "") or "Untitled")[:120], content=str(data.get("content", "")),
                                      format=fmt, language=str(data.get("language", "")), author="user",
                                      project=rt.project(str(data.get("project", ""))).id))
        await announce(doc)
        return web.json_response(asdict(doc))

    @r.put("/api/canvas/{cid}")
    async def canvas_save(req: web.Request) -> web.Response:
        doc = doc_or_404(req.match_info["cid"])
        data = await body(req)
        changes = {k: str(data[k]) for k in ("title", "language") if k in data}
        if "format" in data:
            if data["format"] not in FORMATS:
                raise web.HTTPBadRequest(text="bad format")
            changes["format"] = str(data["format"])
        if data.get("base_updated") and abs(float(data["base_updated"]) - doc.updated) > 1e-3 and doc.author == "agent":
            raise web.HTTPConflict(text="the agent revised this document while you were editing; reload to see its version")
        rt.canvas.save(doc, str(data.get("content", doc.content)), "user", **changes)
        await announce(doc)
        return web.json_response(asdict(doc))

    @r.post("/api/canvas/{cid}/restore")
    async def canvas_restore(req: web.Request) -> web.Response:
        doc = doc_or_404(req.match_info["cid"])
        idx = int((await body(req)).get("index", -1))
        try:
            old = doc.versions[idx]
        except IndexError:
            raise web.HTTPBadRequest(text="no such version")
        rt.canvas.save(doc, old["content"], "user")
        await announce(doc)
        return web.json_response(asdict(doc))

    @r.delete("/api/canvas/{cid}")
    async def canvas_delete(req: web.Request) -> web.Response:
        doc = doc_or_404(req.match_info["cid"])
        rt.canvas.delete(doc.id)
        await hub.broadcast({"kind": "canvas_deleted", "project": doc.project, "id": doc.id})
        return web.json_response({"ok": True})

    @r.post("/api/canvas/{cid}/export")
    async def canvas_export(req: web.Request) -> web.Response:
        doc = doc_or_404(req.match_info["cid"])
        data = await body(req)
        ws, pid = ws_of(doc.project)
        f = resolve(ws, str(data.get("path", "")))
        if f.exists() and not data.get("overwrite"):
            raise web.HTTPConflict(text=f"{ws.rel(f)} exists")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(doc.content, encoding="utf-8")
        await touched(pid, ws.rel(f))
        return web.json_response(entry(ws, f))

    # ─────────────────────────────────────────────────────────── browser
    @r.get("/api/browser")
    async def browser_state(req: web.Request) -> web.Response:
        s = rt.browser.get(rt.project(req.query.get("project", "")).id)
        return web.json_response(s.state())
