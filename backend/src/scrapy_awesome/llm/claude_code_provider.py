"""`cli_login` provider — reuses the Claude Code CLI's login through the Claude Agent SDK.

**Gray zone, opt-in, off by default.** Anthropic's Agent SDK docs state (verbatim): "Unless
previously approved, Anthropic does not allow third party developers to offer claude.ai login or
rate limits for their products, including agents built on the Claude Agent SDK." The person must
enable `llm.cli_login_enabled` in Settings → Advanced and acknowledge that quote. The compliant
subscription path is the MCP plugin (docs/auth-modes.md); this module exists because the person
explicitly asked for the toggle and accepted the risk. It is isolated here and never imported
unless the toggle is on.

Mechanics: `claude_agent_sdk.query()` runs the Claude Code harness (which inherits the CLI's
OAuth login when no API key is set); our tools are exposed to it as an in-process SDK MCP server;
everything else is locked down (no built-in tools, no settings sources, `bypassPermissions` for
our own tools only). If the SDK ever runs in `--bare` mode by default (no subscription login),
this provider simply fails with the CLI's auth error and the UI shows it.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from scrapy_awesome.llm.base import (
    Budget,
    Effort,
    LLMError,
    ModelInfo,
    OnEvent,
    ToolSpec,
    TurnResult,
    Usage,
    emit,
)
from scrapy_awesome.tools.client import ToolError

log = logging.getLogger(__name__)

POLICY_QUOTE = (
    "Unless previously approved, Anthropic does not allow third party developers to offer "
    "claude.ai login or rate limits for their products, including agents built on the Claude "
    "Agent SDK."
)
SERVER_NAME = "sa"


_CLI_CANDIDATES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
    "~/.volta/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def find_claude_cli() -> str | None:
    """Path to the `claude` CLI: PATH first, then common install locations (GUI apps such as the
    desktop shell don't inherit the shell PATH), then the SDK's bundled copy if present."""
    if found := shutil.which("claude"):
        return found
    for cand in _CLI_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled"
        for p in bundled.glob("claude*"):
            return str(p)
    except ImportError:
        pass
    return None


def cli_available() -> bool:
    return find_claude_cli() is not None


def _cli_env() -> dict[str, str]:
    """Extra env for the CLI subprocess: the macOS Keychain lookup of the claude.ai login needs
    USER/LOGNAME (+ a TMPDIR); GUI-launched or stripped environments may lack them."""
    import os
    import tempfile

    env: dict[str, str] = {}
    if not os.environ.get("USER") or not os.environ.get("LOGNAME"):
        try:
            import pwd

            name = pwd.getpwuid(os.getuid()).pw_name
            env.setdefault("USER", os.environ.get("USER") or name)
            env.setdefault("LOGNAME", os.environ.get("LOGNAME") or name)
        except Exception:  # pragma: no cover - Windows has no pwd
            pass
    if not os.environ.get("TMPDIR"):
        env["TMPDIR"] = tempfile.gettempdir()
    return env


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, *, enabled: bool) -> None:
        if not enabled:
            raise LLMError(
                "The Claude Code login provider is disabled. Enable it in Settings → Advanced "
                "(read the policy note first) or use the MCP plugin / an API key."
            )
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise LLMError(
                "claude-agent-sdk is not installed: pip install 'scrapy-awesome[claude-code]'"
            ) from exc
        if not cli_available():
            raise LLMError("the `claude` CLI is not on PATH (install Claude Code and log in).")

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id="claude-opus-5", display_name="Claude Opus 5 (via Claude Code login)"),
            ModelInfo(id="claude-sonnet-5", display_name="Claude Sonnet 5 (via Claude Code login)"),
        ]

    def _mcp_server(
        self,
        tools: list[ToolSpec],
        on_event: OnEvent,
        counter: dict[str, int],
        text_parts: list[str],
    ) -> Any:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        sdk_tools = []
        for spec in tools:

            def make(sp: ToolSpec) -> Any:
                @tool(sp.name, sp.description, sp.input_schema)
                async def _t(args: dict[str, Any]) -> dict[str, Any]:
                    counter["calls"] += 1
                    # keep prose readable: a paragraph break between text and the next tool call
                    if text_parts and not text_parts[-1].endswith("\n"):
                        text_parts.append("\n\n")
                        await emit(on_event, {"t": "text_delta", "text": "\n\n"})
                    await emit(
                        on_event, {"t": "tool_call", "id": sp.name, "name": sp.name, "input": args}
                    )
                    try:
                        out = await sp.fn(**(args or {}))
                        text = out if isinstance(out, str) else json.dumps(out, default=str)
                        await emit(
                            on_event,
                            {
                                "t": "tool_result",
                                "name": sp.name,
                                "ok": True,
                                "summary": text[:160],
                            },
                        )
                        return {"content": [{"type": "text", "text": text[:60_000]}]}
                    except ToolError as exc:
                        await emit(
                            on_event,
                            {
                                "t": "tool_result",
                                "name": sp.name,
                                "ok": False,
                                "summary": str(exc)[:200],
                            },
                        )
                        return {
                            "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                            "is_error": True,
                        }

                return _t

            sdk_tools.append(make(spec))
        return create_sdk_mcp_server(SERVER_NAME, tools=sdk_tools)

    async def run_turn(
        self,
        *,
        model: str,
        system: str,
        history: list[Any],
        user_message: str,
        tools: list[ToolSpec],
        effort: Effort,
        budget: Budget,
        on_event: OnEvent,
        max_iterations: int = 40,
    ) -> TurnResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            StreamEvent,
            TextBlock,
            query,
        )

        counter = {"calls": 0}
        # history: the SDK keeps sessions itself; we resume by session id stored in our history
        session_id = next(
            (
                h.get("session_id")
                for h in reversed(history)
                if isinstance(h, dict) and h.get("session_id")
            ),
            None,
        )
        text_parts: list[str] = []
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=model or None,
            mcp_servers={SERVER_NAME: self._mcp_server(tools, on_event, counter, text_parts)},
            allowed_tools=[f"mcp__{SERVER_NAME}__{t.name}" for t in tools],
            disallowed_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "MultiEdit",
                "Glob",
                "Grep",
                "WebFetch",
                "WebSearch",
                "Task",
                "NotebookEdit",
            ],
            permission_mode="bypassPermissions",  # only our own tools are allowed anyway
            cli_path=find_claude_cli(),
            env=_cli_env(),
            setting_sources=[],
            strict_mcp_config=True,
            max_turns=max_iterations,
            # subscription: no dollar budget (the SDK's cost is an estimate we only display)
            resume=session_id,
            effort=effort,  # type: ignore[arg-type]
            include_partial_messages=True,  # token streaming via StreamEvent
        )
        streamed = False
        usage = Usage()
        stop = "end_turn"
        new_session = session_id
        try:
            async for msg in query(prompt=user_message, options=options):
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta" and d.get("text"):
                            streamed = True
                            text_parts.append(d["text"])
                            await emit(on_event, {"t": "text_delta", "text": d["text"]})
                        elif d.get("type") == "thinking_delta" and d.get("thinking"):
                            await emit(on_event, {"t": "thinking_delta", "text": d["thinking"]})
                elif isinstance(msg, AssistantMessage):
                    if not streamed:  # no partial events (older CLI) → whole blocks
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                text_parts.append(block.text)
                                await emit(on_event, {"t": "text_delta", "text": block.text})
                elif isinstance(msg, ResultMessage):
                    new_session = msg.session_id or new_session
                    usage.calls = max(1, int(msg.num_turns or 1))
                    usage.cost_usd = 0.0  # covered by the subscription
                    u = msg.usage or {}
                    usage.input_tokens = int(u.get("input_tokens", 0) or 0)
                    usage.output_tokens = int(u.get("output_tokens", 0) or 0)
                    usage.cache_read_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
                    usage.cache_write_tokens = int(u.get("cache_creation_input_tokens", 0) or 0)
                    if msg.is_error:
                        stop = "error"
                        await emit(
                            on_event, {"t": "error", "message": str(msg.result or msg.subtype)}
                        )
                    elif not text_parts and msg.result:
                        text_parts.append(str(msg.result))
                        await emit(on_event, {"t": "text_delta", "text": str(msg.result)})
                    await emit(on_event, {"t": "usage", **usage.to_dict()})
        except Exception as exc:  # CLI not found / not logged in / transport errors
            msg = f"Claude Code provider error: {exc.__class__.__name__}: {exc}"
            await emit(on_event, {"t": "error", "message": msg})
            raise LLMError(msg) from exc
        text = "".join(text_parts)
        await emit(on_event, {"t": "done", "text": text, "stop_reason": stop})
        return TurnResult(
            text=text,
            history=[
                *history,
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": text, "session_id": new_session},
            ],
            usage=usage,
            stop_reason=stop,
            tool_calls=counter["calls"],
        )

    async def extract_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> tuple[Any, Usage]:
        raise LLMError(
            "The Claude Code login provider cannot be used for per-page fallback; use an API key for the fallback role."
        )
