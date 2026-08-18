# Spike: scrapy-stealth + scrapy-playwright (Patchright) in one spider

Date: 2026-08-18 · Script: `backend/spikes/coexist_spider.py` · Fixtures: `backend/tests/fixtures/`

## Result: PASS (7/7, ~9 s wall clock including two browser launches)

| Request | Engine / meta | Outcome |
|---|---|---|
| `/static/` | `meta={"stealth": {"driver": "turbo"}}` (wreq TLS impersonation) | 200, 5 items |
| `/static/?page=2` | `meta={"stealth": {"driver": "basic"}}` (curl_cffi) | 200, 5 items |
| `/blocker/` | stealth `turbo` | 403 + `cf-mitigated: challenge`; `AntiBotDetector.is_blocked` → True |
| `/blocker/` (retry) | `meta={"stealth": {"driver": "browser", "headless": True, "settle": 2.5}}` (nodriver Chrome) | JS challenge passed, 200, 5 items |
| `/spa/` | `meta={"playwright": True, "playwright_page_methods": [wait_for_selector]}` via **Patchright** provider | 200, 5 rendered items |
| `/spa/` | stealth `browser` (settle 1.5 s) | 200, 5 rendered items |
| `/infinite/` | Patchright + `PageMethod("evaluate", SCROLL_UNTIL_STABLE_JS)` | 20 items |

## Findings that shape the crawl layer

1. **Coexistence works** under `twisted.internet.asyncioreactor.AsyncioSelectorReactor` with
   `DOWNLOADER_MIDDLEWARES = {"scrapy_stealth.middlewares.StealthDownloaderMiddleware": 950}` and
   `DOWNLOAD_HANDLERS = {"http(s)": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler"}` +
   `PLAYWRIGHT_BROWSER_PROVIDER = "scrapy_awesome.crawl.providers.PatchrightBrowserProvider"`.
   Routing is purely by request meta: a `meta["stealth"]` **dict** → stealth engines (the middleware answers
   `process_request` itself; the download handler never sees it); no stealth dict (or `stealth: False`) +
   `meta["playwright"] = True` → Patchright. `STEALTH_ENABLED` stays False; `FetchPolicy` sets meta explicitly.
2. **We own escalation.** scrapy-stealth's `driver="auto"` fallback calls `mark_fallback_done`, which forces
   `stealth["headless"] = False` (a visible Chrome window) — undesirable inside a desktop app. Our
   `EscalationMiddleware` instead does http → browser (headless per settings) → interactive, using
   `scrapy_stealth.detectors.antibot.AntiBotDetector` + our own markers (`cf-mitigated`, empty app shell),
   and remembers the working tier per domain.
3. stealth engines run their sync clients in a thread pool (`loop.run_in_executor`), so they don't block the
   reactor; the browser driver (nodriver) launches one Chrome and uses per-request tabs. `BROWSER_SETTLE_S`
   (default 4 s) is the only wait primitive — no wait-for-selector — so it's the *browser* tier, not the
   *interactive* tier.
4. Interactive actions map cleanly onto `PageMethod`s; infinite scroll is a single `evaluate` of a
   "scroll until height stable" async JS function (bounded rounds), not N fixed scrolls.
5. Pin `scrapy-stealth==0.6.*`; the integration test derived from this spike guards upgrades.

## Addendum (Phase 1 build-out)

6. **scrapy-stealth bypasses Scrapy's downloader slots.** Because it answers requests inside
   `process_request`, `DOWNLOAD_DELAY`, `CONCURRENT_REQUESTS_PER_DOMAIN` and AutoThrottle never
   apply to stealth-routed requests (18 requests completed in ~1 s with a 0.4 s delay configured).
   `crawl/throttle.py` (`ThrottleMiddleware`, priority 940) restores a randomized per-domain delay,
   a per-domain concurrency cap and exponential backoff on blocks. Playwright-routed requests still
   use Scrapy's own slots.
7. **Middleware order:** `EscalationMiddleware` 930 → `ThrottleMiddleware` 940 → stealth 950. The
   stealth middleware short-circuits later `process_request` methods, so anything that must see the
   request has to come first; responses run in the opposite order, so the throttle releases its
   slot before escalation may return a retry `Request`.
8. A page whose list container matches is never classified "needs JS" (rendered SPAs keep their
   `<noscript>`); a page whose container matches nothing *and* has any `<script>` gets one browser
   attempt (real case: quotes.toscrape.com/js has only two scripts).
