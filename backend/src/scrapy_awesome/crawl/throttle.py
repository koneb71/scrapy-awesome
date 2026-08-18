"""ThrottleMiddleware: per-domain delay + concurrency for requests answered by scrapy-stealth.

scrapy-stealth fetches inside `process_request`, which happens *before* Scrapy's downloader slots
apply DOWNLOAD_DELAY / CONCURRENT_REQUESTS_PER_DOMAIN / AutoThrottle — so those settings silently
don't apply to stealth-routed requests. This middleware (priority 930, ahead of stealth at 950)
restores politeness for them: a randomized per-domain delay (0.5×–1.5× like Scrapy), a per-domain
concurrency cap, and exponential backoff when the escalation middleware reports a block.

Playwright-routed requests (no `stealth` dict) are left alone: they go through the normal downloader
slots and get Scrapy's own throttling.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

from scrapy import Request
from scrapy.http import Response

logger = logging.getLogger(__name__)


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _DomainState:
    __slots__ = ("delay", "next_at", "penalty", "sem")

    def __init__(self, delay: float, conc: int) -> None:
        self.delay = delay
        self.next_at = 0.0
        self.penalty = 0
        self.sem = asyncio.Semaphore(max(1, conc))


class ThrottleMiddleware:
    def __init__(self, crawler: Any) -> None:
        s = crawler.settings
        self.crawler = crawler
        self.base_delay = float(s.getfloat("DOWNLOAD_DELAY", 0.0))
        self.randomize = bool(s.getbool("RANDOMIZE_DOWNLOAD_DELAY", True))
        self.conc = int(s.getint("CONCURRENT_REQUESTS_PER_DOMAIN", 8))
        self.max_delay = float(s.getfloat("SA_THROTTLE_MAX_DELAY", 30.0))
        self._domains: dict[str, _DomainState] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_crawler(cls, crawler: Any) -> ThrottleMiddleware:
        mw = cls(crawler)
        crawler.sa_throttle = mw  # escalation middleware reaches us through the crawler
        return mw

    def _state(self, domain: str) -> _DomainState:
        st = self._domains.get(domain)
        if st is None:
            st = self._domains[domain] = _DomainState(self.base_delay, self.conc)
        return st

    # ---- public: called by EscalationMiddleware ------------------------------------------------
    def penalize(self, domain: str) -> float:
        """Exponential backoff after a block; returns the new delay."""
        st = self._state(domain)
        st.penalty = min(st.penalty + 1, 6)
        st.delay = min(max(self.base_delay, 0.5) * (2**st.penalty), self.max_delay)
        st.next_at = max(st.next_at, time.monotonic() + st.delay)
        self.crawler.stats.set_value(f"sa/throttle/{domain}/delay", round(st.delay, 2))
        return st.delay

    def reward(self, domain: str) -> None:
        st = self._state(domain)
        if st.penalty > 0:
            st.penalty -= 1
            st.delay = (
                max(self.base_delay, max(self.base_delay, 0.5) * (2**st.penalty))
                if st.penalty
                else self.base_delay
            )
            self.crawler.stats.set_value(f"sa/throttle/{domain}/delay", round(st.delay, 2))

    # ---- middleware --------------------------------------------------------------------------
    async def process_request(self, request: Request) -> None:
        if not isinstance(request.meta.get("stealth"), dict):
            return None  # playwright / default handler → Scrapy's own slots throttle it
        domain = _domain(request.url)
        st = self._state(domain)
        await st.sem.acquire()
        request.meta["_sa_throttle_domain"] = domain
        # reserve our slot in time
        async with self._lock:
            now = time.monotonic()
            delay = st.delay
            if self.randomize and delay:
                delay *= random.uniform(0.5, 1.5)
            wait = max(0.0, st.next_at - now)
            st.next_at = max(now, st.next_at) + delay
        if wait > 0:
            await asyncio.sleep(wait)
        return None

    def _release(self, request: Request) -> None:
        domain = request.meta.pop("_sa_throttle_domain", None)
        if domain:
            st = self._domains.get(domain)
            if st:
                st.sem.release()

    def process_response(self, request: Request, response: Response) -> Response:
        self._release(request)
        return response

    def process_exception(self, request: Request, exception: BaseException) -> None:
        self._release(request)
        return None
