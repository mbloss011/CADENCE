"""Kestrel → Cadence one-time migration."""

from __future__ import annotations

import json
import os

from cadence.migrate import legacy_env, migrate_home, migrate_project


def test_home_moves_once_with_symlink_and_rewrites(tmp_path):
    legacy, home = tmp_path / ".kestrel", tmp_path / ".cadence"
    (legacy / "plugins").mkdir(parents=True)
    (legacy / "state" / "sessions").mkdir(parents=True)
    (legacy / "config.yaml").write_text("memory: {backend: qdrant}\nplugins: {dirs: [~/.kestrel/plugins]}\n")
    (legacy / "plugins" / "guard.py").write_text("from kestrel.plugins import HookResult\nimport kestrel\n")
    (legacy / "state" / "sessions" / "s1.json").write_text(json.dumps({"messages": [{"content": "saved as .kestrel/attachments/a.csv"}]}))
    (legacy / "state" / "memory.jsonl").write_text('{"id": "m1"}\n')

    done = migrate_home(home, legacy)
    assert done and home.is_dir() and legacy.is_symlink() and legacy.resolve() == home.resolve()
    assert (home / "state" / "memory.jsonl").read_text() == '{"id": "m1"}\n'  # data moved intact
    assert "~/.cadence/plugins" in (home / "config.yaml").read_text()
    plugin = (home / "plugins" / "guard.py").read_text()
    assert plugin == "from cadence.plugins import HookResult\nimport cadence\n"
    assert (home / "plugins" / "guard.py.kestrel-bak").read_text().startswith("from kestrel")
    assert ".cadence/attachments/a.csv" in (home / "state" / "sessions" / "s1.json").read_text()
    assert json.loads((home / "settings.json").read_text())["memory"]["collection"] == "kestrel_memory"
    assert (home / "state" / "migrated-from-kestrel.json").is_file()
    assert migrate_home(home, legacy) == []  # idempotent


def test_cache_only_home_is_merged_into(tmp_path):
    legacy, home = tmp_path / ".kestrel", tmp_path / ".cadence"
    (legacy / "state").mkdir(parents=True)
    (legacy / "state" / "projects.json").write_text("[]")
    (home / "state" / "cache" / "docs").mkdir(parents=True)  # e.g. a page render made before the first start
    (home / "state" / "cache" / "docs" / "x.png").write_bytes(b"png")
    assert migrate_home(home, legacy)
    assert (home / "state" / "projects.json").read_text() == "[]" and (home / "state" / "cache" / "docs" / "x.png").exists()
    assert legacy.is_symlink()


def test_existing_cadence_home_is_never_overwritten(tmp_path):
    legacy, home = tmp_path / ".kestrel", tmp_path / ".cadence"
    legacy.mkdir()
    (legacy / "x").write_text("old")
    home.mkdir()
    (home / "config.yaml").write_text("big: {name: m}")
    assert migrate_home(home, legacy) == [] and (legacy / "x").read_text() == "old"


def test_project_folder_is_renamed(tmp_path):
    (tmp_path / ".kestrel" / "plugins").mkdir(parents=True)
    (tmp_path / ".kestrel" / "plugins" / "p.py").write_text("from kestrel.tools import Tool\n")
    assert migrate_project(tmp_path)
    assert (tmp_path / ".cadence" / "plugins" / "p.py").read_text() == "from cadence.tools import Tool\n"
    assert not (tmp_path / ".kestrel").exists() and migrate_project(tmp_path) == []


def test_legacy_env_vars_are_fallbacks(monkeypatch):
    monkeypatch.setenv("KESTREL_SANDBOX", "bwrap")
    monkeypatch.setenv("KESTREL_BIG_MODEL", "old")
    monkeypatch.setenv("CADENCE_BIG_MODEL", "new")
    monkeypatch.delenv("CADENCE_SANDBOX", raising=False)
    legacy_env()
    assert os.environ["CADENCE_SANDBOX"] == "bwrap" and os.environ["CADENCE_BIG_MODEL"] == "new"
