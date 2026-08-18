"""Spike: prove scrapy-stealth (http + browser drivers) and scrapy-playwright (Patchright provider)
coexist in ONE spider under the asyncio reactor, against the local fixture sites.

Run:  cd backend && uv run python spikes/coexist_spider.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # for `tests.fixtures`

from scrapy.utils.reactor import install_reactor  # noqa: E402

install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")

import scrapy  # noqa: E402
from scrapy.crawler import CrawlerProcess  # noqa: E402
from scrapy_playwright.page import PageMethod  # noqa: E402

from tests.fixtures.server import FixtureServer  # noqa: E402

RESULTS: dict[str, dict] = {}

# Scroll to the bottom repeatedly until the document height (or item count) stops growing.
SCROLL_UNTIL_STABLE_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let stable = 0, last = -1, rounds = 0;
  while (rounds < 40 && stable < 3) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(250);
    const h = document.body.scrollHeight;
    if (h === last) stable++; else stable = 0;
    last = h; rounds++;
  }
  return {rounds, height: last};
}
"""


class CoexistSpider(scrapy.Spider):
    name = "coexist"
    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
        "CONCURRENT_REQUESTS": 4,
        "DOWNLOAD_TIMEOUT": 60,
        # engines
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy_stealth.middlewares.StealthDownloaderMiddleware": 950,
        },
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_PROVIDER": "scrapy_awesome.crawl.providers.PatchrightBrowserProvider",
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 30_000,
        # stealth
        "STEALTH_ENABLED": False,
        "STEALTH_DRIVER": "turbo",
        "BROWSER_HEADLESS": True,
        "BROWSER_SETTLE_S": 2.0,
    }

    def __init__(self, base: str, **kw):
        super().__init__(**kw)
        self.base = base

    async def start(self):
        b = self.base
        yield scrapy.Request(
            f"{b}/static/",
            meta={"stealth": {"driver": "turbo"}},
            callback=self.parse_list,
            cb_kwargs={"tag": "static/turbo"},
            dont_filter=True,
        )
        yield scrapy.Request(
            f"{b}/static/?page=2",
            meta={"stealth": {"driver": "basic"}},
            callback=self.parse_list,
            cb_kwargs={"tag": "static/basic"},
            dont_filter=True,
        )
        # blocker with HTTP driver -> expect challenge (403); we escalate ourselves
        yield scrapy.Request(
            f"{b}/blocker/",
            meta={"stealth": {"driver": "turbo"}, "handle_httpstatus_all": True},
            callback=self.parse_blocked_http,
            dont_filter=True,
        )
        # spa via Patchright (interactive tier)
        yield scrapy.Request(
            f"{b}/spa/",
            meta={
                "playwright": True,
                "playwright_page_methods": [PageMethod("wait_for_selector", "article.product_pod")],
            },
            callback=self.parse_list,
            cb_kwargs={"tag": "spa/playwright"},
            dont_filter=True,
        )
        # spa via stealth browser driver (nodriver)
        yield scrapy.Request(
            f"{b}/spa/",
            meta={"stealth": {"driver": "browser", "settle": 1.5}},
            callback=self.parse_list,
            cb_kwargs={"tag": "spa/stealth-browser"},
            dont_filter=True,
        )
        # infinite scroll via Patchright
        yield scrapy.Request(
            f"{b}/infinite/",
            meta={
                "playwright": True,
                "playwright_page_methods": [
                    PageMethod("wait_for_selector", "article.product_pod"),
                    PageMethod("evaluate", SCROLL_UNTIL_STABLE_JS),
                ],
            },
            callback=self.parse_list,
            cb_kwargs={"tag": "infinite/playwright"},
            dont_filter=True,
        )

    def parse_list(self, response, tag: str):
        n = len(response.css("article.product_pod"))
        RESULTS[tag] = {
            "status": response.status,
            "items": n,
            "title": response.css("title::text").get(),
        }
        self.logger.info("RESULT %s -> %s", tag, RESULTS[tag])

    def parse_blocked_http(self, response):
        from scrapy_stealth.detectors.antibot import AntiBotDetector

        blocked = AntiBotDetector().is_blocked(response)
        RESULTS["blocker/turbo"] = {
            "status": response.status,
            "cf_mitigated": response.headers.get("cf-mitigated", b"").decode(),
            "blocked_detected": blocked,
        }
        self.logger.info("RESULT blocker/turbo -> %s", RESULTS["blocker/turbo"])
        if blocked:
            # our escalation: retry with the stealth browser driver, headless
            yield response.request.replace(
                meta={"stealth": {"driver": "browser", "headless": True, "settle": 2.5}},
                callback=self.parse_list,
                cb_kwargs={"tag": "blocker/stealth-browser"},
                dont_filter=True,
            )


def main() -> int:
    t0 = time.time()
    with FixtureServer() as srv:
        process = CrawlerProcess()
        process.crawl(CoexistSpider, base=srv.base_url)
        process.start()
    print("\n=== SPIKE RESULTS ===")
    print(json.dumps(RESULTS, indent=2))
    print(f"elapsed {time.time() - t0:.1f}s")
    expected = {
        "static/turbo": 5,
        "static/basic": 5,
        "spa/playwright": 5,
        "spa/stealth-browser": 5,
        "infinite/playwright": 20,
        "blocker/stealth-browser": 5,
    }
    ok = True
    for tag, n in expected.items():
        got = RESULTS.get(tag, {}).get("items")
        flag = "OK " if got == n else "FAIL"
        if got != n:
            ok = False
        print(f"{flag} {tag}: items={got} (expected {n})")
    b = RESULTS.get("blocker/turbo", {})
    print(("OK " if b.get("blocked_detected") else "FAIL") + f" blocker/turbo detected block: {b}")
    return 0 if ok and b.get("blocked_detected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
