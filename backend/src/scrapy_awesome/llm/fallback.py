"""Per-page LLM fallback + agent hand-off for pages the crawler could not extract.

The worker stays deterministic: when a list page yields no items (container gone) or a detail
page fills nothing, it saves the HTML and reports `page_failed`. Here in the server:

* **LLM fallback** (`recipe.fallback.llm_enabled`, a key for the *fallback* role, budget left):
  the page is turned into bounded markdown and the fallback model extracts the recipe's fields as
  JSON (structured output). Rows are appended to the run with `_provenance = llm` per field and
  streamed to the UI like any other item. Cost is charged against
  `recipe.limits.per_run_llm_budget_usd` and shown in the run stats.
* **Agent hand-off** (MCP / plugin mode): `get_failed_pages` returns the same markdown + field
  spec so the person's own Claude/Gemini can extract, and `submit_rows` appends rows with
  `_provenance = agent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scrapy_awesome.extract.coerce import coerce_one
from scrapy_awesome.llm.base import Budget, LLMError, Usage
from scrapy_awesome.llm.registry import make_provider
from scrapy_awesome.recipe.models import Field, Recipe
from scrapy_awesome.snapshot.markdown import to_markdown
from scrapy_awesome.store import FailedPageRow, Store

log = logging.getLogger(__name__)

MAX_MD_CHARS = 40_000
_JSON_TYPE = {
    "text": "string",
    "url": "string",
    "image": "string",
    "date": "string",
    "enum": "string",
    "number": "number",
    "price": "number",
    "bool": "boolean",
    "list": "array",
    "json": "object",
}

SYSTEM = (
    "You extract structured data from a web page rendered as markdown. Return ONLY the JSON the "
    "schema asks for. Never invent values: use null when a field is not present on the page. "
    "Keep values verbatim (no rewording); numbers as numbers (no currency symbols); dates as ISO "
    "8601 when unambiguous. For list pages return one item per repeated entry, in page order."
)


def field_spec(recipe: Recipe, kind: str) -> list[dict[str, Any]]:
    fields = recipe.list_fields if kind == "list" else recipe.detail_fields
    return [
        {
            "name": f.name,
            "type": f.type,
            "description": f.description or "",
            **({"enum": f.enum} if f.enum else {}),
        }
        for f in fields
        if f.scope in ("list", "page", "detail")
    ]


def _prop(f: Field) -> dict[str, Any]:
    t = _JSON_TYPE.get(f.type, "string")
    prop: dict[str, Any] = {"type": [t, "null"]}
    if t == "array":
        prop["items"] = {"type": "string"}
    if f.enum:
        prop["enum"] = [*f.enum, None]
        prop.pop("type", None)
    if f.description:
        prop["description"] = f.description
    return prop


def json_schema(recipe: Recipe, kind: str) -> dict[str, Any]:
    fields = recipe.list_fields if kind == "list" else recipe.detail_fields
    obj = {
        "type": "object",
        "properties": {f.name: _prop(f) for f in fields},
        "required": [f.name for f in fields],
        "additionalProperties": False,
    }
    if kind == "list":
        return {
            "type": "object",
            "properties": {"items": {"type": "array", "items": obj}},
            "required": ["items"],
            "additionalProperties": False,
        }
    return obj


def page_markdown(html_path: str, url: str) -> str:
    try:
        html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return to_markdown(html, url, fit=False, max_chars=MAX_MD_CHARS)


def _coerce_row(recipe: Recipe, kind: str, values: dict[str, Any]) -> dict[str, Any]:
    fields = {f.name: f for f in (recipe.list_fields if kind == "list" else recipe.detail_fields)}
    out: dict[str, Any] = {}
    for name, f in fields.items():
        v = values.get(name)
        if v in (None, "", []):
            out[name] = f.default
            continue
        try:
            if f.type in ("list", "json"):
                out[name] = v
            else:
                out[name] = coerce_one(
                    v
                    if isinstance(v, str)
                    else json.dumps(v)
                    if isinstance(v, dict | list)
                    else str(v),
                    f.type,
                    f,
                )
        except Exception:
            out[name] = v
    return out


def rows_from_values(
    recipe: Recipe, page: FailedPageRow, values: Any, provenance: str
) -> list[dict[str, Any]]:
    """Turn model/agent output into item rows (with implicit fields + provenance)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if page.kind == "detail":
        base = dict(page.base_row or {})
        vals = _coerce_row(recipe, "detail", values if isinstance(values, dict) else {})
        base.update(vals)
        prov = (
            dict(base.get("_provenance") or {}) if isinstance(base.get("_provenance"), dict) else {}
        )
        for k in vals:
            prov[k] = provenance
        base["_provenance"] = prov
        base.setdefault("_url", page.url)
        base["_fetched_at"] = now
        base.setdefault("_tier", page.tier)
        return [base]
    items = values.get("items") if isinstance(values, dict) else values
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        vals = _coerce_row(recipe, "list", it)
        url_field = next((f.name for f in recipe.list_fields if f.type == "url"), None)
        row = {
            **vals,
            "_url": (vals.get(url_field) if url_field else None)
            or f"{page.url}#{provenance}-{i + 1}",
            "_page_url": page.url,
            "_fetched_at": now,
            "_tier": page.tier,
            "_provenance": {k: provenance for k in vals},
        }
        rows.append(row)
    return rows


class FallbackRunner:
    def __init__(
        self,
        *,
        store: Store,
        bus: Any,
        paths: Any,
        settings: Any,
        manager: Any,
        provider_factory: Any = None,
        concurrency: int = 2,
    ) -> None:
        self.store = store
        self.bus = bus
        self.paths = paths
        self.settings = settings
        self.manager = manager
        self._factory = provider_factory or (lambda name: make_provider(name, self.paths))
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self.concurrency = concurrency

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.manager.on_page_failed.append(self.enqueue)
        self.manager.on_finished.append(self.on_run_finished)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"llm-fallback-{i}")
            for i in range(self.concurrency)
        ]

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.manager.on_page_failed.remove(self.enqueue)
        with contextlib.suppress(ValueError):
            self.manager.on_finished.remove(self.on_run_finished)
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._workers = []

    def enqueue(self, page: FailedPageRow) -> None:
        self._queue.put_nowait(page.id)

    async def on_run_finished(self, run: Any) -> None:
        """AI fields (`extract.llm`) are computed once the crawl is done, if configured."""
        if run is None or run.status != "finished" or not run.recipe_id:
            return
        recipe = self.store.get_recipe(run.recipe_id)
        if not recipe or not recipe.llm_fields:
            return
        from scrapy_awesome.llm.ai_fields import run_ai_fields_for

        try:
            out = await run_ai_fields_for(
                store=self.store,
                bus=self.bus,
                paths=self.paths,
                settings=self.settings,
                run_id=run.id,
                provider_factory=self._factory,
            )
            self.bus.publish(run.id, {"t": "ai_fields", "run_id": run.id, **out})
        except LLMError as exc:
            log.info("AI fields skipped for %s: %s", run.id, exc)
            self.bus.publish(run.id, {"t": "ai_fields", "run_id": run.id, "error": str(exc)})

    async def _worker(self) -> None:
        while True:
            page_id = await self._queue.get()
            try:
                await self.process(page_id)
            except Exception:  # never die
                log.exception("fallback processing failed for %s", page_id)

    # ------------------------------------------------------------------ helpers
    def _recipe_for(self, run_id: str) -> Recipe | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        p = Path(run.run_dir) / "recipe.json"
        if p.exists():
            with contextlib.suppress(Exception):
                return Recipe.model_validate(json.loads(p.read_text()))
        return self.store.get_recipe(run.recipe_id) if run.recipe_id else None

    def _llm_stats(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        st = dict((run.stats or {}) if run else {})
        return dict(st.get("llm") or {"pages": 0, "rows": 0, "cost_usd": 0.0, "skipped": 0})

    def _bump_llm_stats(self, run_id: str, **delta: Any) -> None:
        run = self.store.get_run(run_id)
        if not run:
            return
        st = dict(run.stats or {})
        llm = dict(st.get("llm") or {"pages": 0, "rows": 0, "cost_usd": 0.0, "skipped": 0})
        for k, v in delta.items():
            llm[k] = round(llm.get(k, 0) + v, 6) if isinstance(v, float) else llm.get(k, 0) + v
        st["llm"] = llm
        self.store.update_run(run_id, stats=st)

    def append_rows(self, run_id: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        n0 = self.store.next_item_n(run_id)
        self.store.add_items(run_id, [(n0 + i, r) for i, r in enumerate(rows)])
        run = self.store.get_run(run_id)
        if run:
            self.store.update_run(run_id, items=max(run.items, n0 - 1 + len(rows)))
        for i, r in enumerate(rows):
            self.bus.publish(run_id, {"t": "item", "run_id": run_id, "n": n0 + i, "row": r})
        return len(rows)

    # ------------------------------------------------------------------ the work
    async def process(self, page_id: str) -> FailedPageRow | None:
        page = self.store.get_failed_page(page_id)
        if not page or page.status != "pending":
            return page
        recipe = self._recipe_for(page.run_id)
        if recipe is None:
            return self.store.update_failed_page(page_id, status="skipped", error="recipe missing")
        if not recipe.fallback.llm_enabled:
            return self.store.update_failed_page(
                page_id, status="skipped", error="fallback disabled in recipe"
            )
        role = self.settings.llm.fallback
        try:
            provider = self._factory(role.provider)
        except LLMError as exc:
            return self.store.update_failed_page(page_id, status="skipped", error=str(exc)[:200])
        stats = self._llm_stats(page.run_id)
        limit = float(recipe.limits.per_run_llm_budget_usd or 0)
        spent = float(stats.get("cost_usd", 0.0))
        if limit and spent >= limit:
            self._bump_llm_stats(page.run_id, skipped=1)
            return self.store.update_failed_page(
                page_id, status="skipped", error="run LLM budget exhausted"
            )
        md = page_markdown(page.html_path, page.url)
        if not md.strip():
            return self.store.update_failed_page(page_id, status="failed", error="empty page")
        schema = json_schema(recipe, page.kind)
        spec = "\n".join(
            f"- {f['name']} ({f['type']}){': ' + f['description'] if f['description'] else ''}"
            for f in field_spec(recipe, page.kind)
        )
        prompt = (
            f"Page URL: {page.url}\nPage kind: {page.kind} ({'repeated entries' if page.kind == 'list' else 'one entry'})\n"
            f"Fields to extract:\n{spec}\n\nPage (markdown):\n{md}"
        )
        budget = Budget(limit_usd=(limit - spent) if limit else None)
        t0 = time.monotonic()
        try:
            values, usage = await provider.extract_json(
                model=role.model, system=SYSTEM, prompt=prompt, schema=schema, budget=budget
            )
        except LLMError as exc:
            self._bump_llm_stats(page.run_id, skipped=1)
            return self.store.update_failed_page(
                page_id, status="failed", error=str(exc)[:300], provider=role.provider
            )
        rows = rows_from_values(recipe, page, values, "llm")
        added = self.append_rows(page.run_id, rows)
        self._bump_llm_stats(page.run_id, pages=1, rows=added, cost_usd=float(usage.cost_usd))
        row = self.store.update_failed_page(
            page_id,
            status="recovered" if added else "failed",
            rows_added=added,
            provider=f"{role.provider}/{role.model}",
            cost_usd=float(usage.cost_usd),
            error=None if added else "model returned no rows",
        )
        self.bus.publish(
            page.run_id,
            {
                "t": "fallback",
                "run_id": page.run_id,
                "page_id": page_id,
                "url": page.url,
                "kind": page.kind,
                "rows": added,
                "cost_usd": float(usage.cost_usd),
                "elapsed": round(time.monotonic() - t0, 2),
            },
        )
        return row

    async def process_pending(self, run_id: str) -> dict[str, int]:
        out = {"recovered": 0, "skipped": 0, "failed": 0}
        for page in self.store.list_failed_pages(run_id, status="pending"):
            row = await self.process(page.id)
            if row and row.status in out:
                out[row.status] += 1
        return out


def submit_agent_rows(
    store: Store, bus: Any, runner: FallbackRunner, page: FailedPageRow, rows: list[dict[str, Any]]
) -> tuple[int, list[str]]:
    """Rows extracted by an external agent (MCP) for a failed page → appended with provenance
    `agent`. Unknown field names are dropped and reported."""
    recipe = runner._recipe_for(page.run_id)
    if recipe is None:
        raise KeyError("recipe missing")
    known = {f.name for f in (recipe.list_fields if page.kind == "list" else recipe.detail_fields)}
    dropped: set[str] = set()
    cleaned = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        dropped |= set(r) - known
        cleaned.append({k: v for k, v in r.items() if k in known})
    values: Any = {"items": cleaned} if page.kind == "list" else (cleaned[0] if cleaned else {})
    out = rows_from_values(recipe, page, values, "agent")
    added = runner.append_rows(page.run_id, out)
    store.update_failed_page(
        page.id, status="recovered" if added else "failed", rows_added=added, provider="agent"
    )
    return added, sorted(dropped)


__all__ = [
    "FallbackRunner",
    "Usage",
    "field_spec",
    "json_schema",
    "page_markdown",
    "submit_agent_rows",
]
