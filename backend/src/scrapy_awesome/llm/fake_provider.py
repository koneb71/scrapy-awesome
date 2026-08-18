"""A deterministic, offline "designer" for development and end-to-end tests (`SA_FAKE_LLM=1`).

It is *not* an LLM: it walks the same tool path a real model would — fetch the seed page, build a
recipe from the heuristic analysis, save it, validate it — and streams a short report. That lets
the whole in-app chat pipeline (events, tool chips, live recipe refresh, budgets, persistence) be
exercised and tested without an API key. Never enabled unless the env var is set.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from scrapy_awesome.llm.base import Budget, ModelInfo, OnEvent, ToolSpec, TurnResult, Usage, emit
from scrapy_awesome.tools.client import ToolError

_URL = re.compile(r"https?://[^\s\"'<>]+")


class FakeDesignerProvider:
    name = "fake"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake-designer", display_name="Fake designer (offline)")]

    async def _say(self, on_event: OnEvent, text: str, buf: list[str]) -> None:
        for chunk in re.split(r"(?<=[ .,;])", text):
            if not chunk:
                continue
            buf.append(chunk)
            await emit(on_event, {"t": "text_delta", "text": chunk})
            await asyncio.sleep(self.delay)

    async def _call(
        self, on_event: OnEvent, tools: dict[str, ToolSpec], name: str, **kw: Any
    ) -> Any:
        await emit(on_event, {"t": "tool_call", "id": name, "name": name, "input": kw})
        try:
            out = await tools[name].fn(**kw)
        except ToolError as exc:
            await emit(
                on_event, {"t": "tool_result", "name": name, "ok": False, "summary": str(exc)[:200]}
            )
            raise
        summary = (
            f"ok={out.get('ok')} rows={out.get('row_count')}"
            if isinstance(out, dict) and "row_count" in out
            else f"{len(out)} keys"
            if isinstance(out, dict)
            else str(out)[:80]
        )
        await emit(on_event, {"t": "tool_result", "name": name, "ok": True, "summary": summary})
        return out

    async def run_turn(
        self,
        *,
        model: str,
        system: str,
        history: list[Any],
        user_message: str,
        tools: list[ToolSpec],
        effort: str,
        budget: Budget,
        on_event: OnEvent,
        max_iterations: int = 40,
    ) -> TurnResult:
        by_name = {t.name: t for t in tools}
        buf: list[str] = []
        usage = Usage(input_tokens=1200, output_tokens=200, cost_usd=0.0, calls=1)
        text = user_message.split("\n\n", 1)[-1]  # strip the [context] block
        ctx = user_message
        recipe_id = None
        m = re.search(r"current recipe id=(\S+) v(\d+): (\{.*?\})(?:\n|$)", ctx, re.S)
        current: dict[str, Any] | None = None
        if m:
            recipe_id = m.group(1)
            try:
                current = json.loads(m.group(3))
            except json.JSONDecodeError:
                current = None
        url = None
        if current and current.get("seeds"):
            url = current["seeds"][0]
        else:
            u = _URL.search(text)
            url = u.group(0) if u else None
        try:
            if not url:
                await self._say(
                    on_event, "Give me a URL to start from (or open a recipe first).", buf
                )
            else:
                await self._say(on_event, "Fetching the page and reading its structure… ", buf)
                page = await self._call(
                    on_event, by_name, "fetch_page", url=url, kind="list", recipe_id=recipe_id
                )
                a = page.get("analysis") or {}
                containers = a.get("containers") or []
                fields = a.get("fields") or []
                if not containers:
                    await self._say(
                        on_event,
                        "I could not find a repeating list on this page; add fields manually or point me at a listing page.",
                        buf,
                    )
                else:
                    container = containers[0]["selector"]
                    recipe = dict(current or {})
                    recipe.update(
                        {
                            "name": recipe.get("name") or (page.get("title") or "Recipe")[:60],
                            "seeds": [url],
                            "intent": text[:500],
                            "list": {"container": container},
                            "fields": [
                                {
                                    "name": f["name"],
                                    "type": f.get("type", "text"),
                                    "extract": {
                                        "css": f["selector"],
                                        **({"attr": f["attr"]} if f.get("attr") else {}),
                                    },
                                }
                                for f in fields[:8]
                            ]
                            or [{"name": "text", "extract": {"css": container}}],
                        }
                    )
                    dl = a.get("detail_link")
                    if dl and dl.get("selector"):
                        recipe["detail"] = {"enabled": True, "link": {"css": dl["selector"]}}
                    pag = (a.get("pagination") or [None])[0]
                    if pag and pag.get("kind") == "next_link" and pag.get("selector"):
                        recipe["pagination"] = {
                            "kind": "next_link",
                            "selector": pag["selector"],
                            "max_pages": 5,
                        }
                    await self._say(
                        on_event,
                        f"Using container `{container}` with {len(recipe['fields'])} fields. Saving… ",
                        buf,
                    )
                    saved = await self._call(
                        on_event, by_name, "save_recipe", recipe=recipe, recipe_id=recipe_id
                    )
                    recipe_id = saved["id"]
                    await self._say(on_event, "Validating on sample pages… ", buf)
                    rep = await self._call(
                        on_event, by_name, "validate_recipe", recipe_id=recipe_id
                    )
                    fills = ", ".join(
                        f"{k} {int(v['fill_rate'] * 100)}%"
                        for k, v in (rep.get("fields") or {}).items()
                    )
                    verdict = "passes" if rep.get("ok") else "has issues"
                    await self._say(
                        on_event,
                        f"Done — v{saved['version']} {verdict}: {rep.get('row_count')} rows; {fills}. Review the Fields/Preview tabs; say “run a trial” to crawl 2 pages.",
                        buf,
                    )
        except ToolError as exc:
            await self._say(on_event, f"A tool failed: {exc}", buf)
        budget.charge(0.0)
        await emit(on_event, {"t": "usage", **usage.to_dict()})
        out = "".join(buf)
        await emit(on_event, {"t": "done", "text": out, "stop_reason": "end_turn"})
        return TurnResult(
            text=out,
            history=[
                *history,
                {"role": "user", "content": text},
                {"role": "assistant", "content": out},
            ],
            usage=usage,
        )

    async def extract_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> tuple[Any, Usage]:
        return {}, Usage(calls=1)
