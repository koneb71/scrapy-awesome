"""The agent tool set — one implementation, three front doors (stdio MCP for Claude Code /
Claude Desktop / Gemini CLI, and the in-app Claude / Gemini designers).

Every tool is an `async` method on `Tools`; the docstring is the description an LLM sees, so it
says *when* to use the tool and what comes back. All state lives in the local server (SQLite +
run dirs), so whatever an agent builds shows up live in the UI and vice versa.

Design-time workflow the tools are shaped around:

    fetch_page → (page_outline | search_page | test_selector | list_json_blobs)* → save_recipe
    → validate_recipe (fix & repeat) → start_run → run_status/get_rows → export_run
    request_pick / get_pick when a human click beats guessing.
"""

import asyncio
import time
from typing import Any

from scrapy_awesome.extract.jsonpath import resolve as jsonpath_resolve
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot.jsonblobs import summarize_blobs
from scrapy_awesome.tools.client import ServerClient, ToolError

FINISHED = {"finished", "stopped", "failed", "cancelled"}


def _analysis_summary(a: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim the heuristic analysis to what an agent needs to decide next."""
    if not a:
        return None
    return {
        "page_type": a.get("page_type"),
        "title": a.get("title"),
        "containers": [
            {"selector": c["selector"], "count": c["count"], "sample": (c.get("sample") or [])[:2]}
            for c in (a.get("containers") or [])[:3]
        ],
        "fields": [
            {
                "name": f["name"],
                "type": f["type"],
                "selector": f["selector"],
                "attr": f.get("attr"),
                "examples": (f.get("examples") or [])[:2],
                "fill": f.get("fill"),
            }
            for f in (a.get("fields") or [])[:12]
        ],
        "detail_link": a.get("detail_link"),
        "pagination": (a.get("pagination") or [])[:3],
        "json_list_paths": (a.get("json_list_paths") or [])[:5],
        "login_hint": a.get("login_hint"),
        "notes": a.get("notes"),
    }


class Tools:
    def __init__(self, client: ServerClient) -> None:
        self.c = client

    # ------------------------------------------------------------------ app / status
    async def app_status(self) -> dict[str, Any]:
        """Health of the local scrapy-awesome app: URL, version, active runs, data dir, and the
        doctor checks (browsers, keys). Call first if anything seems off, or to tell the user
        where the UI is."""
        h = await self.c.get("/health")
        s = await self.c.get("/api/settings")
        doctor = await self.c.get("/api/settings/doctor")
        return {
            "url": self.c.base_url,
            "version": h.get("version"),
            "active_runs": h.get("active_runs"),
            "data_dir": s.get("data_dir"),
            "doctor": [
                {"name": d["name"], "status": d["status"], "detail": d["detail"]}
                for d in doctor
                if d["status"] != "ok"
            ]
            or "all checks ok",
        }

    async def open_ui(self, route: str = "/") -> dict[str, Any]:
        """Open the scrapy-awesome UI in the user's browser at a route, e.g. `/recipes/<id>` to
        review a recipe, `/runs/<id>` to watch a crawl live. Use it whenever the person should
        look at something (preview table, run progress) instead of describing it."""
        return await self.c.post("/api/ui/open", {"route": route})

    # ------------------------------------------------------------------ pages
    async def fetch_page(
        self,
        url: str,
        tier: str | None = None,
        kind: str = "list",
        recipe_id: str | None = None,
        outline_chars: int = 12_000,
    ) -> dict[str, Any]:
        """Fetch a page through the real crawl engine (auto-escalating http → browser →
        interactive; robots.txt respected) and cache it as a *page* for design work. Returns the
        page id, final URL, tier used, a heuristic analysis (list containers, field guesses,
        pagination, detail-link, embedded JSON lists) and a compact DOM outline. `kind` is
        `list` (a listing/search page) or `detail` (one item's page). Set `tier` to force
        `http|browser|interactive` (e.g. `browser` for JS-rendered sites)."""
        rows = await self.c.post(
            "/api/pages/snapshot",
            {"urls": [url], "kind": kind, "tier": tier, "recipe_id": recipe_id},
        )
        row = rows[0]
        outline = await self.c.get_text(f"/api/pages/{row['id']}/outline", max_chars=outline_chars)
        return {
            "page_id": row["id"],
            "url": row["url"],
            "final_url": row["final_url"],
            "status": row["status"],
            "tier": row["tier"],
            "title": row["title"],
            "blocked": (row.get("verdict") or {}).get("blocked", False),
            "verdict": row.get("verdict"),
            "json_blobs": row.get("blobs") or [],
            "analysis": _analysis_summary(row.get("analysis")),
            "outline": outline,
        }

    async def page_outline(
        self, page_id: str, max_chars: int = 12_000, keep_siblings: int = 2, text_limit: int = 80
    ) -> str:
        """Compact DOM outline of a cached page: scripts/styles removed, long text truncated,
        runs of similar siblings collapsed to `+N more`. Read selectors straight off it
        (`tag`, `#id`, `.class`, `[attr]`). Raise `keep_siblings`/`max_chars` to see more."""
        return await self.c.get_text(
            f"/api/pages/{page_id}/outline",
            max_chars=max_chars,
            keep_siblings=keep_siblings,
            text_limit=text_limit,
        )

    async def page_markdown(self, page_id: str, fit: bool = True, max_chars: int = 20_000) -> str:
        """Readable markdown of a cached page — main content only when `fit` (default), the whole
        body otherwise. Good for understanding what the page is about; use `page_outline` for
        selectors."""
        return await self.c.get_text(f"/api/pages/{page_id}/markdown", fit=fit, max_chars=max_chars)

    async def search_page(
        self, page_id: str, text: str, container: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Where does this text appear on the cached page? Returns matching elements with a CSS
        path and — inside the list container — the selector *relative to each item* plus how many
        items it fills. The fastest way from "I see £11.50" to a field selector."""
        return await self.c.post(
            f"/api/pages/{page_id}/search", {"text": text, "container": container, "limit": limit}
        )

    async def test_selector(
        self,
        page_id: str,
        selector: str,
        attr: str | None = None,
        regex: str | None = None,
        container: str | None = None,
    ) -> dict[str, Any]:
        """Run a CSS or XPath selector against a cached page. Without `container`: match count,
        first values, HTML snippets. With `container` (the list item selector): how many items
        the selector fills and example values per item — exactly how the crawler will apply a
        field. `attr` reads an attribute (`href`, `src`, …), `regex` extracts a group."""
        return await self.c.post(
            f"/api/pages/{page_id}/selector",
            {"selector": selector, "attr": attr, "regex": regex, "container": container},
        )

    async def list_json_blobs(self, page_id: str, path: str | None = None) -> dict[str, Any]:
        """Embedded JSON on the page (`__NEXT_DATA__`, `application/ld+json`, inline state).
        Without `path`: a shape summary of every blob (keys, types, array lengths). With
        `path` (`json:<blob>.<jsonpath>` or `<blob>` + `$.a.b[*].c`): the actual values —
        prefer JSON fields over brittle DOM selectors whenever a list lives here."""
        blobs = await self.c.get(f"/api/pages/{page_id}/blobs")
        if not blobs:
            return {"blobs": {}, "note": "no embedded JSON blobs found on this page"}
        if path:
            p = path[5:] if path.startswith("json:") else path
            name, _, jp = p.partition(".")
            if name not in blobs:
                raise ToolError(f"unknown blob {name!r}; have {list(blobs)}")
            values = jsonpath_resolve(blobs[name], jp or "$")
            return {"path": path, "count": len(values), "values": values[:20]}
        return {"blobs": summarize_blobs(blobs)}

    # ------------------------------------------------------------------ recipes
    async def recipe_schema(self) -> dict[str, Any]:
        """JSON schema of a recipe (the portable scrape definition). Fields have `name`,
        `type` (text|number|price|date|url|image|bool|enum|list|json), `scope` (list|detail)
        and one extractor (`css` / `xpath` / `json_path`), optional `attr`/`regex`. Read this
        once if unsure about a key."""
        return Recipe.model_json_schema()

    async def list_recipes(self) -> list[dict[str, Any]]:
        """Saved recipes (id, name, version, seed URL, field names)."""
        rows = await self.c.get("/api/recipes")
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "version": r["version"],
                "seeds": r["recipe"].get("seeds"),
                "fields": [f["name"] for f in r["recipe"].get("fields", [])],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        """Full recipe JSON by id (plus version and readiness errors)."""
        r = await self.c.get(f"/api/recipes/{recipe_id}")
        rec = Recipe.model_validate(r["recipe"])
        return {
            "id": r["id"],
            "version": r["version"],
            "ready": rec.ready,
            "readiness_errors": rec.readiness_errors(),
            "recipe": r["recipe"],
        }

    async def save_recipe(
        self, recipe: dict[str, Any], recipe_id: str | None = None, note: str = ""
    ) -> dict[str, Any]:
        """Validate and save a recipe (create, or update `recipe_id` → new version). Returns the
        id/version, readiness (a recipe needs seeds, a list container or page-level fields, and
        ≥1 field to run) and which changes would break resuming an older run. Keep `name`
        human-friendly; put the user's request in `intent`."""
        v = await self.c.post("/api/recipes/validate", recipe)
        if not v.get("ok"):
            raise ToolError(
                "recipe invalid: " + "; ".join(f"{e['loc']}: {e['msg']}" for e in v["errors"])
            )
        if recipe_id:
            r = (
                await self.c.request(
                    "PUT", f"/api/recipes/{recipe_id}", json=recipe, params={"note": note}
                )
            ).json()
        else:
            r = await self.c.post("/api/recipes", recipe)
        rec = Recipe.model_validate(r["recipe"])
        return {
            "id": r["id"],
            "version": r["version"],
            "ready": rec.ready,
            "readiness_errors": rec.readiness_errors(),
            "incompatible_with_resume": r.get("incompatible_with_resume", []),
            "ui": f"{self.c.base_url}/recipes/{r['id']}",
        }

    async def validate_recipe(
        self, recipe_id: str, fetch_samples: bool = True, max_rows: int = 20
    ) -> dict[str, Any]:
        """Preview a saved recipe the way the crawler will run it: (optionally) fetch the standard
        sample set — page 1, page 2 via pagination, two detail pages — then extract in-process
        and report per-field fill rate, distinct values, issues (empty fields, positional
        selectors, missing next link) and the first rows. Iterate until it passes, then run."""
        r = await self.c.get(f"/api/recipes/{recipe_id}")
        recipe = r["recipe"]
        if fetch_samples:
            await self.c.post("/api/preview/samples", {"recipe": recipe})
        out = await self.c.post("/api/preview", {"recipe": recipe, "max_rows": max_rows})
        rep = out["report"]
        rows = rep.get("rows") or []
        return {
            "ok": rep.get("ok"),
            "row_count": len(rows),
            "containers": rep.get("containers"),
            "fields": rep.get("fields"),
            "issues": rep.get("issues"),
            "pagination": rep.get("pagination"),
            "detail": rep.get("detail"),
            "samples": [
                {"id": s["id"], "kind": s["kind"], "url": s["final_url"], "tier": s["tier"]}
                for s in out.get("samples", [])
            ],
            "rows": rows[:max_rows],
            "ui": f"{self.c.base_url}/recipes/{recipe_id}",
        }

    # ------------------------------------------------------------------ human in the loop
    async def request_pick(
        self,
        prompt: str,
        kind: str = "field",
        recipe_id: str | None = None,
        page_id: str | None = None,
        field_name: str | None = None,
        hint: str | None = None,
        wait_seconds: int = 120,
    ) -> dict[str, Any]:
        """Ask the person to click an element in the app's visual picker (e.g. "click the price
        of the first product"). Opens the UI, waits up to `wait_seconds`, and returns the picked
        selector (relative to the list item when `kind=field`), examples and match count. If it
        is still `pending`, poll `get_pick`. Use this instead of guessing when selectors are
        ambiguous — one click from the user beats three wrong tries."""
        pick = await self.c.post(
            "/api/picks",
            {
                "prompt": prompt,
                "kind": kind,
                "recipe_id": recipe_id,
                "sample_id": page_id,
                "field_name": field_name,
                "hint": hint,
            },
        )
        await self.c.post("/api/ui/open", {"route": f"/pick/{pick['id']}"})
        return await self.get_pick(pick["id"], wait_seconds=wait_seconds)

    async def get_pick(self, pick_id: str, wait_seconds: int = 25) -> dict[str, Any]:
        """Poll a pick request (long-poll up to `wait_seconds`). Status is `pending`, `answered`
        (see `answer.selector` / `answer.relative_selector`, `answer.examples`) or `cancelled`."""
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            remaining = deadline - time.monotonic()
            p = await self.c.get(f"/api/picks/{pick_id}", wait=min(25, max(0, remaining)))
            if p["status"] != "pending" or remaining <= 0:
                return p

    # ------------------------------------------------------------------ runs
    async def start_run(
        self,
        recipe_id: str,
        max_pages: int | None = None,
        max_items: int | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Start crawling a saved recipe in the background (deterministic, zero tokens). Returns
        the run id; then `run_status` / `get_rows` / `export_run`. Override limits for a quick
        trial (`max_pages=2`). Tell the user they can watch it at the returned `ui` URL."""
        r = await self.c.post(
            "/api/runs",
            {"recipe_id": recipe_id, "max_pages": max_pages, "max_items": max_items, "tier": tier},
        )
        return {**_run_view(r), "ui": f"{self.c.base_url}/runs/{r['id']}"}

    async def run_status(self, run_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Status and counters of a run (items, pages, blocked, escalations, tier mix). With
        `wait_seconds` it waits (polling) until the run finishes or the time is up — handy for
        short runs; for long ones, return to the user and check later."""
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            r = await self.c.get(f"/api/runs/{run_id}")
            if r["status"] in FINISHED or time.monotonic() >= deadline:
                return _run_view(r)
            await asyncio.sleep(2)

    async def get_rows(self, run_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Scraped rows of a run (paged). Rows carry the recipe fields plus `_url`,
        `_page_url`, `_tier`, `_fetched_at`."""
        r = await self.c.get(f"/api/runs/{run_id}/items", offset=offset, limit=min(limit, 500))
        return {"total": r["total"], "offset": offset, "rows": r["items"]}

    async def stop_run(self, run_id: str) -> dict[str, Any]:
        """Gracefully stop a run (finishes in-flight pages; resumable later)."""
        return await self.c.post(f"/api/runs/{run_id}/stop")

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Kill a run immediately."""
        return await self.c.post(f"/api/runs/{run_id}/cancel")

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        """Resume a stopped/failed run from its checkpoint (same recipe; field selector changes
        are fine, seed/pagination changes need a new run)."""
        r = await self.c.post(f"/api/runs/{run_id}/resume")
        return _run_view(r)

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent runs (id, recipe, status, items, pages)."""
        rows = await self.c.get("/api/runs")
        return [_run_view(r) for r in rows[:limit]]

    async def get_failed_pages(
        self, run_id: str, limit: int = 5, include_markdown: bool = True
    ) -> dict[str, Any]:
        """Pages of a run the crawler fetched but could not extract (list page with no items,
        detail page with nothing filled). Returns each page's markdown and the recipe's field
        spec so *you* can extract the rows and hand them back with `submit_rows`. Use it when a
        run finished with fewer rows than expected or the person asks to recover missing pages."""
        listing = await self.c.get(f"/api/runs/{run_id}/failed", status="pending")
        pages = listing["pages"][:limit]
        out = []
        for p in pages:
            d = await self.c.get(f"/api/runs/{run_id}/failed/{p['id']}", markdown=include_markdown)
            out.append(
                {
                    "page_id": d["id"],
                    "url": d["url"],
                    "kind": d["kind"],
                    "reason": d["reason"],
                    "fields": d.get("fields"),
                    "base_row": d.get("base_row"),
                    "markdown": d.get("markdown"),
                }
            )
        return {"counts": listing["counts"], "pages": out}

    async def submit_rows(
        self, run_id: str, page_id: str, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Hand back rows you extracted for a failed page (see `get_failed_pages`). For a `list`
        page pass one object per entry; for a `detail` page pass a single object with the detail
        fields (it is merged into the item's row). Only recipe field names are kept; values are
        coerced to the field types; rows get provenance `agent`."""
        return await self.c.post(f"/api/runs/{run_id}/failed/{page_id}/rows", {"rows": rows})

    async def export_run(
        self, run_id: str, format: str = "csv", dest: str | None = None
    ) -> dict[str, Any]:
        """Export a run's rows as `json`, `jsonl`, `csv` or `xlsx`. Writes into the run
        directory, or to `dest` (absolute path or `~/…`, file or directory). Returns the path —
        give it to the user."""
        return await self.c.post(
            f"/api/runs/{run_id}/export", {"fmt": format, "dest": dest, "include_meta": True}
        )


def _run_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "recipe_id": r.get("recipe_id"),
        "recipe_name": r.get("recipe_name"),
        "status": r["status"],
        "reason": r.get("reason"),
        "items": r.get("items"),
        "pages": r.get("pages"),
        "blocked": r.get("blocked"),
        "escalations": r.get("escalations"),
        "tiers": (r.get("stats") or {}).get("tiers"),
        "error": r.get("error"),
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
    }


TOOL_NAMES: tuple[str, ...] = (
    "app_status",
    "open_ui",
    "fetch_page",
    "page_outline",
    "page_markdown",
    "search_page",
    "test_selector",
    "list_json_blobs",
    "recipe_schema",
    "list_recipes",
    "get_recipe",
    "save_recipe",
    "validate_recipe",
    "request_pick",
    "get_pick",
    "start_run",
    "run_status",
    "get_rows",
    "stop_run",
    "cancel_run",
    "resume_run",
    "list_runs",
    "get_failed_pages",
    "submit_rows",
    "export_run",
)
