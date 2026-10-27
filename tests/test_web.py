from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from conftest import tool_call

from kestrel.llm import Completion
from kestrel.memory import MemoryStore
from kestrel.runtime import Runtime
from kestrel.web.server import build_app

ORIGIN = {"Origin": "http://127.0.0.1"}


async def _client(cfg, router, token=None):
    rt = await Runtime(cfg, router=router, memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    client = TestClient(TestServer(build_app(rt, token=token)))
    await client.start_server()
    return rt, client


async def _until_finished(ws, sid):
    events = []
    async with asyncio.timeout(10):
        async for msg in ws:
            m = msg.json()
            if m.get("session") != sid:
                continue
            events.append(m)
            if m["kind"] == "turn" and m["state"] == "finished":
                return events
    return events


async def test_chat_over_websocket_streams_events_and_persists_transcript(cfg, make_router):
    router = make_router(big_script=[tool_call("write_file", path="a.txt", content="hi"), Completion(content="wrote a.txt")])
    rt, client = await _client(cfg, router)
    try:
        sid = (await (await client.post("/api/sessions", headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "make a.txt"})
        events = await _until_finished(ws, sid)
        types = [m["event"]["type"] for m in events if m["kind"] == "agent"]
        assert "tool_call" in types and "tool_result" in types and types[-1] == "message_done"
        assert (cfg.workspace / "a.txt").read_text() == "hi"
        detail = await (await client.get(f"/api/sessions/{sid}")).json()
        assert [i["role"] for i in detail["items"]] == ["user", "tool", "assistant"]
        assert detail["items"][1]["result"].startswith("wrote")
        listed = await (await client.get("/api/sessions")).json()
        assert listed[0]["id"] == sid and listed[0]["title"] == "make a.txt"
        await ws.close()
    finally:
        await client.close()
        await rt.close()


async def test_steering_a_running_turn_reaches_the_model(cfg, make_router):
    router = make_router(big_script=[tool_call("bash", command="sleep 0.5"), Completion(content="done")])
    rt, client = await _client(cfg, router)
    try:
        sid = (await (await client.post("/api/sessions", headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "go"})
        await asyncio.sleep(0.2)  # the bash call is running
        await ws.send_json({"op": "send", "session": sid, "text": "use python instead"})  # send while busy = steer
        events = await _until_finished(ws, sid)
        assert any(m["kind"] == "steer" and m["text"] == "use python instead" for m in events)
        assert any("use python instead" in str(m.get("content")) for m in router.big.calls[-1])
        detail = await (await client.get(f"/api/sessions/{sid}")).json()
        assert {"role": "steer", "text": "use python instead"} in detail["items"]
        await ws.close()
    finally:
        await client.close()
        await rt.close()


async def test_rebinding_and_cross_origin_requests_are_refused(cfg, make_router):
    rt, client = await _client(cfg, make_router())
    try:
        assert (await client.get("/api/status", headers={"Host": "attacker.example"})).status == 403
        assert (await client.post("/api/sessions", headers={"Origin": "https://attacker.example"})).status == 403
        assert (await client.post("/api/sessions", headers=ORIGIN)).status == 200
    finally:
        await client.close()
        await rt.close()


async def test_token_is_required_when_configured(cfg, make_router):
    rt, client = await _client(cfg, make_router(), token="s3cret")
    try:
        assert (await client.get("/api/sessions")).status == 401
        ok = await client.get("/api/sessions?token=s3cret")
        assert ok.status == 200 and "kestrel_token" in ok.cookies
        assert (await client.get("/api/sessions")).status == 200  # cookie now set
    finally:
        await client.close()
        await rt.close()


async def test_tasks_and_memory_endpoints(cfg, make_router):
    rt, client = await _client(cfg, make_router())
    try:
        t = await (await client.post("/api/tasks", json={"title": "add docs", "verify_command": "true"}, headers=ORIGIN)).json()
        assert t["status"] == "pending" and t["verify_command"] == "true" and t["project"] == rt.default_project.id
        rt.tasks.update(rt.tasks.get(t["id"]), status="proposed")
        assert (await (await client.post(f"/api/tasks/{t['id']}/approve", headers=ORIGIN)).json())["status"] == "pending"
        assert (await client.post(f"/api/tasks/{t['id']}/retry", headers=ORIGIN)).status == 409
        edited = await (await client.patch(f"/api/tasks/{t['id']}", json={"title": "add API docs"}, headers=ORIGIN)).json()
        assert edited["title"] == "add API docs"
        assert (await client.delete(f"/api/tasks/{t['id']}", headers=ORIGIN)).status == 200 and not rt.tasks.all()
        m = await (await client.post("/api/memory", json={"kind": "semantic", "text": "Deploys use make release"}, headers=ORIGIN)).json()
        found = await (await client.get("/api/memory?q=deploys")).json()
        assert found["items"][0]["text"] == "Deploys use make release"
        assert (await client.delete(f"/api/memory/{m['id']}", headers=ORIGIN)).status == 200 and len(rt.memory) == 0
    finally:
        await client.close()
        await rt.close()


async def test_upload_accepts_images_only(cfg, make_router):
    import aiohttp
    rt, client = await _client(cfg, make_router())
    try:
        fd = aiohttp.FormData()
        fd.add_field("file", b"\x89PNG\r\n", filename="shot.png", content_type="image/png")
        up = await client.post("/api/upload", data=fd, headers=ORIGIN)
        assert up.status == 200 and (await up.json())["id"].endswith(".png")
        bad = aiohttp.FormData()
        bad.add_field("file", b"#!/bin/sh", filename="x.sh", content_type="text/plain")
        assert (await client.post("/api/upload", data=bad, headers=ORIGIN)).status == 400
    finally:
        await client.close()
        await rt.close()
