"""RecipeSpider — the generic interpreter that executes any Recipe — and SnapshotSpider, which fetches
sample pages through the exact same engine stack for design-time analysis/validation.

Both spiders emit events through `self.emit(kind, **data)` (see events.py) and never print to stdout.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import scrapy
from scrapy import Request
from scrapy.http import Response

from scrapy_awesome.crawl.events import Emitter, make_sink
from scrapy_awesome.extract.engine import (
    extract_list_items,
    extract_page_fields,
    next_page_url,
    select_containers,
)
from scrapy_awesome.extract.fingerprint import find_heal, heal_field
from scrapy_awesome.fetch.policy import META_KEY, FetchPolicy
from scrapy_awesome.recipe.io import load_recipe
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot.jsonblobs import extract_json_blobs

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _uses_json(recipe: Recipe) -> bool:
    if recipe.list_ and recipe.list_.container.startswith("json:"):
        return True
    return any(e.source == "json_path" for f in recipe.fields for e in (f.extract, *f.alternates))


class _BaseSpider(scrapy.Spider):
    """Shared plumbing: emitter, policies, blocked handling."""

    custom_settings: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str,
        recipe_path: str | None = None,
        tier: str | None = None,
        storage_state: str | None = None,
        headless: bool | str = True,
        events_url: str | None = None,
        events_token: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.recipe: Recipe | None = load_recipe(recipe_path) if recipe_path else None
        self.tier_override = tier or None
        self.storage_state = storage_state
        self.headless = str(headless).lower() not in ("0", "false", "no")
        sink = make_sink(
            events_file=self.run_dir / "events.jsonl", events_url=events_url, token=events_token
        )
        self.emit = Emitter(sink, run_id)
        self._closed_emitted = False

    def policy(self, *, for_detail: bool = False) -> FetchPolicy:
        assert self.recipe is not None
        return FetchPolicy.from_recipe(
            self.recipe,
            for_detail=for_detail,
            tier_override=self.tier_override,
            storage_state_path=self.storage_state,
            headless=self.headless,
        )

    def _blocked_final(self, response: Response) -> bool:
        sa = response.meta.get(META_KEY) or {}
        v = sa.get("verdict") or {}
        if sa.get("final") and (v.get("blocked") or v.get("needs_js")):
            self.emit(
                "page",
                url=response.url,
                status=response.status,
                tier=sa.get("tier"),
                ok=False,
                reason=v.get("reason"),
                detail=v.get("detail"),
                kind=response.meta.get("sa_kind"),
            )
            return True
        if response.status >= 400:
            self.emit(
                "page",
                url=response.url,
                status=response.status,
                tier=sa.get("tier"),
                ok=False,
                reason="http_error",
                kind=response.meta.get("sa_kind"),
            )
            return True
        return False

    def errback(self, failure: Any) -> None:
        req = getattr(failure, "request", None)
        self.emit(
            "page",
            url=getattr(req, "url", None),
            ok=False,
            reason="exception",
            detail=repr(failure.value)[:300],
            kind=req.meta.get("sa_kind") if req is not None else None,
        )

    def closed(self, reason: str) -> None:
        stats = self.crawler.stats.get_stats() or {}
        summary = {
            "run_id": self.run_id,
            "reason": reason,
            "finished_at": _now(),
            "items": int(stats.get("sa/items_written", stats.get("item_scraped_count", 0))),
            "duplicates": int(stats.get("sa/items_duplicate", 0)),
            "pages": int(stats.get("response_received_count", 0)),
            "requests": int(stats.get("downloader/request_count", 0)),
            "errors": int(stats.get("log_count/ERROR", 0)),
            "blocked": {
                k.split("/", 2)[2]: v for k, v in stats.items() if k.startswith("sa/blocked/")
            },
            "escalations": {
                k.split("/", 2)[2]: v for k, v in stats.items() if k.startswith("sa/escalations/")
            },
            "tiers": {
                k.split("/")[2]: v
                for k, v in stats.items()
                if k.startswith("sa/tier/") and k.endswith("/responses")
            },
            "tier_memory": stats.get("sa/tier_memory", {}),
            "elapsed_seconds": stats.get("elapsed_time_seconds"),
        }
        (self.run_dir / "stats.json").write_text(json.dumps(summary, indent=2, default=str))
        if not self._closed_emitted:
            self._closed_emitted = True
            self.emit("done", **summary)
        self.emit.close()


class RecipeSpider(_BaseSpider):
    name = "recipe"

    def __init__(
        self,
        *,
        recipe_path: str,
        max_pages: int | str | None = None,
        max_items: int | str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(recipe_path=recipe_path, **kwargs)
        assert self.recipe is not None
        self.max_pages = int(max_pages) if max_pages else self.recipe.limits.max_pages
        self.max_items = int(max_items) if max_items else self.recipe.limits.max_items
        self.max_detail = self.recipe.limits.max_detail_pages
        self.allowed_domains = self.recipe.domains()
        self.uses_json = _uses_json(self.recipe)
        self.pages_scheduled = 0
        self.detail_scheduled = 0
        self._seen_keys: set[str] = set()
        self._heal_tried: set[str] = set()  # field names we already tried to relocate
        self._failed_saved = 0
        self.max_failed_pages = 50

    # ---- self-heal ------------------------------------------------------------------------
    HEAL_MIN_ITEMS = 3
    HEAL_FILL_COLLAPSE = 0.25

    def _maybe_heal(self, html: str, url: str, items: list[Any], blobs: Any) -> list[Any] | None:
        """If a fingerprinted list field stopped filling on this page, try to relocate it inside
        the item container. On success the recipe (in memory) is patched for the rest of the run
        and the page is re-extracted; a `healed` event carries old → new."""
        assert self.recipe is not None
        fps = self.recipe.fingerprints or {}
        if not fps or len(items) < self.HEAL_MIN_ITEMS or self.recipe.list_ is None:
            return None
        collapsed = []
        for f in self.recipe.list_fields:
            if f.scope != "list" or f.name not in fps or f.name in self._heal_tried:
                continue
            if f.extract.source not in ("css", "xpath"):
                continue
            filled = sum(1 for it in items if it.values.get(f.name) not in (None, "", []))
            if filled / len(items) <= self.HEAL_FILL_COLLAPSE:
                collapsed.append(f)
        if not collapsed:
            return None
        from parsel import Selector

        sel = Selector(text=html, base_url=url)
        nodes, _ = select_containers(
            sel, self.recipe.list_.container, self.recipe.list_.alternates, blobs
        )
        if not nodes:
            return None
        healed_any = False
        for f in collapsed:
            self._heal_tried.add(f.name)
            cand = find_heal(nodes, f, fps[f.name])
            if cand is None:
                self.emit("heal_failed", field=f.name, url=url, selector=f.extract.selector)
                self.crawler.stats.inc_value(f"sa/heal_failed/{f.name}")
                continue
            new_field = heal_field(f, cand.selector, cand.attr)
            self.recipe.fields = [new_field if x.name == f.name else x for x in self.recipe.fields]
            healed_any = True
            self.emit(
                "healed",
                field=f.name,
                url=url,
                old={"css": f.extract.css, "xpath": f.extract.xpath, "attr": f.extract.attr},
                new={"css": cand.selector, "attr": cand.attr},
                score=cand.score,
                fill=cand.fill,
                examples=cand.examples,
            )
            self.crawler.stats.inc_value(f"sa/healed/{f.name}")
        if not healed_any:
            return None
        items2, _ = extract_list_items(self.recipe, html, url, json_blobs=blobs)
        return items2

    def _save_failed_page(
        self, response: Response, kind: str, reason: str, base_row: dict | None
    ) -> None:
        """Keep the HTML of a page we could not extract, for the LLM/agent fallback."""
        if self._failed_saved >= self.max_failed_pages:
            return
        self._failed_saved += 1
        d = self.run_dir / "failed"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self._failed_saved:04d}.html"
        path.write_text(response.text, encoding="utf-8")
        self.emit(
            "page_failed",
            url=response.url,
            kind=kind,
            reason=reason,
            path=str(path),
            base_row=base_row,
            tier=(response.meta.get(META_KEY) or {}).get("tier"),
        )
        self.crawler.stats.inc_value("sa/pages_failed")

    # ---- start ----------------------------------------------------------------------------
    async def start(self) -> AsyncIterator[Request]:
        assert self.recipe is not None
        self.emit(
            "started",
            recipe_id=self.recipe.id,
            recipe_name=self.recipe.name,
            seeds=self.recipe.seeds,
            max_pages=self.max_pages,
            max_items=self.max_items,
            tier=self.tier_override or self.recipe.fetch.tier,
        )
        for seed in self.recipe.seeds:
            req = self._list_request(seed, page_no=1, seed=seed)
            if req:
                yield req

    def _list_request(self, url: str, *, page_no: int, seed: str) -> Request | None:
        if self.pages_scheduled >= self.max_pages:
            return None
        self.pages_scheduled += 1
        pol = self.policy()
        meta = pol.to_meta(pol.initial_tier(), extra={"sa_kind": "list"})
        return Request(
            url,
            callback=self.parse_list,
            errback=self.errback,
            meta=meta,
            cb_kwargs={"page_no": page_no, "seed": seed},
            dont_filter=True,
        )

    # ---- list pages -----------------------------------------------------------------------
    def parse_list(self, response: Response, page_no: int, seed: str):
        assert self.recipe is not None
        if self._blocked_final(response):
            return
        html = response.text
        blobs = extract_json_blobs(html) if self.uses_json else None
        items, which = extract_list_items(self.recipe, html, response.url, json_blobs=blobs)
        healed = self._maybe_heal(html, response.url, items, blobs)
        if healed is not None:
            items = healed
        sa = response.meta.get(META_KEY) or {}
        tier = sa.get("tier")
        self.emit(
            "page",
            url=response.url,
            status=response.status,
            tier=tier,
            ok=True,
            kind="list",
            page_no=page_no,
            items=len(items),
            container=which,
        )
        if not items and self.recipe.page_type != "single":
            self._save_failed_page(
                response, "list", "no_items" if which == "missing" else "empty_items", None
            )
        if items:
            fill = {
                f.name: round(
                    sum(1 for it in items if it.values.get(f.name) not in (None, "", []))
                    / len(items),
                    3,
                )
                for f in self.recipe.list_fields
            }
            self.emit("fill", url=response.url, page_no=page_no, rates=fill)

        new_keys = 0
        for it in items:
            row: dict[str, Any] = dict(it.values)
            row["_url"] = it.detail_url or f"{response.url}#item-{it.index}"
            row["_page_url"] = response.url
            row["_fetched_at"] = _now()
            row["_tier"] = tier
            row["_provenance"] = it.provenance
            key = json.dumps([row.get(k) for k in self.recipe.dedupe_key], default=str)
            if key not in self._seen_keys:
                self._seen_keys.add(key)
                new_keys += 1
            if self.recipe.detail.enabled and it.detail_url:
                if self.max_detail is not None and self.detail_scheduled >= self.max_detail:
                    yield row
                    continue
                self.detail_scheduled += 1
                dpol = self.policy(for_detail=True)
                yield Request(
                    it.detail_url,
                    callback=self.parse_detail,
                    errback=self.errback,
                    meta=dpol.to_meta(dpol.initial_tier(), extra={"sa_kind": "detail"}),
                    cb_kwargs={"row": row},
                    priority=10,
                )
            else:
                yield row

        # ---- pagination -------------------------------------------------------------------
        pg = self.recipe.pagination
        if pg.stop_when_no_new_items and items and new_keys == 0:
            self.emit(
                "log", level="info", msg=f"no new items on {response.url}; stopping pagination"
            )
            return
        if pg.kind == "next_link":
            nxt = next_page_url(self.recipe, html, response.url)
            if nxt:
                req = self._list_request(nxt, page_no=page_no + 1, seed=seed)
                if req:
                    yield req
        elif pg.kind == "url_template" and items and pg.url_template:
            n = pg.start + page_no * pg.step
            req = self._list_request(pg.url_template.format(page=n), page_no=page_no + 1, seed=seed)
            if req:
                yield req
        # none / load_more / infinite_scroll: interactive actions already expanded the page

    # ---- detail pages ---------------------------------------------------------------------
    def parse_detail(self, response: Response, row: dict[str, Any]):
        assert self.recipe is not None
        if self._blocked_final(response):
            row["_provenance"] = {**row.get("_provenance", {}), "_detail": "blocked"}
            yield row
            return
        html = response.text
        blobs = extract_json_blobs(html) if self.uses_json else None
        it = extract_page_fields(self.recipe, html, response.url, scope="detail", json_blobs=blobs)
        row.update(it.values)
        row["_provenance"] = {**row.get("_provenance", {}), **it.provenance}
        row["_url"] = response.url
        if self.recipe.detail_fields and not any(
            v not in (None, "", []) for v in it.values.values()
        ):
            self._save_failed_page(response, "detail", "detail_empty", dict(row))
        self.emit(
            "page",
            url=response.url,
            status=response.status,
            tier=(response.meta.get(META_KEY) or {}).get("tier"),
            ok=True,
            kind="detail",
            filled=[k for k, v in it.values.items() if v not in (None, "", [])],
        )
        yield row


class SnapshotSpider(_BaseSpider):
    """Fetch N URLs through the tiered engines and dump {html, status, tier, blobs} JSON files."""

    name = "snapshot"

    def __init__(self, *, urls: str, kind: str = "list", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.urls: list[str] = json.loads(urls) if urls.strip().startswith("[") else [urls]
        self.kind = kind
        self.out_dir = self.run_dir / "snapshots"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> AsyncIterator[Request]:
        if self.recipe is not None:
            pol = self.policy(for_detail=(self.kind == "detail"))
        else:
            from scrapy_awesome.recipe.models import FetchConfig

            pol = FetchPolicy.from_config(
                FetchConfig(), tier_override=self.tier_override, headless=self.headless
            )
        for i, url in enumerate(self.urls):
            yield Request(
                url,
                callback=self.parse,
                errback=self.errback,
                meta=pol.to_meta(pol.initial_tier(), extra={"sa_kind": self.kind}),
                cb_kwargs={"index": i},
                dont_filter=True,
            )

    def parse(self, response: Response, index: int):
        sa = response.meta.get(META_KEY) or {}
        try:
            html = response.text
        except Exception:
            html = ""
        blobs = extract_json_blobs(html) if html else {}
        rec = {
            "index": index,
            "url": response.request.url if response.request else response.url,
            "final_url": response.url,
            "status": response.status,
            "tier": sa.get("tier"),
            "verdict": sa.get("verdict"),
            "kind": self.kind,
            "fetched_at": _now(),
            "headers": {
                k.decode(errors="replace"): v[0].decode(errors="replace")
                for k, v in response.headers.items()
                if v
            },
            "html": html,
            "blobs": blobs,
        }
        path = self.out_dir / f"{index:03d}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False, default=str))
        self.emit(
            "snapshot",
            index=index,
            url=rec["url"],
            status=response.status,
            tier=rec["tier"],
            path=str(path),
            bytes=len(html),
            blobs=list(blobs.keys()),
            verdict=rec["verdict"],
        )
        return None
