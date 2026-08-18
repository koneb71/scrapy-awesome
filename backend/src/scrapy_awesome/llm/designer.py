"""The in-app designer: a chat whose assistant drives the same `Tools` the MCP server exposes.

`ChatManager` (one per server) owns turns: it builds the system prompt, streams provider events
onto the bus (`chat:<id>` topic → WebSocket), executes tools through a loopback `ServerClient`
(so the UI reflects every change live), enforces the session budget and persists the transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from scrapy_awesome.config import Paths, UserSettings
from scrapy_awesome.llm.base import Budget, LLMError, LLMProvider, ToolSpec, Usage
from scrapy_awesome.llm.registry import make_provider
from scrapy_awesome.store import ChatRow, Store
from scrapy_awesome.tools.client import ServerClient
from scrapy_awesome.tools.core import Tools
from scrapy_awesome.tools.schemas import in_app_tool_specs

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the recipe designer inside scrapy-awesome, a local-first web scraper. The person talks to you in the app; everything you do through tools shows up live in their UI (recipe editor, preview table, runs).

What you build: a *recipe* — seeds, a list container (or page-level fields), fields with selectors (css / xpath / json_path, optional attr / regex), optional detail-page following, pagination, limits. Crawls are deterministic Scrapy runs and cost no tokens; tokens are spent only while designing against cached sample pages, so be economical: fetch once, read the outline, test a few selectors, save, validate.

How to work:
1. If no page is cached yet, fetch_page(seed_url). Read `analysis` (containers, field guesses, pagination, detail link, embedded JSON) and the `outline`.
2. Confirm selectors cheaply: test_selector with `container` for per-item fields (aim for ≥0.9 fill on required fields); search_page when you know a value but not its element; list_json_blobs when the data lives in embedded JSON (prefer json_path then). Prefer semantic selectors (`.price`, `[itemprop=name]`, `h3 a`) over positional ones (`:nth-child`).
3. save_recipe(...) — create, or update the current recipe id (new version). Keep the field list to what the person asked for. Then validate_recipe(id): read fill rates, issues, pagination/detail proof; fix and repeat until `ok`.
4. Report briefly: which fields, fill rates, anything you could not find, and what you propose next (run a small trial: start_run(id, max_pages=2)). Do NOT start a full crawl or export unless the person asked for it in this conversation.
5. When a selector is ambiguous, ask the person to click it: request_pick("click the price of the first product", field_name="price"). One click beats three wrong guesses.

Rules: never invent values or selectors — every field must be confirmed by a tool; respect the person's scope (site, fields, limits) and the app's politeness defaults; for login-gated pages, tell them to add a login session in the app (Sessions → Log in once) instead of asking for credentials; keep replies short and concrete (the UI shows the tables — don't paste large outputs).

Recipe JSON keys: name, seeds[], intent, fetch{tier: auto|http|browser|interactive, session}, list{container}, detail{enabled, link{css}}, pagination{kind: none|next_link|url_template|load_more|infinite_scroll|xhr_json, selector, url_template, max_pages}, fields[{name (snake_case), type: text|number|price|date|url|image|bool|enum|list|json, scope: list|detail|page, extract{css|xpath|json_path, attr, regex, all}, required, enum[]}], limits{max_pages, max_items, download_delay}. Call recipe_schema() if unsure."""


def _context_block(store: Store, settings: UserSettings, recipe_id: str | None) -> str:
    """Dynamic context prepended to the user's message (keeps the system prompt cacheable)."""
    lines = [
        f"[context] robots.txt: {'respected' if settings.crawl.obey_robots else 'ignored'}; "
        f"default delay {settings.crawl.default_download_delay}s."
    ]
    if recipe_id:
        row = store.get_recipe_row(recipe_id)
        if row:
            rec = dict(row.data)
            lines.append(
                f"[context] current recipe id={recipe_id} v{row.version}: "
                + json.dumps(rec, separators=(",", ":"))[:6000]
            )
        samples = store.list_samples(recipe_id=recipe_id, limit=6)
        if samples:
            lines.append(
                "[context] cached pages for this recipe: "
                + "; ".join(f"{s.id} ({s.kind}, {s.tier}) {s.final_url or s.url}" for s in samples)
            )
        else:
            lines.append("[context] no cached pages yet — call fetch_page on the seed first.")
    return "\n".join(lines)


def _title_from(text: str) -> str:
    t = " ".join(text.split())
    return (t[:60] + "…") if len(t) > 60 else t or "New chat"


class ChatManager:
    def __init__(
        self,
        *,
        store: Store,
        bus: Any,
        paths: Paths,
        settings: UserSettings,
        base_url: str,
        token: str,
        provider_factory: Any = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.paths = paths
        self.settings = settings
        self.base_url = base_url
        self.token = token
        self._provider_factory = provider_factory or (lambda name: make_provider(name, self.paths))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._live: dict[str, dict[str, Any]] = {}  # chat_id → partial assistant message
        self._client: ServerClient | None = None

    # ------------------------------------------------------------------ helpers
    def _tools(self) -> list[ToolSpec]:
        if self._client is None or self._client.base_url != self.base_url.rstrip("/"):
            self._client = ServerClient(self.base_url, self.token, timeout=600.0)
        return in_app_tool_specs(Tools(self._client))

    def _publish(self, chat_id: str, ev: dict[str, Any]) -> None:
        self.bus.publish(f"chat:{chat_id}", {"chat_id": chat_id, **ev})

    def is_running(self, chat_id: str) -> bool:
        t = self._tasks.get(chat_id)
        return t is not None and not t.done()

    def live_snapshot(self, chat_id: str) -> dict[str, Any] | None:
        """Partial assistant state of a running turn (replayed to late-joining WebSockets)."""
        if not self.is_running(chat_id):
            return None
        a = self._live.get(chat_id)
        if not a:
            return {"t": "snapshot", "chat_id": chat_id, "content": "", "tool_calls": []}
        return {
            "t": "snapshot",
            "chat_id": chat_id,
            "content": a.get("content", ""),
            "tool_calls": list(a.get("tool_calls", [])),
            "usage": a.get("usage", {}),
        }

    # ------------------------------------------------------------------ lifecycle
    def create(
        self,
        *,
        recipe_id: str | None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        title: str = "",
    ) -> ChatRow:
        role = self.settings.llm.designer
        row = ChatRow(
            id=uuid.uuid4().hex[:12],
            recipe_id=recipe_id,
            provider=provider or role.provider,
            model=model or role.model,
            effort=effort or role.effort,
            title=title,
        )
        return self.store.create_chat(row)

    async def send(self, chat_id: str, content: str) -> ChatRow:
        row = self.store.get_chat(chat_id)
        if not row:
            raise KeyError(chat_id)
        if self.is_running(chat_id):
            raise RuntimeError("a turn is already running for this chat")
        # provider is constructed up front so a missing key fails fast (HTTP 4xx), not mid-stream
        provider = self._provider_factory(row.provider)
        messages = list(row.messages or [])
        messages.append({"role": "user", "content": content, "ts": time.time()})
        row = self.store.update_chat(
            chat_id,
            messages=messages,
            status="running",
            error=None,
            title=row.title or _title_from(content),
        )  # type: ignore[assignment]
        self._tasks[chat_id] = asyncio.create_task(self._run_turn(row, provider, content))
        return row

    async def cancel(self, chat_id: str) -> bool:
        t = self._tasks.get(chat_id)
        if t and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
            return True
        return False

    async def shutdown(self) -> None:
        for cid in list(self._tasks):
            await self.cancel(cid)

    # ------------------------------------------------------------------ the turn
    async def _run_turn(self, row: ChatRow, provider: LLMProvider, content: str) -> None:
        chat_id = row.id
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "usage": {},
            "stop_reason": None,
            "ts": time.time(),
        }
        u0 = row.usage or {}
        prior = Usage(
            input_tokens=int(u0.get("input_tokens", 0)),
            output_tokens=int(u0.get("output_tokens", 0)),
            cache_read_tokens=int(u0.get("cache_read_tokens", 0)),
            cache_write_tokens=int(u0.get("cache_write_tokens", 0)),
            cost_usd=float(u0.get("cost_usd", 0.0)),
            calls=int(u0.get("calls", 0)),
        )
        budget = Budget(limit_usd=self.settings.llm.session_budget_usd, spent_usd=prior.cost_usd)
        pending_calls: dict[str, list[dict[str, Any]]] = {}
        self._live[chat_id] = assistant

        def on_event(ev: dict[str, Any]) -> None:
            t = ev.get("t")
            if t == "text_delta":
                assistant["content"] += ev.get("text", "")
            elif t == "tool_call":
                call = {"name": ev["name"], "input": ev.get("input"), "ok": None, "summary": ""}
                assistant["tool_calls"].append(call)
                pending_calls.setdefault(ev["name"], []).append(call)
            elif t == "tool_result":
                q = pending_calls.get(ev["name"]) or []
                call = q.pop(0) if q else None
                if call is None:  # result without a seen call (Gemini emits both; Anthropic too)
                    call = {"name": ev["name"], "input": None, "ok": None, "summary": ""}
                    assistant["tool_calls"].append(call)
                call["ok"] = ev.get("ok")
                call["summary"] = ev.get("summary", "")
            elif t == "usage":
                assistant["usage"] = {k: v for k, v in ev.items() if k != "t"}
            self._publish(chat_id, ev)

        self._publish(chat_id, {"t": "turn_start"})
        stop = "end_turn"
        error: str | None = None
        history = list(row.history or [])
        try:
            user_message = (
                _context_block(self.store, self.settings, row.recipe_id) + "\n\n" + content
            )
            result = await provider.run_turn(
                model=row.model,
                system=SYSTEM_PROMPT,
                history=history,
                user_message=user_message,
                tools=self._tools(),
                effort=row.effort,  # type: ignore[arg-type]
                budget=budget,
                on_event=on_event,
            )
            history = result.history
            stop = result.stop_reason
            prior.add(result.usage)
            assistant["content"] = result.text or assistant["content"]
        except asyncio.CancelledError:
            stop = "cancelled"
            assistant["content"] += "\n\n[cancelled]"
            self._publish(chat_id, {"t": "error", "message": "cancelled"})
        except LLMError as exc:
            error = str(exc)
            stop = "error"
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("chat turn failed")
            error = f"{exc.__class__.__name__}: {exc}"
            stop = "error"
            self._publish(chat_id, {"t": "error", "message": error})
        assistant["stop_reason"] = stop
        assistant["usage"] = prior.to_dict()
        fresh = self.store.get_chat(chat_id)
        messages = list((fresh.messages if fresh else row.messages) or [])
        messages.append(assistant)
        self.store.update_chat(
            chat_id,
            messages=messages,
            history=history,
            usage=prior.to_dict(),
            status="error" if error else "idle",
            error=error,
        )
        self._live.pop(chat_id, None)
        self._publish(
            chat_id,
            {"t": "turn_end", "stop_reason": stop, "usage": prior.to_dict(), "error": error},
        )
        self._tasks.pop(chat_id, None)


def chat_out(row: ChatRow, running: bool = False) -> dict[str, Any]:
    from scrapy_awesome.store.models import iso

    return {
        "id": row.id,
        "recipe_id": row.recipe_id,
        "provider": row.provider,
        "model": row.model,
        "effort": row.effort,
        "title": row.title,
        "status": "running" if running else row.status,
        "messages": row.messages or [],
        "usage": row.usage or {},
        "error": row.error,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }
