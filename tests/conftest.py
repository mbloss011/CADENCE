from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cadence.config import Config
from cadence.llm import Completion, ModelRouter, StreamDelta, ToolCall


class ScriptedLLM:
    """Deterministic stand-in for a model. `script` items are Completion objects or callables."""

    def __init__(self, name: str, script: list[Any] | None = None, default: str = "ok"):
        self.name = name
        self.script = list(script or [])
        self.default = default
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools=None, *, on_delta=None, max_tokens=None, temperature=None) -> Completion:
        self.calls.append(json.loads(json.dumps(messages)))
        item = self.script.pop(0) if self.script else Completion(content=self.default)
        if callable(item):
            item = item(messages)
        if on_delta and item.content:
            await on_delta(StreamDelta("content", item.content))
        return item

    async def aclose(self) -> None:  # pragma: no cover
        pass


def tool_call(_tool: str, /, **args: Any) -> Completion:
    return Completion(tool_calls=[ToolCall(id=f"c_{_tool}", name=_tool, arguments=json.dumps(args))])


@pytest.fixture(autouse=True)
def _private_home(tmp_path_factory, monkeypatch):
    """Nothing a test does may write to the real ~/.cadence (caches included)."""
    import cadence.config
    monkeypatch.setattr(cadence.config, "HOME_DIR", tmp_path_factory.mktemp("home"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(workspace=tmp_path / "ws", home=tmp_path / "home")
    c.workspace.mkdir()
    return c


@pytest.fixture
def make_router():
    def _make(big_script=None, small_script=None, small_default="SUMMARY"):
        return ModelRouter(ScriptedLLM("big", big_script), ScriptedLLM("small", small_script, default=small_default))
    return _make
