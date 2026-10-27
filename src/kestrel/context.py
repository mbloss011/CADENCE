"""Dynamic context management.

The small model owns this module's heavy lifting. Compaction runs in three escalating
stages, chosen by how full the context window is:

1. *Shrink*   — deterministic head/tail truncation of oversized old tool outputs.
2. *Distill*  — small model condenses very large old tool outputs to the facts that matter.
3. *Fold*     — small model folds the oldest turns into a rolling summary
                (chunked so a 1.2B model never sees more than it can handle).

Recent turns are never touched, and tool-call/tool-result pairs are never split.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import Config
from .llm import LLM, LLMError, content_text, estimate_tokens, message_tokens, messages_tokens

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "You compress coding-agent transcripts. Output a dense, factual summary in markdown with these "
    "sections, omitting empty ones: **Goal**, **Decisions**, **Files touched** (path — what changed), "
    "**Commands & results**, **Errors & fixes**, **Open items**. Keep exact file paths, function names, "
    "error messages and numbers. No preamble, no speculation."
)
DISTILL_SYSTEM = (
    "You condense tool output for a coding agent. Keep exact paths, line numbers, error messages, "
    "identifiers and numeric results. Drop noise and repetition. Output only the condensed text."
)


@dataclass
class CompactionEvent:
    stage: str
    tokens_before: int
    tokens_after: int
    detail: str = ""


@dataclass
class ContextManager:
    cfg: Config
    small: LLM
    messages: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    compactions: int = 0
    on_event: Callable[[CompactionEvent], Awaitable[None]] | None = None

    # ------------------------------------------------------------------ basics
    @property
    def window(self) -> int:
        return self.cfg.big.context_window

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def reset(self) -> None:
        self.messages.clear()
        self.summary = ""

    def build(self, system_prompt: str) -> list[dict[str, Any]]:
        system = system_prompt
        if self.summary:
            system += "\n\n## Conversation summary (earlier turns, compacted)\n" + self.summary
        return [{"role": "system", "content": system}, *self.messages]

    def usage(self, system_prompt: str, tools_tokens: int = 0) -> int:
        return messages_tokens(self.build(system_prompt)) + tools_tokens

    def pressure(self, system_prompt: str, tools_tokens: int = 0) -> float:
        return self.usage(system_prompt, tools_tokens) / max(1, self.window)

    # --------------------------------------------------------------- compaction
    async def maybe_compact(self, system_prompt: str, tools_tokens: int = 0) -> list[CompactionEvent]:
        """Compact only when over the trigger threshold. Returns events describing what ran."""
        events: list[CompactionEvent] = []
        trigger = int(self.window * self.cfg.compact_trigger)
        target = int(self.window * self.cfg.compact_target)
        if self.usage(system_prompt, tools_tokens) <= trigger:
            return events

        for stage in (self._shrink, self._distill, self._fold):
            before = self.usage(system_prompt, tools_tokens)
            if before <= target:
                break
            detail = await stage(system_prompt, tools_tokens, target)
            after = self.usage(system_prompt, tools_tokens)
            if detail is not None:
                ev = CompactionEvent(stage.__name__.lstrip("_"), before, after, detail)
                events.append(ev)
                if self.on_event:
                    await self.on_event(ev)

        # Last resort: the summary itself or the recent tail still overflows.
        if self.usage(system_prompt, tools_tokens) > int(self.window * 0.92):
            before = self.usage(system_prompt, tools_tokens)
            self._emergency_trim(system_prompt, tools_tokens)
            ev = CompactionEvent("emergency", before, self.usage(system_prompt, tools_tokens), "dropped oldest turns")
            events.append(ev)
            if self.on_event:
                await self.on_event(ev)
        if events:
            self.compactions += 1
        return events

    def _old_region(self) -> int:
        """Index separating compactable history from the protected recent tail."""
        cut = max(0, len(self.messages) - self.cfg.keep_recent_messages)
        return self._safe_cut(cut)

    def _safe_cut(self, cut: int) -> int:
        """Move `cut` forward so the tail never starts with an orphaned tool result."""
        while cut < len(self.messages) and self.messages[cut].get("role") == "tool":
            cut += 1
        return cut

    async def _shrink(self, system_prompt: str, tools_tokens: int, target: int) -> str | None:
        limit = max(600, self.cfg.tool_output_limit // 4)
        saved = 0
        for msg in self.messages[: self._old_region()]:
            content = msg.get("content")
            if msg.get("role") == "tool" and isinstance(content, str) and len(content) > limit:
                head, tail = content[: limit // 2], content[-limit // 2 :]
                omitted = len(content) - limit
                msg["content"] = f"{head}\n…[{omitted} chars omitted by compaction]…\n{tail}"
                saved += omitted
        return f"trimmed {saved} chars of old tool output" if saved else None

    async def _distill(self, system_prompt: str, tools_tokens: int, target: int) -> str | None:
        count = 0
        for msg in self.messages[: self._old_region()]:
            content = msg.get("content")
            if msg.get("role") != "tool" or not isinstance(content, str) or len(content) < 1800:
                continue
            if content.startswith("[distilled]"):
                continue
            try:
                text = await self._ask_small(DISTILL_SYSTEM, content[:12000], max_tokens=400)
            except LLMError as exc:
                log.warning("distill failed: %s", exc)
                return f"distill skipped ({exc})" if count == 0 else f"distilled {count} tool outputs"
            if text:
                msg["content"] = "[distilled] " + text
                count += 1
            if self.usage(system_prompt, tools_tokens) <= target:
                break
        return f"distilled {count} tool outputs" if count else None

    async def _fold(self, system_prompt: str, tools_tokens: int, target: int) -> str | None:
        """Fold oldest turns into the rolling summary, chunk by chunk, until under target."""
        folded = 0
        while self.usage(system_prompt, tools_tokens) > target:
            cut = self._old_region()
            if cut <= 0:
                break
            # Adaptive chunk: bigger bites under heavy pressure, never > ~6k tokens for the small model.
            pressure = self.pressure(system_prompt, tools_tokens)
            budget = 6000 if pressure > 0.85 else 3500
            take, used = 0, 0
            for i in range(cut):
                t = message_tokens(self.messages[i])
                if used + t > budget and take > 0:
                    break
                used += t
                take = i + 1
            take = self._safe_cut(take)
            take = min(take, cut) if take <= cut else cut
            if take <= 0:
                break
            chunk = self.messages[:take]
            self.summary = await self._summarize(chunk)
            del self.messages[:take]
            folded += take
        return f"folded {folded} messages into rolling summary" if folded else None

    async def _summarize(self, chunk: list[dict[str, Any]]) -> str:
        transcript = render_transcript(chunk, per_message_limit=1500)
        prompt = ""
        if self.summary:
            prompt += f"## Existing summary\n{self.summary}\n\n"
        prompt += f"## New transcript to merge\n{transcript}\n\nWrite the updated merged summary."
        try:
            text = await self._ask_small(SUMMARY_SYSTEM, prompt, max_tokens=900)
            if text.strip():
                return text.strip()
        except LLMError as exc:
            log.warning("small-model summary failed, using deterministic fallback: %s", exc)
        return deterministic_summary(self.summary, chunk)

    async def _ask_small(self, system: str, user: str, max_tokens: int) -> str:
        comp = await self.small.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        return comp.content.strip()

    def _emergency_trim(self, system_prompt: str, tools_tokens: int) -> None:
        if len(self.summary) > 6000:
            self.summary = self.summary[-6000:]
        while self.usage(system_prompt, tools_tokens) > int(self.window * 0.85) and len(self.messages) > 2:
            del self.messages[0]
            while self.messages and self.messages[0].get("role") == "tool":
                del self.messages[0]


def render_transcript(messages: list[dict[str, Any]], per_message_limit: int = 1500) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = content_text(m.get("content"))
        if len(content) > per_message_limit:
            content = content[: per_message_limit // 2] + " …[cut]… " + content[-per_message_limit // 2 :]
        if role == "assistant" and m.get("tool_calls"):
            calls = "; ".join(
                f"{tc['function']['name']}({tc['function']['arguments'][:200]})" for tc in m["tool_calls"]
            )
            lines.append(f"[assistant → tools] {calls}")
            if content:
                lines.append(f"[assistant] {content}")
        else:
            lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def deterministic_summary(previous: str, messages: list[dict[str, Any]]) -> str:
    """No-model fallback: keep user asks and a compact record of tool activity."""
    parts = [previous.strip()] if previous.strip() else []
    for m in messages:
        role, content = m.get("role"), content_text(m.get("content"))
        if role == "user":
            parts.append(f"- User: {content[:300]}")
        elif role == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            parts.append(f"- Ran tools: {names}")
        elif role == "assistant" and content:
            parts.append(f"- Assistant: {content[:200]}")
    text = "\n".join(parts)
    return text[-5000:]


__all__ = ["ContextManager", "CompactionEvent", "render_transcript", "deterministic_summary", "estimate_tokens"]
