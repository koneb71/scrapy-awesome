"""Provider adapters with scripted fake SDK clients (no network): streaming events, tool
execution + result feedback, history mirroring, cost/budget accounting, error surfaces."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from scrapy_awesome.llm.base import Budget, LLMError, ToolSpec, Usage
from scrapy_awesome.llm.pricing import cost_usd, rates
from scrapy_awesome.tools.client import ToolError

# ------------------------------------------------------------------ shared fixtures


async def _search(page_id: str, text: str, container: str | None = None, limit: int = 10) -> dict:
    return {"query": text, "matches": [{"css": "p.price", "relative_css": "p.price"}]}


async def _boom(page_id: str) -> dict:
    raise ToolError("page not found")


SPECS = [
    ToolSpec(
        name="search_page",
        description="find text",
        input_schema={
            "type": "object",
            "properties": {"page_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["page_id", "text"],
        },
        fn=_search,
    ),
    ToolSpec(
        name="boom",
        description="fails",
        input_schema={"type": "object", "properties": {"page_id": {"type": "string"}}},
        fn=_boom,
    ),
]


class Collector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)

    def types(self) -> list[str]:
        return [e["t"] for e in self.events]


def test_pricing_table_and_prefix_match():
    assert rates("anthropic", "claude-opus-5-20260601") == rates("anthropic", "claude-opus-5")
    assert cost_usd("anthropic", "claude-opus-5", input_tokens=1_000_000) == 5.0
    assert cost_usd("gemini", "gemini-3.7-flash", output_tokens=1_000_000) == 3.0
    assert (
        cost_usd("gemini", "unknown-model", input_tokens=1_000_000) == 5.0
    )  # conservative default


def test_budget_charges_and_raises():
    from scrapy_awesome.llm.base import BudgetExceeded

    b = Budget(limit_usd=1.0)
    b.charge(0.4)
    assert b.remaining_usd == pytest.approx(0.6)
    with pytest.raises(BudgetExceeded):
        b.charge(0.7)
    assert Budget(limit_usd=None).remaining_usd is None


# ------------------------------------------------------------------ Anthropic fake runner


class _Delta(SimpleNamespace):
    pass


def _text_event(text: str) -> Any:
    return SimpleNamespace(type="content_block_delta", delta=_Delta(type="text_delta", text=text))


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        return d


class FakeStream:
    def __init__(self, turn: dict[str, Any]) -> None:
        self.turn = turn

    def __aiter__(self):
        async def gen():
            for t in self.turn.get("text", []):
                yield _text_event(t)

        return gen()

    async def get_final_message(self) -> Any:
        content = [_Block(type="text", text="".join(self.turn.get("text", [])))]
        for i, (name, inp) in enumerate(self.turn.get("tools", [])):
            content.append(_Block(type="tool_use", id=f"tu_{i}", name=name, input=inp))
        return SimpleNamespace(
            stop_reason=self.turn.get("stop", "tool_use" if self.turn.get("tools") else "end_turn"),
            content=content,
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=100,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


class FakeRunner:
    """Mimics BetaAsyncStreamingToolRunner: yields streams, executes tools on request."""

    def __init__(self, script: list[dict[str, Any]], tools: list[Any]) -> None:
        self.script = script
        self.tools = {t.name: t for t in tools}
        self._i = -1
        self._cached: Any = None

    def __aiter__(self):
        async def gen():
            for i, _turn in enumerate(self.script):
                self._i = i
                self._cached = None
                yield FakeStream(self.script[i])
                if not self.script[i].get("tools"):
                    return

        return gen()

    async def generate_tool_call_response(self) -> Any:
        if self._cached is not None:
            return self._cached
        turn = self.script[self._i]
        if not turn.get("tools"):
            return None
        results = []
        for i, (name, inp) in enumerate(turn["tools"]):
            out = await self.tools[name].call(inp)
            results.append({"type": "tool_result", "tool_use_id": f"tu_{i}", "content": out})
        self._cached = {"role": "user", "content": results}
        return self._cached


def _anthropic_provider(script: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch):
    from scrapy_awesome.llm.anthropic_provider import AnthropicProvider

    p = AnthropicProvider("sk-test")
    captured: dict[str, Any] = {}

    def fake_tool_runner(**kw: Any) -> FakeRunner:
        captured.update(kw)
        return FakeRunner(script, kw["tools"])

    monkeypatch.setattr(p.client.beta.messages, "tool_runner", fake_tool_runner)
    return p, captured


def test_anthropic_turn_streams_tools_and_mirrors_history(monkeypatch: pytest.MonkeyPatch):
    script = [
        {
            "text": ["Let me ", "look."],
            "tools": [("search_page", {"page_id": "p1", "text": "£11"})],
        },
        {"text": ["Found ", "it."], "tools": [("boom", {"page_id": "p1"})]},
        {"text": ["Done."]},
    ]
    p, captured = _anthropic_provider(script, monkeypatch)
    col = Collector()
    res = asyncio.run(
        p.run_turn(
            model="claude-opus-5",
            system="SYS",
            history=[],
            user_message="find the price",
            tools=SPECS,
            effort="high",
            budget=Budget(limit_usd=5.0),
            on_event=col,
        )
    )
    assert res.text == "Let me look.Found it.Done."
    assert res.stop_reason == "end_turn" and res.tool_calls == 2
    ts = col.types()
    assert (
        ts.count("text_delta") == 5
        and "tool_call" in ts
        and "tool_result" in ts
        and ts[-1] == "done"
    )
    results = [e for e in col.events if e["t"] == "tool_result"]
    assert (
        results[0]["ok"] is True
        and results[1]["ok"] is False
        and "page not found" in results[1]["summary"]
    )
    # cost: 3 calls × (1000 in + 100 out) at opus-5 rates
    assert res.usage.calls == 3
    assert res.usage.cost_usd == pytest.approx(3 * (1000 * 5 + 100 * 25) / 1e6)
    # request shape: cached system, adaptive thinking, effort, streaming
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["thinking"] == {"type": "adaptive"} and captured["output_config"] == {
        "effort": "high"
    }
    assert captured["stream"] is True
    # mirrored, JSON-safe history: user, assistant(+tool_use), tool_result, assistant, tool_result, assistant
    roles = [m["role"] for m in res.history]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    json.dumps(res.history)
    tool_result_msg = res.history[2]
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert "p.price" in tool_result_msg["content"][0]["content"]


def test_anthropic_budget_stops_loop(monkeypatch: pytest.MonkeyPatch):
    script = [
        {"text": ["a"], "tools": [("search_page", {"page_id": "p1", "text": "x"})]},
        {"text": ["b"], "tools": [("search_page", {"page_id": "p1", "text": "y"})]},
        {"text": ["c"]},
    ]
    p, _ = _anthropic_provider(script, monkeypatch)
    col = Collector()
    res = asyncio.run(
        p.run_turn(
            model="claude-opus-5",
            system="S",
            history=[],
            user_message="go",
            tools=SPECS,
            effort="low",
            budget=Budget(limit_usd=0.001),  # first call costs ~0.0075
            on_event=col,
        )
    )
    assert res.stop_reason == "budget"
    assert any(e["t"] == "error" and "budget" in e["message"] for e in col.events)
    assert res.usage.calls == 1


def test_anthropic_refusal_is_terminal(monkeypatch: pytest.MonkeyPatch):
    p, _ = _anthropic_provider([{"text": ["no"], "stop": "refusal"}], monkeypatch)
    col = Collector()
    res = asyncio.run(
        p.run_turn(
            model="claude-opus-5",
            system="S",
            history=[],
            user_message="x",
            tools=SPECS,
            effort="low",
            budget=Budget(None),
            on_event=col,
        )
    )
    assert res.stop_reason == "refusal" and any(e["t"] == "error" for e in col.events)


def test_anthropic_api_error_maps_to_llmerror(monkeypatch: pytest.MonkeyPatch):
    import httpx
    from anthropic import AuthenticationError

    from scrapy_awesome.llm.anthropic_provider import AnthropicProvider

    p = AnthropicProvider("sk-bad")

    class Boom:
        def __aiter__(self):
            async def gen():
                req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                resp = httpx.Response(
                    401, request=req, json={"error": {"message": "invalid x-api-key"}}
                )
                raise AuthenticationError("auth", response=resp, body=resp.json())
                yield  # pragma: no cover

            return gen()

    monkeypatch.setattr(p.client.beta.messages, "tool_runner", lambda **kw: Boom())
    with pytest.raises(LLMError, match="401"):
        asyncio.run(
            p.run_turn(
                model="claude-opus-5",
                system="S",
                history=[],
                user_message="x",
                tools=[],
                effort="low",
                budget=Budget(None),
                on_event=lambda e: None,
            )
        )


# ------------------------------------------------------------------ Gemini fake stream


def _gemini_provider(script: list[list[Any]], monkeypatch: pytest.MonkeyPatch):
    """script: one entry per model call; each is a list of Parts for that call."""
    from google.genai import types

    from scrapy_awesome.llm.gemini_provider import GeminiProvider

    p = GeminiProvider("gk-test")
    calls: list[Any] = []
    it = iter(script)

    async def fake_stream(*, model: str, contents: Any, config: Any) -> Any:
        calls.append({"model": model, "contents": list(contents), "config": config})
        parts = next(it)

        async def gen():
            for part in parts:
                yield types.GenerateContentResponse(
                    candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
                )
            yield types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(role="model", parts=[]), finish_reason="STOP"
                    )
                ],
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=1000, candidates_token_count=100, thoughts_token_count=50
                ),
            )

        return gen()

    monkeypatch.setattr(p.client.aio.models, "generate_content_stream", fake_stream)
    return p, calls


def test_gemini_turn_function_calling_loop(monkeypatch: pytest.MonkeyPatch):
    from google.genai import types

    script = [
        [
            types.Part(text="Looking…", thought=True),
            types.Part(text="Let me search. "),
            types.Part.from_function_call(
                name="search_page", args={"page_id": "p1", "text": "£11"}
            ),
        ],
        [types.Part.from_function_call(name="boom", args={"page_id": "p1"})],
        [types.Part(text="Done.")],
    ]
    p, calls = _gemini_provider(script, monkeypatch)
    col = Collector()
    res = asyncio.run(
        p.run_turn(
            model="gemini-3.7-flash",
            system="SYS",
            history=[],
            user_message="find price",
            tools=SPECS,
            effort="high",
            budget=Budget(limit_usd=5.0),
            on_event=col,
        )
    )
    assert (
        res.text == "Let me search. Done." and res.tool_calls == 2 and res.stop_reason == "end_turn"
    )
    ts = col.types()
    assert "thinking_delta" in ts and ts.count("tool_call") == 2 and ts.count("tool_result") == 2
    assert res.usage.calls == 3 and res.usage.output_tokens == 3 * 150
    # tool results were fed back as function_response parts; history is JSON-safe
    json.dumps(res.history)
    roles = [c["role"] for c in res.history]
    assert roles == ["user", "model", "user", "model", "user", "model"]
    fr = res.history[2]["parts"][0]["function_response"]
    assert fr["name"] == "search_page" and "matches" in fr["response"]
    err = res.history[4]["parts"][0]["function_response"]["response"]
    assert "page not found" in err["error"]
    # request config: system instruction, declarations with raw JSON schema, AFC disabled
    cfg = calls[0]["config"]
    assert cfg.system_instruction == "SYS"
    assert cfg.tools[0].function_declarations[0].parameters_json_schema["type"] == "object"
    assert cfg.automatic_function_calling.disable is True


def test_gemini_history_roundtrip(monkeypatch: pytest.MonkeyPatch):
    from google.genai import types

    p, calls = _gemini_provider([[types.Part(text="hi")], [types.Part(text="again")]], monkeypatch)
    r1 = asyncio.run(
        p.run_turn(
            model="gemini-3.7-flash",
            system="S",
            history=[],
            user_message="a",
            tools=[],
            effort="low",
            budget=Budget(None),
            on_event=lambda e: None,
        )
    )
    r2 = asyncio.run(
        p.run_turn(
            model="gemini-3.7-flash",
            system="S",
            history=r1.history,
            user_message="b",
            tools=[],
            effort="low",
            budget=Budget(None),
            on_event=lambda e: None,
        )
    )
    assert [c["role"] for c in r2.history] == ["user", "model", "user", "model"]
    assert len(calls[1]["contents"]) == 3  # prior 2 + new user


def test_usage_add():
    u = Usage(input_tokens=1, output_tokens=2, cost_usd=0.5, calls=1)
    u.add(Usage(input_tokens=3, cost_usd=0.25, calls=1))
    assert u.to_dict()["input_tokens"] == 4 and u.cost_usd == 0.75 and u.calls == 2
