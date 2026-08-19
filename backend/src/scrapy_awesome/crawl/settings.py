"""Build the Scrapy settings dict for a run (both engines, escalation, throttling, cache, JOBDIR)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scrapy_awesome.recipe.models import Recipe

REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"


def abort_static_assets(request: Any) -> bool:
    """PLAYWRIGHT_ABORT_REQUEST hook: drop images/media/fonts for requests that opted in."""
    try:
        return request.resource_type in ("image", "media", "font")
    except Exception:  # pragma: no cover
        return False


def build_settings(
    recipe: Recipe,
    *,
    run_dir: Path,
    httpcache_dir: Path | None = None,
    jobdir: Path | None = None,
    log_level: str = "INFO",
    obey_robots: bool = True,
    chrome_executable_path: str | None = None,
    proxies: list[str] | None = None,
    headless: bool = True,
    concurrency: int | None = None,
    download_delay: float | None = None,
    autothrottle: bool = True,
    httpcache: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lim = recipe.limits
    conc = concurrency or lim.concurrency_per_domain
    settings: dict[str, Any] = {
        "BOT_NAME": "scrapy-awesome",
        "TWISTED_REACTOR": REACTOR,
        "LOG_LEVEL": log_level,
        "LOG_STDOUT": False,
        "ROBOTSTXT_OBEY": obey_robots,
        "USER_AGENT": "",  # let the impersonation profile / browser set it
        "COOKIES_ENABLED": True,
        "TELNETCONSOLE_ENABLED": False,
        "CONCURRENT_REQUESTS": max(conc * 2, 4),
        "CONCURRENT_REQUESTS_PER_DOMAIN": conc,
        "DOWNLOAD_DELAY": download_delay if download_delay is not None else lim.download_delay,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "DOWNLOAD_TIMEOUT": lim.request_timeout_seconds,
        "AUTOTHROTTLE_ENABLED": autothrottle,
        "AUTOTHROTTLE_START_DELAY": max(lim.download_delay, 0.5),
        "AUTOTHROTTLE_MAX_DELAY": 30.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": float(conc),
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": [500, 502, 504, 522, 524, 408],  # 403/429/503 → escalation, not retry
        "REDIRECT_ENABLED": True,
        "DEPTH_LIMIT": 0,
        "CLOSESPIDER_ERRORCOUNT": 0,
        # ---- engines ------------------------------------------------------------------------
        # Order matters. Requests run ascending: escalation (may rebuild the request from tier
        # memory) → throttle (per-domain delay/concurrency for stealth requests, which bypass
        # Scrapy's downloader slots) → stealth (answers in process_request, short-circuiting the
        # chain). Responses run descending: throttle releases its slot, then escalation may return
        # a retry Request. scrapy-stealth defines no process_response.
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy_awesome.crawl.middlewares.EscalationMiddleware": 930,
            "scrapy_awesome.crawl.throttle.ThrottleMiddleware": 940,
            "scrapy_stealth.middlewares.StealthDownloaderMiddleware": 950,
        },
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_PROVIDER": "scrapy_awesome.crawl.providers.PatchrightBrowserProvider",
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": headless},
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": lim.request_timeout_seconds * 1000,
        "PLAYWRIGHT_MAX_CONTEXTS": 4,
        "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": conc,
        "PLAYWRIGHT_ABORT_REQUEST": "scrapy_awesome.crawl.settings.abort_static_assets"
        if recipe.fetch.block_static_assets
        else None,
        "STEALTH_ENABLED": False,  # routing is explicit via meta (see FetchPolicy)
        "STEALTH_DRIVER": "turbo",
        "STEALTH_AUTO_FALLBACK": False,
        "STEALTH_PROXIES": list(proxies or []),
        "BROWSER_HEADLESS": headless,
        "BROWSER_SETTLE_S": recipe.fetch.settle_seconds or 3.0,
        "BROWSER_MAX_TABS": max(conc, 2),
        "BROWSER_STATIC_ASSETS_BLOCK": recipe.fetch.block_static_assets,
        # ---- pipelines / extensions ---------------------------------------------------------
        "ITEM_PIPELINES": {"scrapy_awesome.crawl.pipelines.ItemsPipeline": 100},
        "EXTENSIONS": {"scrapy_awesome.crawl.control.ControlExtension": 500},
        "SA_RUN_DIR": str(run_dir),
    }
    if chrome_executable_path:
        settings["BROWSER_EXECUTABLE_PATH"] = chrome_executable_path
    if jobdir:
        settings["JOBDIR"] = str(jobdir)
    if httpcache and httpcache_dir:
        settings.update(
            {
                "HTTPCACHE_ENABLED": True,
                "HTTPCACHE_DIR": str(httpcache_dir),
                "HTTPCACHE_EXPIRATION_SECS": 0,
                "HTTPCACHE_IGNORE_HTTP_CODES": [403, 429, 430, 503],
                "HTTPCACHE_GZIP": True,
                "HTTPCACHE_STORAGE": "scrapy.extensions.httpcache.FilesystemCacheStorage",
            }
        )
    if settings["PLAYWRIGHT_ABORT_REQUEST"] is None:
        del settings["PLAYWRIGHT_ABORT_REQUEST"]
    if extra:
        settings.update(extra)
    if recipe.api is not None:
        # API pages are far larger than HTML pages and one request replaces dozens, so be
        # politer per request; the RFC2616 cache policy issues conditional requests, which turns
        # a nightly re-run of an unchanged catalogue into a handful of 304s.
        settings.update(
            {
                "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
                "DOWNLOAD_DELAY": max(1.0, float(settings.get("DOWNLOAD_DELAY") or 0)),
                "RANDOMIZE_DOWNLOAD_DELAY": True,
                "AUTOTHROTTLE_ENABLED": True,
                "DOWNLOAD_MAXSIZE": 64 * 1024 * 1024,
            }
        )
        if settings.get("HTTPCACHE_ENABLED"):
            settings["HTTPCACHE_POLICY"] = "scrapy.extensions.httpcache.RFC2616Policy"

    return settings
