"""Skills as folders: SKILL.md + references/ scripts/ assets/ — installing, the agent's tools, the API."""

from __future__ import annotations

import io
import os
import shutil
import zipfile

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from cadence.config import SandboxConfig
from cadence.memory import MemoryStore
from cadence.runtime import Runtime
from cadence.sandbox import Sandbox
from cadence.skills import SkillError, SkillRegistry, github_zip_url, read_zip, skill_tools, subtree, unpack
from cadence.tools import Workspace
from cadence.web.server import build_app

ORIGIN = {"Origin": "http://127.0.0.1"}
SKILL_MD = """---
name: {name}
description: Fill PDF forms from data. Use when asked to complete a PDF form.
license: MIT
---

# PDF forms

1. Read references/fields.md for the field names.
2. Run scripts/fill.py <out.txt>.
3. Start from assets/template.txt.
"""
FILL_PY = """import os, sys
fields = open(os.path.join(os.environ["SKILL_DIR"], "references", "fields.md")).read().split()
open(sys.argv[1], "w").write("filled: " + ",".join(fields))
print("ok", len(fields))
"""


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def pdf_forms_zip(prefix: str = "pdf-forms/") -> bytes:
    return make_zip({
        f"{prefix}SKILL.md": SKILL_MD.format(name="pdf-forms"),
        f"{prefix}references/fields.md": "name date signature",
        f"{prefix}scripts/fill.py": FILL_PY,
        f"{prefix}assets/template.txt": "TEMPLATE",
        "__MACOSX/pdf-forms/._SKILL.md": "junk",
        "../../evil.txt": "zip slip",
    })


# ─────────────────────────────────────────────────────────────── installing
def test_unpack_installs_folder_skill_with_resources(tmp_path):
    names = unpack(read_zip(pdf_forms_zip()), tmp_path)
    assert names == ["pdf-forms"]
    root = tmp_path / "pdf-forms"
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        "SKILL.md", "assets/template.txt", "references/fields.md", "scripts/fill.py"]
    assert os.access(root / "scripts" / "fill.py", os.X_OK)
    assert not (tmp_path.parent / "evil.txt").exists() and not any("MACOSX" in str(p) for p in tmp_path.rglob("*"))
    reg = SkillRegistry([tmp_path])
    reg.scan()
    s = reg.get("pdf-forms")
    assert s.grouped()["references"] == ["references/fields.md"] and s.meta["license"] == "MIT"
    text = s.render()
    assert "### references/ (read only the ones you need, with read_skill_file)" in text
    assert "- scripts/fill.py" in text and "use_skill_asset" in text


def test_unpack_top_level_skill_md_multiple_skills_and_conflicts(tmp_path):
    flat = make_zip({"SKILL.md": SKILL_MD.format(name="flat-skill"), "references/a.md": "a"})
    assert unpack(read_zip(flat), tmp_path) == ["flat-skill"]
    assert (tmp_path / "flat-skill" / "references" / "a.md").read_text() == "a"
    two = make_zip({"skills/one/SKILL.md": SKILL_MD.format(name="one"), "skills/two/SKILL.md": SKILL_MD.format(name="two"),
                    "skills/two/scripts/x.sh": "echo hi"})
    assert unpack(read_zip(two), tmp_path) == ["one", "two"]
    with pytest.raises(SkillError, match="already exists"):
        unpack(read_zip(flat), tmp_path)
    assert unpack(read_zip(flat), tmp_path, overwrite=True) == ["flat-skill"]
    with pytest.raises(SkillError, match="no SKILL.md"):
        unpack(read_zip(make_zip({"README.md": "x"})), tmp_path)
    with pytest.raises(SkillError, match="invalid skill name .Not Valid."):
        unpack(read_zip(make_zip({"wrong/SKILL.md": SKILL_MD.format(name="Not Valid")})), tmp_path)


def test_github_urls_map_to_archives():
    assert github_zip_url("https://github.com/anthropics/skills/tree/main/document-skills/pdf") == (
        "https://codeload.github.com/anthropics/skills/zip/main", "document-skills/pdf")
    assert github_zip_url("https://github.com/o/r") == ("https://codeload.github.com/o/r/zip/HEAD", "")
    files = read_zip(make_zip({"skills-main/document-skills/pdf/SKILL.md": "x", "skills-main/other/SKILL.md": "y"}))
    assert list(subtree(files, "document-skills/pdf")) == [__import__("pathlib").Path("pdf/SKILL.md")]


# ────────────────────────────────────────────────────────────── agent tools
def _tools(skills_dir, ws, mode="none"):
    reg = SkillRegistry([skills_dir])
    reg.scan()
    return {t.name: t.handler for t in skill_tools(reg, Workspace(ws), Sandbox(SandboxConfig(mode=mode), ws), timeout=30, output_limit=8000)}


async def test_agent_reads_copies_and_runs_skill_resources(tmp_path):
    skills, ws = tmp_path / "skills", tmp_path / "project"
    ws.mkdir()
    unpack(read_zip(pdf_forms_zip()), skills)
    t = _tools(skills, ws)
    assert await t["read_skill_file"]({"skill": "pdf-forms", "path": "references/fields.md"}) == "name date signature"
    assert "fields.md" in await t["read_skill_file"]({"skill": "pdf-forms", "path": "references"})
    from cadence.tools import ToolError
    with pytest.raises(ToolError, match="outside the skill"):
        await t["read_skill_file"]({"skill": "pdf-forms", "path": "../../etc/passwd"})
    with pytest.raises(ToolError, match="unknown skill"):
        await t["read_skill_file"]({"skill": "nope", "path": "SKILL.md"})
    assert "copied" in await t["use_skill_asset"]({"skill": "pdf-forms", "path": "assets/template.txt", "dest": "out/t.txt"})
    assert (ws / "out" / "t.txt").read_text() == "TEMPLATE"
    res = await t["run_skill_script"]({"skill": "pdf-forms", "script": "scripts/fill.py", "args": ["result.txt"]})
    assert res.startswith("exit 0") and "ok 3" in res
    assert (ws / "result.txt").read_text() == "filled: name,date,signature"  # output lands in the project
    bare = await t["run_skill_script"]({"skill": "pdf-forms", "script": "fill.py", "args": ["r2.txt"]})  # folder omitted
    assert bare.startswith("exit 0") and (ws / "r2.txt").exists()


@pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")
async def test_scripts_see_their_skill_inside_bwrap(tmp_path):
    # bwrap mounts an empty tmpfs over /tmp (and $HOME): without the per-call bind the skill folder is invisible
    skills, ws = tmp_path / "skills", tmp_path / "project"
    ws.mkdir()
    unpack(read_zip(pdf_forms_zip()), skills)
    res = await _tools(skills, ws, mode="bwrap")["run_skill_script"]({"skill": "pdf-forms", "script": "scripts/fill.py", "args": ["r.txt"]})
    assert res.startswith("exit 0"), res
    assert (ws / "r.txt").read_text() == "filled: name,date,signature"
    sbx = Sandbox(SandboxConfig(mode="bwrap"), ws)
    denied = await sbx.run(f"touch {skills}/pdf-forms/x", 10, ro=[str(skills / "pdf-forms")])
    assert denied.exit != 0  # read-only: scripts cannot modify the skill


# ───────────────────────────────────────────────────────────────────── API
async def test_skill_upload_edit_and_export_over_http(cfg, make_router):
    rt = await Runtime(cfg, router=make_router(), memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    client = TestClient(TestServer(build_app(rt)))
    await client.start_server()
    try:
        fd = FormData()
        fd.add_field("file", pdf_forms_zip(), filename="pdf-forms.skill")
        res = await client.post("/api/skills-upload", data=fd, headers=ORIGIN)
        assert (await res.json())["installed"] == ["pdf-forms"]
        assert rt.scope().registry.get("run_skill_script") and "pdf-forms" in rt.scope().skills.skills
        row = next(s for s in (await (await client.get("/api/skills")).json())["skills"] if s["name"] == "pdf-forms")
        assert row["counts"] == {"references": 1, "scripts": 1, "assets": 1, "other": 0}
        again = FormData()
        again.add_field("file", pdf_forms_zip(), filename="pdf-forms.zip")
        assert (await client.post("/api/skills-upload", data=again, headers=ORIGIN)).status == 409

        files = await (await client.get("/api/skills/pdf-forms/files")).json()
        assert {"path": "scripts/fill.py", "dir": False, "size": len(FILL_PY), "exec": True} in files
        await client.put("/api/skills/pdf-forms/file", json={"path": "references/api.md", "content": "# API"}, headers=ORIGIN)
        assert (await (await client.get("/api/skills/pdf-forms/file?path=references/api.md")).json())["content"] == "# API"
        assert (await client.put("/api/skills/pdf-forms/file", json={"path": "../x.md", "content": ""}, headers=ORIGIN)).status == 403
        bad = await client.put("/api/skills/pdf-forms/file", json={"path": "SKILL.md", "content": "no frontmatter"}, headers=ORIGIN)
        assert bad.status == 400 and "PDF forms" in (cfg.home / "skills/pdf-forms/SKILL.md").read_text()

        up = FormData()
        up.add_field("file", b"\x89PNG logo", filename="logo.png")
        assert (await (await client.post("/api/skills/pdf-forms/upload?dir=assets", data=up, headers=ORIGIN)).json())["saved"] == ["assets/logo.png"]
        assert (await (await client.get("/api/skills/pdf-forms/file?path=assets/logo.png")).json())["binary"] is True
        await client.delete("/api/skills/pdf-forms/file?path=assets/template.txt", headers=ORIGIN)
        assert (await client.delete("/api/skills/pdf-forms/file?path=SKILL.md", headers=ORIGIN)).status == 400

        exported = await (await client.get("/api/skills/pdf-forms/export")).read()
        names = sorted(zipfile.ZipFile(io.BytesIO(exported)).namelist())
        assert names == ["pdf-forms/SKILL.md", "pdf-forms/assets/logo.png", "pdf-forms/references/api.md",
                         "pdf-forms/references/fields.md", "pdf-forms/scripts/fill.py"]

        folder = cfg.workspace / "incoming" / "notes-skill"
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text(SKILL_MD.format(name="notes-skill"))
        (folder / "references" / "r.md").write_text("r")
        imp = await (await client.post("/api/skills-import", json={"source": str(folder), "scope": "project"}, headers=ORIGIN)).json()
        assert imp["installed"] == ["notes-skill"] and (cfg.workspace / ".cadence/skills/notes-skill/references/r.md").exists()

        created = await client.post("/api/skills", json={"name": "my-skill", "description": "does things"}, headers=ORIGIN)
        assert created.status == 200 and all((cfg.home / "skills/my-skill" / d).is_dir() for d in ("references", "scripts", "assets"))
        assert (await client.post("/api/skills", json={"name": "Bad_Name", "description": "x"}, headers=ORIGIN)).status == 400
    finally:
        await client.close()
        await rt.close()
