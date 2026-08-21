"""RecipeSpider — the generic interpreter that executes any Recipe — and SnapshotSpider, which fetches
sample pages through the exact same engine stack for design-time analysis/validation.

Both spiders emit events through `self.emit(kind, **data)` (see events.py) and never print to stdout.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import scrapy
from scrapy import Request
from scrapy.http import Response

from scrapy_awesome.crawl import sitemap
from scrapy_awesome.crawl.events import Emitter, make_sink
from scrapy_awesome.extract import jsonpath
from scrapy_awesome.extract.engine import (
    api_blobs,
    extract_list_items,
    extract_page_fields,
    item_url,
    next_page_url,
    select_containers,
)
from scrapy_awesome.extract.fingerprint import find_heal, heal_field
from scrapy_awesome.fetch.policy import META_KEY, FetchPolicy
from scrapy_awesome.recipe.io import load_recipe
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot.jsonblobs import extract_json_blobs
from scrapy_awesome.snapshot.platform import origin_of

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
            "skipped": getattr(self, "skipped_pages", 0),  # unchanged since the last run
            "elapsed_seconds": stats.get("elapsed_time_seconds"),
        }
        out = getattr(self, "_state_out", None)
        if out is not None:
            with contextlib.suppress(OSError):
                out.close()
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
        self._api_fell_back = False
        self._sitemap_seen: set[str] = set()
        self._sitemap_urls_seen = 0
        # Incremental: what the last run knew, and what this one learns.
        self.prior_state: dict[str, dict[str, Any]] = {}
        state_file = self.run_dir / "page_state.json"
        if self.recipe is not None and self.recipe.incremental.enabled and state_file.exists():
            with contextlib.suppress(ValueError, OSError):
                self.prior_state = json.loads(state_file.read_text(encoding="utf-8"))
        self._state_out = (self.run_dir / "page_state.jsonl").open("a", encoding="utf-8")
        self.skipped_pages = 0
        # A page of 250 is politer than 12 pages of 20 — but "give me 100 rows" must not walk a
        # whole catalogue, so max_items becomes a page budget.
        self.api_max_pages = self.max_pages
        api = self.recipe.api
        if api and api.paging.page_size and self.max_items:
            budget = -(-self.max_items // api.paging.page_size)  # ceil
            self.api_max_pages = max(1, min(self.max_pages, budget))

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

        sel = Selector(text=html, base_url=url, type="html")
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
        if self.recipe.api:
            req = self._api_request(page=self.recipe.api.paging.start, page_no=1)
            if req:
                yield req
            return

        src = self.recipe.source
        if src.kind == "urls":
            urls = sitemap.clean_urls(src.urls, limit=src.max_urls)
            # A pasted list is the page budget: 300 URLs is 300 pages, whatever `max_pages` said
            # back when this recipe walked a list page.
            self.max_pages = max(self.max_pages, len(urls))
            self.emit("log", level="info", msg=f"crawling {len(urls)} URLs from the recipe's list")
            for url in urls:
                req = self._list_request(url, page_no=1, seed=url)
                if req:
                    yield req
            return
        if src.kind == "sitemap":
            self.max_pages = max(self.max_pages, src.max_urls)
            start = src.sitemap or sitemap.default_sitemap(self.recipe.seeds[0])
            yield self._sitemap_request(start, depth=0, from_robots=False)
            return

        for seed in self.recipe.seeds:
            req = self._list_request(seed, page_no=1, seed=seed)
            if req:
                yield req

    # ---- incremental ----------------------------------------------------------------------
    def _stale_enough(self, prior: dict[str, Any]) -> bool:
        """Even a page that swears it has not changed gets looked at eventually."""
        assert self.recipe is not None
        days = self.recipe.incremental.refetch_after_days
        if days is None:
            return False
        seen = str(prior.get("fetched_at") or "")
        if not seen:
            return True
        with contextlib.suppress(ValueError):
            when = datetime.fromisoformat(seen.replace("Z", "+00:00"))
            return (datetime.now(UTC) - when).days >= days
        return True

    def _skip_unchanged(self, url: str, lastmod: str) -> bool:
        """A sitemap `lastmod` we have already crawled means the page is not worth a request."""
        assert self.recipe is not None
        if not self.recipe.incremental.enabled or not lastmod:
            return False
        prior = self.prior_state.get(url)
        if not prior or prior.get("lastmod") != lastmod or self._stale_enough(prior):
            return False
        self.skipped_pages += 1
        self._remember(url, lastmod=lastmod, prior=prior)
        return True

    def _conditional_headers(self, url: str) -> dict[str, str]:
        assert self.recipe is not None
        prior = self.prior_state.get(url) if self.recipe.incremental.enabled else None
        if not prior or self._stale_enough(prior):
            return {}
        headers = {}
        if prior.get("etag"):
            headers["If-None-Match"] = str(prior["etag"])
        if prior.get("last_modified"):
            headers["If-Modified-Since"] = str(prior["last_modified"])
        return headers

    def _remember(
        self,
        url: str,
        *,
        response: Response | None = None,
        items: int = 0,
        lastmod: str = "",
        prior: dict[str, Any] | None = None,
    ) -> None:
        """One line per URL; the parent folds them into the store when the run ends."""
        entry = {
            "url": url,
            "lastmod": lastmod or (prior or {}).get("lastmod", ""),
            "etag": (prior or {}).get("etag", ""),
            "last_modified": (prior or {}).get("last_modified", ""),
            "content_hash": (prior or {}).get("content_hash", ""),
            "items": items or int((prior or {}).get("items") or 0),
            "status": 304 if response is None else response.status,
            "run_id": self.run_id,
        }
        if response is not None:
            head = response.headers
            entry["etag"] = (head.get(b"ETag") or b"").decode(errors="replace")
            entry["last_modified"] = (head.get(b"Last-Modified") or b"").decode(errors="replace")
            entry["content_hash"] = hashlib.sha1(response.body or b"").hexdigest()
        with contextlib.suppress(OSError, ValueError):
            self._state_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._state_out.flush()

    def _unchanged(self, response: Response) -> dict[str, Any] | None:
        """304, or a body byte-identical to last time — either way there is nothing new to parse."""
        assert self.recipe is not None
        if not self.recipe.incremental.enabled:
            return None
        url = response.url
        prior = self.prior_state.get(url)
        if prior is None:
            return None
        if response.status == 304:
            return prior
        digest = hashlib.sha1(response.body or b"").hexdigest()
        if prior.get("content_hash") and prior["content_hash"] == digest:
            return prior
        return None

    # ---- sitemaps -------------------------------------------------------------------------
    def _sitemap_request(self, url: str, *, depth: int, from_robots: bool) -> Request:
        pol = self.policy()
        # A sitemap is a file, not a page: never spend a browser on it.
        pol = replace(pol, tier="http")
        return Request(
            url,
            callback=self.parse_sitemap,
            errback=self.sitemap_errback,
            meta=pol.to_meta("http", extra={"sa_kind": "sitemap"}),
            cb_kwargs={"depth": depth, "from_robots": from_robots},
            dont_filter=True,
        )

    def sitemap_errback(self, failure: Any) -> Any:
        """A missing /sitemap.xml is not the end: robots.txt usually says where the real one is."""
        request = getattr(failure, "request", None)
        if request is None or (request.cb_kwargs or {}).get("from_robots"):
            return self.errback(failure)
        origin = origin_of(request.url)
        self.emit("log", level="info", msg=f"{request.url} did not answer; asking robots.txt")
        return self._robots_request(origin)

    def _robots_request(self, origin: str) -> Request:
        pol = replace(self.policy(), tier="http")
        return Request(
            f"{origin}/robots.txt",
            callback=self.parse_robots_for_sitemaps,
            errback=self.errback,
            meta=pol.to_meta("http", extra={"sa_kind": "sitemap"}),
            dont_filter=True,
        )

    def parse_robots_for_sitemaps(self, response: Response):
        origin = origin_of(response.url)
        found = sitemap.sitemaps_in_robots(response.text, origin)
        if not found:
            self.emit("log", level="warning", msg="no sitemap in robots.txt either")
            return
        for url in found[:3]:
            yield self._sitemap_request(url, depth=0, from_robots=True)

    def parse_sitemap(self, response: Response, depth: int, from_robots: bool):
        assert self.recipe is not None
        src = self.recipe.source
        kind, entries = sitemap.parse(response.body, url=response.url)
        if not entries:
            # Block detection needs to see every status, so a 404 arrives here rather than in the
            # errback. Either way the site's own robots.txt is the next place to ask.
            if not from_robots:
                self.emit(
                    "log",
                    level="info",
                    msg=f"{response.url} held no URLs ({response.status}); asking robots.txt",
                )
                yield self._robots_request(origin_of(response.url))
                return
            self.emit("log", level="warning", msg=f"{response.url} held no URLs")
            return
        if kind == "sitemapindex":
            if depth >= 2:  # an index of indexes of indexes is a loop, not a site
                return
            for child in entries:
                if self._sitemap_urls_seen >= src.max_urls:
                    return
                yield self._sitemap_request(child.loc, depth=depth + 1, from_robots=from_robots)
            return

        remaining = max(0, src.max_urls - self._sitemap_urls_seen)
        chosen = sitemap.select(
            entries,
            include=src.include,
            exclude=src.exclude,
            limit=remaining,
            seen=self._sitemap_seen,
        )
        self._sitemap_urls_seen += len(chosen)
        self.emit(
            "page",
            url=response.url,
            status=response.status,
            tier="http",
            ok=True,
            kind="sitemap",
            page_no=1,
            items=len(chosen),
        )
        for entry in chosen:
            if self._skip_unchanged(entry.loc, entry.lastmod):
                continue
            req = self._list_request(entry.loc, page_no=1, seed=entry.loc)
            if req is None:
                return
            if entry.lastmod:
                req.meta["sa_lastmod"] = entry.lastmod
            yield req

    def _list_request(self, url: str, *, page_no: int, seed: str) -> Request | None:
        if self.pages_scheduled >= self.max_pages:
            return None
        self.pages_scheduled += 1
        pol = self.policy()
        meta = pol.to_meta(pol.initial_tier(), extra={"sa_kind": "list"})
        conditional = self._conditional_headers(url)
        if conditional:
            meta.setdefault("sa_headers", {}).update(conditional)
        return Request(
            url,
            callback=self.parse_list,
            errback=self.errback,
            meta=meta,
            cb_kwargs={"page_no": page_no, "seed": seed},
            dont_filter=True,
        )

    # ---- shared row plumbing ---------------------------------------------------------------
    def _row(self, it: Any, response: Response, tier: str | None) -> dict[str, Any]:
        assert self.recipe is not None
        row: dict[str, Any] = dict(it.values)
        row["_url"] = item_url(self.recipe, it, response.url)
        row["_page_url"] = response.url
        row["_fetched_at"] = _now()
        row["_tier"] = tier
        row["_provenance"] = it.provenance
        return row

    def _detail_request(self, url: str, row: dict[str, Any]) -> Request | None:
        assert self.recipe is not None
        if self.max_detail is not None and self.detail_scheduled >= self.max_detail:
            return None
        self.detail_scheduled += 1
        dpol = self.policy(for_detail=True)
        return Request(
            url,
            callback=self.parse_detail,
            errback=self.errback,
            meta=dpol.to_meta(dpol.initial_tier(), extra={"sa_kind": "detail"}),
            cb_kwargs={"row": row},
            priority=10,
        )

    # ---- API pages ------------------------------------------------------------------------
    def _api_request(
        self, *, page: int | None = None, cursor: str | None = None, page_no: int = 1
    ) -> Request | None:
        assert self.recipe is not None and self.recipe.api is not None
        api = self.recipe.api
        if self.pages_scheduled >= self.api_max_pages:
            return None
        self.pages_scheduled += 1
        url, body = api.render(page=page, cursor=cursor)
        pol = self.policy()
        # A JSON endpoint must never escalate to a browser tier: Chrome would hand back the
        # body wrapped in a JSON-viewer document. Pinning the tier also pins `policy_tier`,
        # which is what the escalation middleware keys off.
        if pol.tier in ("auto", "http"):
            pol = replace(pol, tier="http")
        meta = pol.to_meta(pol.tier, extra={"sa_kind": "api"})
        headers = {"Accept": "application/json", **api.headers}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        return Request(
            url,
            method=api.method,
            body=body,
            headers=headers,
            meta=meta,
            callback=self.parse_api,
            errback=self.errback,
            cb_kwargs={"page_no": page_no, "page": page},
            dont_filter=True,
        )

    def _fallback_to_html(self, why: str):
        """The endpoint died mid-run: finish the job with the HTML selectors instead."""
        assert self.recipe is not None
        if self._api_fell_back or (self.recipe.api and self.recipe.api.on_error == "stop"):
            return
        self._api_fell_back = True
        self.crawler.stats.inc_value("sa/api/fallback_to_html")
        self.emit("log", level="warning", msg=f"API mode fell back to HTML selectors: {why}")
        for seed in self.recipe.seeds:
            req = self._list_request(seed, page_no=1, seed=seed)
            if req:
                yield req

    def parse_api(self, response: Response, page_no: int, page: int | None):
        assert self.recipe is not None and self.recipe.api is not None
        api = self.recipe.api
        sa = response.meta.get(META_KEY) or {}
        tier = sa.get("tier")
        ctype = (response.headers.get(b"Content-Type") or b"").decode(errors="replace").lower()
        try:
            doc = json.loads(response.text)
        except (ValueError, AttributeError):
            doc = None
        if doc is None:
            # Status alone lies (404/403/429 all appear as HTML app shells): branch on the body.
            self.emit(
                "page",
                url=response.url,
                status=response.status,
                tier=tier,
                ok=False,
                kind="api",
                reason="not_json",
                detail=f"content-type {ctype or '?'}, {len(response.text or '')} chars",
            )
            self.crawler.stats.inc_value("sa/api/not_json")
            yield from self._fallback_to_html(f"{response.url} returned {response.status} non-JSON")
            return

        if response.status >= 400:
            # Platforms signal "past the last page" differently: Shopify returns an empty array,
            # WordPress a 400 with a JSON error object. An error body is never rows.
            self.emit(
                "page",
                url=response.url,
                status=response.status,
                tier=tier,
                ok=False,
                kind="api",
                page_no=page_no,
                reason="http_error",
                detail=str(doc)[:200],
            )
            if page_no == 1:
                yield from self._fallback_to_html(f"{response.url} returned HTTP {response.status}")
            return

        blobs = api_blobs(doc)
        # The body is JSON, so there is no markup to parse: CSS/XPath alternates simply do not
        # match here — they are what the HTML fallback uses.
        items, which = extract_list_items(self.recipe, "", response.url, json_blobs=blobs)
        self.emit(
            "page",
            url=response.url,
            status=response.status,
            tier=tier,
            ok=True,
            kind="api",
            page_no=page_no,
            items=len(items),
            container=which,
        )
        if not items and page_no == 1:
            # An empty *first* page means the container path is wrong (or the catalogue really is
            # empty) — worth falling back. An empty *later* page is just the end of the walk.
            self.crawler.stats.inc_value("sa/api/empty_container")
            self._save_failed_page(response, "api", "container_missing", None)
            yield from self._fallback_to_html(f"no items at {self.recipe.list_.container!r}")
            return
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
            row = self._row(it, response, tier)
            key = json.dumps([row.get(k) for k in self.recipe.dedupe_key], default=str)
            if key not in self._seen_keys:
                self._seen_keys.add(key)
                new_keys += 1
            if self.recipe.detail.enabled and it.detail_url:
                req = self._detail_request(it.detail_url, row)
                if req is not None:
                    yield req
                    continue
            yield row

        # ---- next page ----------------------------------------------------------------
        paging = api.paging
        if paging.kind == "none":
            return
        if paging.stop_on_empty and not items:
            self.emit("log", level="info", msg=f"API returned no items on page {page_no}; done")
            return
        if paging.kind == "page":
            nxt = self._api_request(
                page=(page if page is not None else paging.start) + paging.step,
                page_no=page_no + 1,
            )
        else:  # cursor
            cursor = jsonpath.first(doc, paging.cursor_path or "")
            has_more = (
                bool(jsonpath.first(doc, paging.has_more_path))
                if paging.has_more_path
                else bool(cursor)
            )
            nxt = (
                self._api_request(cursor=str(cursor), page_no=page_no + 1)
                if has_more and cursor
                else None
            )
        if nxt is not None:
            yield nxt

    # ---- list pages -----------------------------------------------------------------------
    def parse_list(self, response: Response, page_no: int, seed: str):
        assert self.recipe is not None
        if self._blocked_final(response):
            return
        prior = self._unchanged(response)
        if prior is not None:
            self.skipped_pages += 1
            self._remember(response.url, lastmod=response.meta.get("sa_lastmod", ""), prior=prior)
            self.emit(
                "page",
                url=response.url,
                status=response.status,
                tier=(response.meta.get(META_KEY) or {}).get("tier"),
                ok=True,
                kind="unchanged",
                page_no=page_no,
                items=0,
            )
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
        # what this page looked like, so the next run can ask "still the same?"
        self._remember(
            response.url,
            response=response,
            items=len(items),
            lastmod=response.meta.get("sa_lastmod", ""),
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
            row = self._row(it, response, tier)
            key = json.dumps([row.get(k) for k in self.recipe.dedupe_key], default=str)
            if key not in self._seen_keys:
                self._seen_keys.add(key)
                new_keys += 1
            req = (
                self._detail_request(it.detail_url, row)
                if (self.recipe.detail.enabled and it.detail_url)
                else None
            )
            if req is not None:
                yield req
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
        doc = None
        if self.recipe.api is not None:  # API-mode detail pages return JSON documents
            try:
                doc = json.loads(html)
            except (ValueError, AttributeError):
                doc = None
        if doc is not None:
            blobs = api_blobs(doc)
            html = ""
        else:
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

    # A page can fetch hundreds of things; we only want the JSON it renders itself from.
    CAPTURE_MAX = 40
    CAPTURE_BODY_BYTES = 1_000_000
    CAPTURE_SETTLE_MS = 2_500  # how long to keep watching after the page loads

    def __init__(
        self, *, urls: str, kind: str = "list", capture_xhr: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.urls: list[str] = json.loads(urls) if urls.strip().startswith("[") else [urls]
        self.kind = kind
        self.capture_xhr = str(capture_xhr).lower() not in ("0", "false", "no", "")
        self.captures: list[dict[str, Any]] = []
        self.out_dir = self.run_dir / "snapshots"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    async def on_playwright_response(self, response: Any) -> None:
        """Every response the page receives, filtered down to the JSON it could be reading.

        Named in `meta["playwright_page_event_handlers"]` as a *string*, which scrapy-playwright
        resolves against the spider — so the meta stays JSON-serializable and JOBDIR-safe.
        """
        if len(self.captures) >= self.CAPTURE_MAX:
            return
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower() or response.status >= 400:
                return
            body = await response.text()
        except Exception:  # a redirect, a closed page, a body already consumed
            return
        if len(body) > self.CAPTURE_BODY_BYTES:
            return
        self.captures.append(
            {
                "url": response.url,
                "method": getattr(response.request, "method", "GET"),
                "status": response.status,
                "content_type": ctype,
                "bytes": len(body),
                "body": body,  # request headers are deliberately not kept: they carry tokens
            }
        )

    async def start(self) -> AsyncIterator[Request]:
        if self.recipe is not None:
            pol = self.policy(for_detail=(self.kind == "detail"))
        else:
            from scrapy_awesome.recipe.models import FetchConfig

            pol = FetchPolicy.from_config(
                FetchConfig(), tier_override=self.tier_override, headless=self.headless
            )
        extra: dict[str, Any] = {"sa_kind": self.kind}
        if self.capture_xhr:
            # only the interactive tier runs a real browser, so only it can watch the network
            pol = replace(pol, tier="interactive")
            extra["playwright_page_event_handlers"] = {"response": "on_playwright_response"}
            # The page is closed as soon as navigation resolves, and a list fetched by script
            # arrives *after* that — without a wait we watch an empty window and see nothing.
            from scrapy_playwright.page import PageMethod

            extra["playwright_page_methods"] = [
                *pol.page_methods(),
                PageMethod("wait_for_timeout", self.CAPTURE_SETTLE_MS),
            ]
        for i, url in enumerate(self.urls):
            yield Request(
                url,
                callback=self.parse,
                errback=self.errback,
                meta=pol.to_meta(pol.initial_tier(), extra=extra),
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
            "xhr": list(self.captures),
        }
        self.captures.clear()  # each URL keeps only what it fetched
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
            xhr=len(rec["xhr"]),
        )
        return None
