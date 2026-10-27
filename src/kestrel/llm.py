"""Async OpenAI-compatible LLM client + big/small router.

The *big* model plans and writes code. The *small* model does cheap housekeeping:
context compaction, tool-output distillation, goal classification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

import httpx

from .config import Config, ModelConfig


class LLMError(RuntimeError):
    """Raised when a model endpoint is unreachable or returns an error."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as emitted by the model

    def parsed(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON arguments for {self.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"arguments for {self.name} must be a JSON object")
        return value


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments or "{}"}}
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class StreamDelta:
    kind: str  # "content" | "reasoning"
    text: str


class LLM(Protocol):
    name: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: Any = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion: ...

    async def aclose(self) -> None: ...


class OpenAICompatLLM:
    """Streaming chat-completions client for LM Studio / llama.cpp / vLLM / Ollama."""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=httpx.Timeout(cfg.timeout, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach {self.cfg.base_url}: {exc}") from exc
        return [m.get("id", "") for m in resp.json().get("data", [])]

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: Any = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.cfg.name,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        out = Completion()
        calls: dict[int, ToolCall] = {}
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise LLMError(f"{self.cfg.name}: HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        out.usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            out.finish_reason = choice["finish_reason"]
                        text = delta.get("content")
                        if text:
                            out.content += text
                            if on_delta:
                                await on_delta(StreamDelta("content", text))
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            out.reasoning += reasoning
                            if on_delta:
                                await on_delta(StreamDelta("reasoning", reasoning))
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = calls.setdefault(idx, ToolCall(id=tc.get("id") or f"call_{idx}", name="", arguments=""))
                            if tc.get("id"):
                                slot.id = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot.name += fn["name"]
                            if fn.get("arguments"):
                                slot.arguments += fn["arguments"]
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.cfg.name}: connection failed ({exc.__class__.__name__}: {exc})") from exc

        out.tool_calls = [calls[i] for i in sorted(calls) if calls[i].name]
        return out


class OpenAICompatEmbedder:
    """`/embeddings` client (LM Studio serves nomic-embed and friends here)."""

    def __init__(self, base_url: str, model: str, api_key: str = "lm-studio", timeout: float = 60.0):
        self.name = model
        self.base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"},
                                         timeout=httpx.Timeout(timeout, connect=10.0))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = await self._client.post("/embeddings", json={"model": self.name, "input": texts})
        except httpx.HTTPError as exc:
            raise LLMError(f"embedder {self.name}: connection failed ({exc.__class__.__name__}: {exc})") from exc
        if resp.status_code >= 400:
            raise LLMError(f"embedder {self.name}: HTTP {resp.status_code}: {resp.text[:300]}")
        rows = sorted(resp.json().get("data", []), key=lambda r: r.get("index", 0))
        if len(rows) != len(texts):
            raise LLMError(f"embedder {self.name}: expected {len(texts)} vectors, got {len(rows)}")
        return [r["embedding"] for r in rows]

    async def aclose(self) -> None:
        await self._client.aclose()


class ModelRouter:
    """Holds the big, small and vision model and exposes intent-named accessors."""

    def __init__(self, big: LLM, small: LLM, vision: LLM | None = None):
        self.big = big
        self.small = small
        self.vision = vision or big

    @classmethod
    def from_config(cls, cfg: Config) -> "ModelRouter":
        big = OpenAICompatLLM(cfg.big)
        small = big if cfg.small.name == cfg.big.name and cfg.small.base_url == cfg.big.base_url else OpenAICompatLLM(cfg.small)
        vision = None
        if cfg.vision and (cfg.vision.name, cfg.vision.base_url) != (cfg.big.name, cfg.big.base_url):
            vision = OpenAICompatLLM(cfg.vision)
        return cls(big, small, vision)

    async def aclose(self) -> None:
        await self.big.aclose()
        if self.small is not self.big:
            await self.small.aclose()
        if self.vision is not self.big:
            await self.vision.aclose()


IMAGE_TOKENS = 1100  # rough per-image cost for typical VLMs at default resolution


def content_text(content: Any) -> str:
    """Plain text of a message's content; image parts become `[image]` so nothing ever sees raw base64."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                out.append("[image]")
        return "\n".join(out)
    return "" if content is None else str(content)


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~3.6 chars/token for code-heavy text)."""
    return max(1, int(len(text) / 3.6))


def message_tokens(msg: dict[str, Any]) -> int:
    total = 4
    content = msg.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                total += IMAGE_TOKENS if p.get("type") == "image_url" else estimate_tokens(str(p.get("text", "")))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        total += estimate_tokens(fn.get("name", "") + fn.get("arguments", ""))
    return total


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)
