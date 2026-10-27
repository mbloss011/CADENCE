from __future__ import annotations

from kestrel.context import ContextManager


def _fill(ctx: ContextManager, turns: int, size: int = 1200) -> None:
    for i in range(turns):
        ctx.add({"role": "user", "content": f"task {i} " + "x" * size})
        ctx.add({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"t{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
        ctx.add({"role": "tool", "tool_call_id": f"t{i}", "content": "out " + "y" * size})
        ctx.add({"role": "assistant", "content": f"done {i}"})


async def test_no_compaction_below_threshold(cfg, make_router):
    router = make_router()
    ctx = ContextManager(cfg, router.small)
    _fill(ctx, 1, 100)
    assert await ctx.maybe_compact("sys") == []
    assert router.small.calls == []


async def test_small_model_folds_history_and_keeps_pairs_intact(cfg, make_router):
    cfg.big.context_window = 6000
    router = make_router(small_default="- Goal: things\n- Files touched: a.py")
    ctx = ContextManager(cfg, router.small)
    _fill(ctx, 12)
    events = await ctx.maybe_compact("sys")
    assert events, "compaction should have triggered"
    assert router.small.calls, "the SMALL model must do the summarising"
    assert "a.py" in ctx.summary
    assert ctx.usage("sys") < cfg.big.context_window * cfg.compact_trigger
    # the retained tail must never start with an orphaned tool result
    assert ctx.messages[0]["role"] != "tool"
    ids = {tc["id"] for m in ctx.messages if m.get("tool_calls") for tc in m["tool_calls"]}
    assert all(m["tool_call_id"] in ids for m in ctx.messages if m["role"] == "tool")


async def test_summary_included_in_system_prompt(cfg, make_router):
    ctx = ContextManager(cfg, make_router().small)
    ctx.summary = "earlier: fixed bug in parser"
    assert "fixed bug in parser" in ctx.build("SYS")[0]["content"]


async def test_small_model_failure_falls_back_to_deterministic_summary(cfg, make_router):
    from kestrel.llm import LLMError

    cfg.big.context_window = 6000
    router = make_router()

    async def boom(*a, **k):
        raise LLMError("small model down")

    router.small.complete = boom
    ctx = ContextManager(cfg, router.small)
    _fill(ctx, 12)
    await ctx.maybe_compact("sys")
    assert "User:" in ctx.summary
    assert ctx.usage("sys") <= cfg.big.context_window
