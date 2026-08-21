"""EscalationMiddleware: http → browser → interactive on blocks / JS-only pages, with per-domain memory.

Sits at priority 930 — before the throttle (940) and scrapy-stealth (950), whose `process_request`
returns a Response and short-circuits later `process_request` methods. Requests carry
`meta["sa"] = {tier, attempt, policy_tier, policy}` (stamped by `FetchPolicy.to_meta`).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from scrapy import Request, Spider, signals
from scrapy.http import Response

from scrapy_awesome.extract.engine import count_matches
from scrapy_awesome.fetch.blocks import classify_response
from scrapy_awesome.fetch.policy import META_KEY, TIER_ORDER, FetchPolicy, next_tier

logger = logging.getLogger(__name__)

_FETCH_META_KEYS = (
    "stealth",
    "playwright",
    "playwright_page_methods",
    "playwright_context",
    "playwright_context_kwargs",
    "playwright_abort_static",
    "playwright_include_page",
    "download_timeout",
    "handle_httpstatus_all",
    META_KEY,
)


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _rebuild(request: Request, tier: str, attempt: int) -> Request:
    sa = request.meta[META_KEY]
    policy = FetchPolicy.from_dict(sa["policy"])
    keep = {k: v for k, v in request.meta.items() if k not in _FETCH_META_KEYS}
    keep.update(policy.to_meta(tier, attempt=attempt))  # type: ignore[arg-type]
    return request.replace(meta=keep, dont_filter=True)


class EscalationMiddleware:
    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler
        self.stats = crawler.stats
        self.tier_memory: dict[str, str] = {}
        seed = crawler.settings.getdict("SA_TIER_MEMORY", {})
        self.tier_memory.update({k: v for k, v in seed.items() if v in TIER_ORDER})

    @classmethod
    def from_crawler(cls, crawler: Any) -> EscalationMiddleware:
        mw = cls(crawler)
        crawler.signals.connect(mw.spider_closed, signal=signals.spider_closed)
        return mw

    def spider_closed(self, spider: Spider) -> None:
        if self.tier_memory:
            self.stats.set_value("sa/tier_memory", dict(self.tier_memory))

    # ---- requests -----------------------------------------------------------------------------
    def process_request(self, request: Request) -> Request | None:
        # `sa_headers` carries the recipe's own headers and, on an incremental run, the
        # If-None-Match / If-Modified-Since that let a server answer "nothing new".
        for key, value in (request.meta.get("sa_headers") or {}).items():
            if not request.headers.get(key):
                request.headers[key] = value
        sa = request.meta.get(META_KEY)
        if not isinstance(sa, dict) or sa.get("attempt", 0) > 0 or sa.get("policy_tier") != "auto":
            return None
        remembered = self.tier_memory.get(_domain(request.url))
        if remembered and TIER_ORDER.index(remembered) > TIER_ORDER.index(sa["tier"]):
            logger.debug("tier memory: %s starts at %s", request.url, remembered)
            self.stats.inc_value("sa/tier_memory_hits")
            return _rebuild(request, remembered, attempt=0)
        return None

    # ---- responses ----------------------------------------------------------------------------
    def process_response(self, request: Request, response: Response) -> Any:
        sa = request.meta.get(META_KEY)
        if not isinstance(sa, dict):
            return response
        if response.status == 304:
            # "Nothing has changed" — an empty body by design. Block detection reads an empty body
            # as a wall and would escalate to a browser, turning the cheapest possible answer into
            # the most expensive one.
            self.stats.inc_value("sa/unchanged")
            return response
        spider = self.crawler.spider
        tier: str = sa["tier"]
        attempt: int = sa.get("attempt", 0)
        domain = _domain(request.url)

        try:
            body_text = response.text
        except Exception:
            body_text = ""
        headers = {
            k.decode(errors="replace"): v[0].decode(errors="replace")
            for k, v in response.headers.items()
            if v
        }

        matched: int | None = None
        expected = _expected_selector(spider, request)
        if expected and body_text:
            matched = count_matches(body_text, expected, response.url)

        verdict = classify_response(
            response.status, headers, body_text, expected_selector_matched=matched
        )
        sa["verdict"] = {
            "blocked": verdict.blocked,
            "needs_js": verdict.needs_js,
            "reason": verdict.reason,
            "detail": verdict.detail,
        }
        sa["matched"] = matched
        self.stats.inc_value(f"sa/tier/{tier}/responses")
        logger.debug(
            "response %s tier=%s attempt=%s status=%s verdict=%s matched=%s",
            request.url,
            tier,
            attempt,
            response.status,
            sa["verdict"],
            matched,
        )

        throttle = getattr(self.crawler, "sa_throttle", None)
        if not verdict.escalate:
            if attempt > 0 or (self.tier_memory.get(domain) != tier and tier != "http"):
                self.tier_memory[domain] = tier
                self.stats.set_value("sa/tier_memory", dict(self.tier_memory))
            if throttle is not None:
                throttle.reward(domain)
            return response

        self.stats.inc_value(f"sa/blocked/{verdict.reason}")
        if throttle is not None and verdict.blocked:
            new_delay = throttle.penalize(domain)
            logger.info("backoff for %s: delay now %.1fs", domain, new_delay)
        nxt = next_tier(tier) if sa.get("policy_tier") == "auto" else None  # type: ignore[arg-type]
        _emit(
            spider,
            "blocked",
            url=request.url,
            status=response.status,
            tier=tier,
            reason=verdict.reason,
            detail=verdict.detail,
            escalated_to=nxt,
        )
        if nxt:
            logger.info(
                "escalating %s: %s → %s (%s: %s)",
                request.url,
                tier,
                nxt,
                verdict.reason,
                verdict.detail,
            )
            self.stats.inc_value(f"sa/escalations/{tier}->{nxt}")
            return _rebuild(request, nxt, attempt=attempt + 1)

        sa["final"] = True
        return response


def _expected_selector(spider: Spider, request: Request) -> str | None:
    """List container selector for list pages (so 0 matches on a script-heavy page ⇒ needs JS)."""
    if request.meta.get("sa_kind") != "list":
        return None
    recipe = getattr(spider, "recipe", None)
    if recipe is None or recipe.list_ is None:
        return None
    c = recipe.list_.container
    return None if c.startswith("json:") else c


def _emit(spider: Spider, kind: str, **data: Any) -> None:
    fn = getattr(spider, "emit", None)
    if callable(fn):
        try:
            fn(kind, **data)
        except Exception:  # pragma: no cover
            logger.debug("emit failed", exc_info=True)
