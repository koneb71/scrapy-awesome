"""Opt-in live checks against the real providers (need keys; never run in CI by default).

SA_LIVE=1 ANTHROPIC_API_KEY=… uv run pytest -m live tests/test_llm_live.py
SA_LIVE=1 GEMINI_API_KEY=…    uv run pytest -m live tests/test_llm_live.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from scrapy_awesome.llm.base import Budget, ToolSpec

pytestmark = pytest.mark.live

_ADD_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
}


async def _add(a: float, b: float) -> dict:
    return {"sum": a + b}


SPECS = [ToolSpec(name="add", description="Add two numbers", input_schema=_ADD_SCHEMA, fn=_add)]


def _skip_unless(env: str) -> str:
    if not os.environ.get("SA_LIVE"):
        pytest.skip("set SA_LIVE=1 to run live provider tests")
    key = os.environ.get(env)
    if not key:
        pytest.skip(f"{env} not set")
    return key


def _collect():
    events = []
    return events, (lambda e: events.append(e))


def test_anthropic_live_tool_turn():
    from scrapy_awesome.llm.anthropic_provider import AnthropicProvider

    p = AnthropicProvider(_skip_unless("ANTHROPIC_API_KEY"))
    events, on = _collect()
    res = asyncio.run(
        p.run_turn(
            model=os.environ.get("SA_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5"),
            system="You are terse. Use the add tool for arithmetic and reply with just the number.",
            history=[],
            user_message="What is 41 + 1?",
            tools=SPECS,
            effort="low",
            budget=Budget(limit_usd=0.5),
            on_event=on,
        )
    )
    assert "42" in res.text
    assert any(e["t"] == "tool_call" and e["name"] == "add" for e in events)
    assert res.usage.calls >= 2 and res.usage.cost_usd > 0
    models = asyncio.run(p.list_models())
    assert any(m.id.startswith("claude") for m in models)


def test_gemini_live_tool_turn():
    from scrapy_awesome.llm.gemini_provider import GeminiProvider

    p = GeminiProvider(_skip_unless("GEMINI_API_KEY"))
    events, on = _collect()
    res = asyncio.run(
        p.run_turn(
            model=os.environ.get("SA_LIVE_GEMINI_MODEL", "gemini-2.5-flash"),
            system="You are terse. Use the add tool for arithmetic and reply with just the number.",
            history=[],
            user_message="What is 41 + 1?",
            tools=SPECS,
            effort="low",
            budget=Budget(limit_usd=0.5),
            on_event=on,
        )
    )
    assert "42" in res.text
    assert any(e["t"] == "tool_call" and e["name"] == "add" for e in events)
    assert res.usage.calls >= 2
    models = asyncio.run(p.list_models())
    assert any(m.id.startswith("gemini") for m in models)
