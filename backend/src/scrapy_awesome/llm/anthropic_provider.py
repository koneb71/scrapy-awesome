"""Claude via the Anthropic API (user's own API key) — "Powered by Claude".

Uses the SDK's beta **async streaming tool runner**: each iteration yields a message stream we
forward as `text_delta` / `thinking_delta` events; the runner then executes our tools and loops
until Claude stops calling them. Adaptive thinking, `output_config.effort`, prompt caching on
the (stable) system prompt + tool definitions, refusal handling and per-call cost accounting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from scrapy_awesome.llm.base import (
    Budget,
    BudgetExceeded,
    Effort,
    LLMError,
    ModelInfo,
    OnEvent,
    ToolSpec,
    TurnResult,
    Usage,
    emit,
)
from scrapy_awesome.llm.pricing import cost_usd
from scrapy_awesome.tools.client import ToolError

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 60_000
DEFAULT_MAX_TOKENS = 16_000


def _truncate(s: str, n: int = MAX_TOOL_RESULT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n… (truncated, {len(s) - n} more chars)"


def _summary(result: Any) -> str:
    """Short human line for the UI's tool chip."""
    if isinstance(result, str):
        return result[:160].replace("\n", " ")
    if isinstance(result, dict):
        keys = [
            k
            for k in ("ok", "status", "row_count", "matches", "id", "version", "rows")
            if k in result
        ]
        bits = [f"{k}={result[k]}" for k in keys][:4]
        return ", ".join(bits) or f"{len(result)} keys"
    if isinstance(result, list):
        return f"{len(result)} items"
    return str(result)[:160]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        if not api_key:
            raise LLMError("No Anthropic API key. Add one in Settings → AI providers.")
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=2)

    # ------------------------------------------------------------------ models
    async def list_models(self) -> list[ModelInfo]:
        try:
            page = await self.client.models.list(limit=100)
        except Exception as exc:  # bad key, network
            raise LLMError(
                f"Anthropic: could not list models ({exc.__class__.__name__}: {exc})"
            ) from exc
        out = []
        async for m in page:
            out.append(ModelInfo(id=m.id, display_name=getattr(m, "display_name", "") or m.id))
        # newest first as the API returns; keep claude-* only
        return [m for m in out if m.id.startswith("claude")]

    # ------------------------------------------------------------------ turn
    def _wrap_tools(
        self, tools: list[ToolSpec], on_event: OnEvent, counter: dict[str, int]
    ) -> list[Any]:
        from anthropic.lib.tools import BetaAsyncFunctionTool

        wrapped = []
        for spec in tools:

            def make(sp: ToolSpec) -> Any:
                async def _call(**kwargs: Any) -> str:
                    counter["calls"] += 1
                    try:
                        result = await sp.fn(**kwargs)
                        await emit(
                            on_event,
                            {
                                "t": "tool_result",
                                "name": sp.name,
                                "ok": True,
                                "summary": _summary(result),
                            },
                        )
                        text = (
                            result if isinstance(result, str) else json.dumps(result, default=str)
                        )
                        return _truncate(text)
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
                        return f"ERROR: {exc}"
                    except Exception as exc:  # never crash the loop on a tool bug
                        log.exception("tool %s failed", sp.name)
                        await emit(
                            on_event,
                            {
                                "t": "tool_result",
                                "name": sp.name,
                                "ok": False,
                                "summary": repr(exc)[:200],
                            },
                        )
                        return f"ERROR: {exc.__class__.__name__}: {exc}"

                _call.__name__ = sp.name
                return BetaAsyncFunctionTool(
                    _call, name=sp.name, description=sp.description, input_schema=sp.input_schema
                )

            wrapped.append(make(spec))
        # cache breakpoint after the last tool definition (tools + system are stable per session)
        if wrapped:
            wrapped[-1]._cache_control = {"type": "ephemeral"}
        return wrapped

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
        from anthropic import APIStatusError

        messages: list[Any] = [*history, {"role": "user", "content": user_message}]
        counter = {"calls": 0}
        total = Usage()
        text_parts: list[str] = []
        stop_reason = "end_turn"

        runner = self.client.beta.messages.tool_runner(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=self._wrap_tools(tools, on_event, counter),
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            max_iterations=max_iterations,
            stream=True,
        )
        try:
            async for stream in runner:
                async for event in stream:
                    et = getattr(event, "type", "")
                    if et == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            text_parts.append(d.text)
                            await emit(on_event, {"t": "text_delta", "text": d.text})
                        elif d.type == "thinking_delta":
                            await emit(on_event, {"t": "thinking_delta", "text": d.thinking})
                final = await stream.get_final_message()
                stop_reason = final.stop_reason or "end_turn"
                # accounting per model call
                u = final.usage
                call_usage = Usage(
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_input_tokens or 0,
                    cache_write_tokens=u.cache_creation_input_tokens or 0,
                    calls=1,
                )
                call_usage.cost_usd = cost_usd(
                    "anthropic",
                    model,
                    input_tokens=call_usage.input_tokens,
                    output_tokens=call_usage.output_tokens,
                    cache_read_tokens=call_usage.cache_read_tokens,
                    cache_write_tokens=call_usage.cache_write_tokens,
                )
                total.add(call_usage)
                await emit(on_event, {"t": "usage", **total.to_dict()})
                # tool calls the runner is about to execute
                for block in final.content:
                    if block.type == "tool_use":
                        await emit(
                            on_event,
                            {
                                "t": "tool_call",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            },
                        )
                # mirror the history (the runner keeps its own copy)
                messages.append(
                    {
                        "role": "assistant",
                        "content": [b.model_dump(exclude_none=True) for b in final.content],
                    }
                )
                tool_response = await runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)
                if stop_reason == "refusal":
                    await emit(
                        on_event,
                        {"t": "error", "message": "Claude declined this request (refusal)."},
                    )
                    break
                budget.charge(call_usage.cost_usd)  # raises BudgetExceeded → stops the loop
                if stop_reason == "max_tokens":
                    text_parts.append("\n\n[stopped: max_tokens]")
        except BudgetExceeded as exc:
            await emit(on_event, {"t": "error", "message": str(exc)})
            stop_reason = "budget"
        except APIStatusError as exc:
            msg = _api_error(exc)
            await emit(on_event, {"t": "error", "message": msg})
            raise LLMError(msg) from exc

        text = "".join(text_parts)
        await emit(on_event, {"t": "done", "text": text, "stop_reason": stop_reason})
        return TurnResult(
            text=text,
            history=_jsonable(messages),
            usage=total,
            stop_reason=stop_reason,
            tool_calls=counter["calls"],
        )

    # ------------------------------------------------------------------ one-shot JSON
    async def extract_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> tuple[Any, Usage]:
        from anthropic import APIStatusError

        try:
            resp = await self.client.beta.messages.create(
                model=model,
                max_tokens=8_000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except APIStatusError as exc:
            raise LLMError(_api_error(exc)) from exc
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            calls=1,
        )
        usage.cost_usd = cost_usd(
            "anthropic", model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )
        budget.charge(usage.cost_usd)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned non-JSON: {text[:200]}") from exc


def _api_error(exc: Any) -> str:
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    detail = ""
    if isinstance(body, dict):
        err = body.get("error") or {}
        detail = err.get("message") or ""
    hint = {
        401: "invalid API key — check Settings → AI providers",
        403: "key lacks permission for this model",
        404: "model not found — pick another in Settings",
        429: "rate limited / out of credits",
        529: "Anthropic API overloaded — retry shortly",
    }.get(status or 0, "")
    return f"Anthropic API error {status}: {detail or exc}" + (f" ({hint})" if hint else "")


def _jsonable(messages: list[Any]) -> list[Any]:
    """Provider history must survive JSON round-trips (we persist it)."""
    return json.loads(
        json.dumps(
            messages, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)
        )
    )
