from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from conftest import tool_call

from cadence.llm import Completion
from cadence.memory import MemoryStore
from cadence.runtime import Runtime
from cadence.web.server import build_app

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
        assert ok.status == 200 and "cadence_token" in ok.cookies
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


async def test_any_file_can_be_attached_and_is_read_into_the_message(cfg, make_router):
    import aiohttp
    router = make_router(big_script=[Completion(content="Ada is 36")])
    rt, client = await _client(cfg, router)
    try:
        fd = aiohttp.FormData()
        fd.add_field("file", b"\x89PNG\r\n", filename="shot.png", content_type="image/png")
        img = await (await client.post("/api/upload", data=fd, headers=ORIGIN)).json()
        assert img["kind"] == "image" and img["id"].endswith("-shot.png")
        fd = aiohttp.FormData()
        fd.add_field("file", b"name,age\nAda,36\n", filename="people.csv", content_type="text/csv")
        doc = await (await client.post("/api/upload", data=fd, headers=ORIGIN)).json()
        assert doc["kind"] == "file" and doc["name"] == "people.csv"
        sid = (await (await client.post("/api/sessions", headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "how old is Ada?", "files": [doc["id"]]})
        await _until_finished(ws, sid)
        sent = router.big.calls[0][-1]["content"]
        assert "how old is Ada?" in sent and "### people.csv (table) — saved as .cadence/attachments/people.csv" in sent
        assert "| Ada | 36 |" in sent
        assert (cfg.workspace / ".cadence/attachments/people.csv").read_text().startswith("name,age")
        item = (await (await client.get(f"/api/sessions/{sid}")).json())["items"][0]
        assert item["text"] == "how old is Ada?" and item["files"] == [
            {"name": "people.csv", "kind": "table", "path": ".cadence/attachments/people.csv"}]
        prev = await (await client.get("/api/files/preview?path=.cadence/attachments/people.csv")).json()
        assert prev["tables"][0]["columns"] == ["name", "age"]
        await ws.close()
    finally:
        await client.close()
        await rt.close()


async def test_projects_can_be_archived(cfg, make_router, tmp_path):
    other = tmp_path / "old"
    other.mkdir()
    rt, client = await _client(cfg, make_router())
    try:
        p = await (await client.post("/api/projects", json={"path": str(other), "name": "Old"}, headers=ORIGIN)).json()
        assert (await client.patch(f"/api/projects/{rt.default_project.id}", json={"archived": True}, headers=ORIGIN)).status == 409
        a = await (await client.patch(f"/api/projects/{p['id']}", json={"archived": True}, headers=ORIGIN)).json()
        assert a["archived"] is True and rt.projects.get(p["id"]).archived
        from cadence.tasks import Task
        rt.tasks.add(Task(title="dormant", project=p["id"]))
        rt.make_daemon()
        rt.daemon._launch_ready()
        assert rt.daemon.running == []  # the daemon leaves archived projects alone
        b = await (await client.patch(f"/api/projects/{p['id']}", json={"archived": False}, headers=ORIGIN)).json()
        assert b["archived"] is False
    finally:
        await client.close()
        await rt.close()
