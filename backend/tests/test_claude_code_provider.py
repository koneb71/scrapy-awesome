"""cli_login provider (gray zone, opt-in): locked-down options, event mapping, disabled-by-default."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scrapy_awesome.llm.base import Budget, LLMError, ToolSpec


async def _echo(text: str) -> dict:
    return {"echo": text}


SPEC = ToolSpec(
    name="echo",
    description="echo",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    fn=_echo,
)


def test_disabled_by_default_and_registry_gate(monkeypatch: pytest.MonkeyPatch):
    from scrapy_awesome.config import get_paths
    from scrapy_awesome.llm.claude_code_provider import ClaudeCodeProvider
    from scrapy_awesome.llm.registry import make_provider

    with pytest.raises(LLMError, match="disabled"):
        ClaudeCodeProvider(enabled=False)
    with pytest.raises(LLMError, match="disabled"):  # settings default: cli_login_enabled = False
        make_provider("claude_code", get_paths())


def test_run_turn_maps_sdk_messages(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk as sdk

    from scrapy_awesome.llm import claude_code_provider as ccp

    monkeypatch.setattr(ccp, "cli_available", lambda: True)
    captured: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: Any):
        captured["prompt"] = prompt
        captured["options"] = options
        # exercise our tool through the SDK MCP server config to prove the wiring
        server = options.mcp_servers["sa"]
        tool = (
            next(t for t in server["instance"]._tool_manager._tools.values())
            if hasattr(server.get("instance"), "_tool_manager")
            else None
        )
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Hello ")], model="claude-opus-5")
        if tool is not None:
            res = await tool.fn({"text": "x"})
            assert "echo" in str(res)
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="world")], model="claude-opus-5")
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=2,
            session_id="sess-1",
            total_cost_usd=0.0123,
            usage={"input_tokens": 100, "output_tokens": 20},
            result="Hello world",
        )

    monkeypatch.setattr(sdk, "query", fake_query)
    p = ccp.ClaudeCodeProvider(enabled=True)
    events: list[dict[str, Any]] = []
    res = asyncio.run(
        p.run_turn(
            model="claude-opus-5",
            system="SYS",
            history=[],
            user_message="hi",
            tools=[SPEC],
            effort="high",
            budget=Budget(limit_usd=1.0),
            on_event=events.append,
        )
    )
    assert res.text == "Hello world" and res.usage.cost_usd == 0.0  # subscription: no $ charged
    assert res.usage.input_tokens == 100 and res.usage.calls == 2
    assert res.history[-1]["session_id"] == "sess-1"
    o = captured["options"]
    assert o.system_prompt == "SYS" and o.setting_sources == [] and o.strict_mcp_config is True
    assert o.allowed_tools == ["mcp__sa__echo"] and "Bash" in o.disallowed_tools
    assert o.permission_mode == "bypassPermissions" and o.max_budget_usd is None
    assert o.include_partial_messages is True and o.effort == "high"
    types = [e["t"] for e in events]
    assert types.count("text_delta") == 2 and "usage" in types and types[-1] == "done"
    # a second turn resumes the SDK session
    asyncio.run(
        p.run_turn(
            model="claude-opus-5",
            system="SYS",
            history=res.history,
            user_message="more",
            tools=[SPEC],
            effort="high",
            budget=Budget(None),
            on_event=lambda e: None,
        )
    )
    assert captured["options"].resume == "sess-1"
