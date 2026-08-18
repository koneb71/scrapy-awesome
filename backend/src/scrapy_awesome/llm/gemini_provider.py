"""Gemini via the Google Gen AI SDK (user's own API key).

A manual function-calling loop (streaming) rather than the SDK's automatic function calling, so
we can stream text, report every tool call/result to the UI, enforce budgets between calls and
keep the history JSON-serialisable. Thought parts are never fed back as text; `thought_signature`
parts are preserved on the model turn so multi-step reasoning stays coherent.
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
_THINKING_LEVEL = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _truncate_obj(result: Any) -> Any:
    """Gemini wants a JSON object as the function response; keep it bounded."""
    if isinstance(result, dict):
        s = json.dumps(result, default=str)
        if len(s) <= MAX_TOOL_RESULT_CHARS:
            return json.loads(s)
        return {"truncated": True, "text": s[:MAX_TOOL_RESULT_CHARS]}
    if isinstance(result, list):
        s = json.dumps(result, default=str)
        return {
            "items": json.loads(s) if len(s) <= MAX_TOOL_RESULT_CHARS else s[:MAX_TOOL_RESULT_CHARS]
        }
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return {"text": text[:MAX_TOOL_RESULT_CHARS]}


def _summary(result: Any) -> str:
    if isinstance(result, str):
        return result[:160].replace("\n", " ")
    if isinstance(result, dict):
        keys = [
            k
            for k in ("ok", "status", "row_count", "matches", "id", "version", "rows")
            if k in result
        ]
        return ", ".join(f"{k}={result[k]}" for k in keys[:4]) or f"{len(result)} keys"
    if isinstance(result, list):
        return f"{len(result)} items"
    return str(result)[:160]


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        from google import genai

        if not api_key:
            raise LLMError("No Google (Gemini) API key. Add one in Settings → AI providers.")
        self.client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------ models
    async def list_models(self) -> list[ModelInfo]:
        try:
            pager = await self.client.aio.models.list(config={"page_size": 100})
        except Exception as exc:
            raise LLMError(
                f"Gemini: could not list models ({exc.__class__.__name__}: {exc})"
            ) from exc
        out: list[ModelInfo] = []
        async for m in pager:
            name = (m.name or "").removeprefix("models/")
            actions = set(getattr(m, "supported_actions", None) or [])
            if not name.startswith("gemini") or (actions and "generateContent" not in actions):
                continue
            out.append(ModelInfo(id=name, display_name=m.display_name or name))
        return out

    # ------------------------------------------------------------------ turn
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
        from google.genai import errors, types

        by_name = {t.name: t for t in tools}
        decls = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.input_schema
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
            thinking_config=types.ThinkingConfig(
                include_thoughts=False, thinking_level=_THINKING_LEVEL.get(effort, "high")
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents: list[types.Content] = [types.Content.model_validate(c) for c in history]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))

        total = Usage()
        text_parts: list[str] = []
        calls = 0
        stop_reason = "end_turn"
        try:
            for _ in range(max_iterations):
                model_parts: list[types.Part] = []
                fn_calls: list[types.FunctionCall] = []
                stream = await self.client.aio.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )
                last_usage = None
                async for chunk in stream:
                    if chunk.usage_metadata:
                        last_usage = chunk.usage_metadata
                    cand = (chunk.candidates or [None])[0]
                    if not cand or not cand.content or not cand.content.parts:
                        continue
                    for part in cand.content.parts:
                        if part.thought:
                            if part.text:
                                await emit(on_event, {"t": "thinking_delta", "text": part.text})
                            if part.thought_signature:
                                model_parts.append(part)  # keep signatures for continuity
                            continue
                        if part.function_call:
                            fn_calls.append(part.function_call)
                            model_parts.append(part)
                        elif part.text:
                            text_parts.append(part.text)
                            model_parts.append(part)
                            await emit(on_event, {"t": "text_delta", "text": part.text})
                    if cand.finish_reason:
                        fr = str(
                            cand.finish_reason.value
                            if hasattr(cand.finish_reason, "value")
                            else cand.finish_reason
                        )
                        if fr not in ("STOP", "FinishReason.STOP"):
                            stop_reason = fr.lower()
                # accounting
                if last_usage:
                    cu = Usage(
                        input_tokens=int(last_usage.prompt_token_count or 0),
                        output_tokens=int(last_usage.candidates_token_count or 0)
                        + int(last_usage.thoughts_token_count or 0),
                        cache_read_tokens=int(last_usage.cached_content_token_count or 0),
                        calls=1,
                    )
                    cu.cost_usd = cost_usd(
                        "gemini",
                        model,
                        input_tokens=cu.input_tokens,
                        output_tokens=cu.output_tokens,
                        cache_read_tokens=cu.cache_read_tokens,
                    )
                    total.add(cu)
                    await emit(on_event, {"t": "usage", **total.to_dict()})
                    budget.charge(cu.cost_usd)
                if model_parts:
                    contents.append(types.Content(role="model", parts=model_parts))
                if not fn_calls:
                    break
                # execute tools, feed responses back
                resp_parts: list[types.Part] = []
                for fc in fn_calls:
                    calls += 1
                    args = dict(fc.args or {})
                    await emit(
                        on_event,
                        {"t": "tool_call", "id": fc.id or fc.name, "name": fc.name, "input": args},
                    )
                    spec = by_name.get(fc.name or "")
                    if spec is None:
                        payload: Any = {"error": f"unknown tool {fc.name}"}
                        await emit(
                            on_event,
                            {
                                "t": "tool_result",
                                "name": fc.name,
                                "ok": False,
                                "summary": "unknown tool",
                            },
                        )
                    else:
                        try:
                            result = await spec.fn(**args)
                            payload = _truncate_obj(result)
                            await emit(
                                on_event,
                                {
                                    "t": "tool_result",
                                    "name": fc.name,
                                    "ok": True,
                                    "summary": _summary(result),
                                },
                            )
                        except ToolError as exc:
                            payload = {"error": str(exc)}
                            await emit(
                                on_event,
                                {
                                    "t": "tool_result",
                                    "name": fc.name,
                                    "ok": False,
                                    "summary": str(exc)[:200],
                                },
                            )
                        except Exception as exc:
                            log.exception("tool %s failed", fc.name)
                            payload = {"error": f"{exc.__class__.__name__}: {exc}"}
                            await emit(
                                on_event,
                                {
                                    "t": "tool_result",
                                    "name": fc.name,
                                    "ok": False,
                                    "summary": repr(exc)[:200],
                                },
                            )
                    resp_parts.append(
                        types.Part.from_function_response(name=fc.name or "", response=payload)
                    )
                contents.append(types.Content(role="user", parts=resp_parts))
            else:
                stop_reason = "max_iterations"
        except BudgetExceeded as exc:
            await emit(on_event, {"t": "error", "message": str(exc)})
            stop_reason = "budget"
        except errors.APIError as exc:
            msg = _api_error(exc)
            await emit(on_event, {"t": "error", "message": msg})
            raise LLMError(msg) from exc

        text = "".join(text_parts)
        await emit(on_event, {"t": "done", "text": text, "stop_reason": stop_reason})
        return TurnResult(
            text=text,
            history=[c.model_dump(mode="json", exclude_none=True) for c in contents],
            usage=total,
            stop_reason=stop_reason,
            tool_calls=calls,
        )

    # ------------------------------------------------------------------ one-shot JSON
    async def extract_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> tuple[Any, Usage]:
        from google.genai import errors, types

        try:
            resp = await self.client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
        except errors.APIError as exc:
            raise LLMError(_api_error(exc)) from exc
        um = resp.usage_metadata
        usage = Usage(
            input_tokens=int(um.prompt_token_count or 0) if um else 0,
            output_tokens=int(um.candidates_token_count or 0) if um else 0,
            calls=1,
        )
        usage.cost_usd = cost_usd(
            "gemini", model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )
        budget.charge(usage.cost_usd)
        try:
            return json.loads(resp.text or ""), usage
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned non-JSON: {(resp.text or '')[:200]}") from exc


def _api_error(exc: Any) -> str:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = getattr(exc, "message", None) or str(exc)
    hint = {
        400: "bad request (model/schema) — try another model",
        401: "invalid API key — check Settings → AI providers",
        403: "key lacks permission",
        404: "model not found — pick another in Settings",
        429: "rate limited / quota exhausted",
        503: "Gemini overloaded — retry shortly",
    }.get(code or 0, "")
    return f"Gemini API error {code}: {msg}" + (f" ({hint})" if hint else "")
