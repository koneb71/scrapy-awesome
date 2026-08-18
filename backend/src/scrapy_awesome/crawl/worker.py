"""Crawl worker process: `python -m scrapy_awesome.crawl.worker ...`

One process per crawl (or per snapshot job). Installs the asyncio reactor *before* anything imports
`twisted.internet.reactor`, builds settings, runs one spider, exits. Frozen-safe: the parent spawns
`sys.executable -m scrapy_awesome.crawl.worker` (or the frozen binary with `--worker`).

Modes:
  crawl     --recipe recipe.json --run-dir DIR [--tier T] [--max-pages N] [--max-items N] [--resume]
  snapshot  --urls '["https://…", …]' --run-dir DIR [--recipe recipe.json] [--kind list|detail] [--tier T]
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import sys
from pathlib import Path


def _install_reactor() -> None:
    from scrapy.utils.reactor import install_reactor

    install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scrapy-awesome-worker")
    p.add_argument("mode", choices=["crawl", "snapshot"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--recipe", help="recipe JSON/YAML path")
    p.add_argument("--tier", choices=["http", "browser", "interactive"], default=None)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument("--resume", action="store_true", help="reuse JOBDIR under run-dir")
    p.add_argument("--headed", action="store_true", help="show browser windows")
    p.add_argument(
        "--storage-state", default=None, help="Playwright storage_state.json for the session"
    )
    p.add_argument("--events-url", default=None)
    p.add_argument("--events-token", default=None)
    p.add_argument("--control-url", default=None)
    p.add_argument("--log-level", default=os.environ.get("SCRAPY_AWESOME_LOG_LEVEL", "INFO"))
    p.add_argument("--no-robots", action="store_true")
    p.add_argument(
        "--httpcache", action="store_true", help="enable HTTP cache (design-time iteration)"
    )
    p.add_argument("--chrome", default=None, help="BROWSER_EXECUTABLE_PATH for scrapy-stealth")
    p.add_argument("--proxy", action="append", default=[], help="proxy URL (repeatable)")
    p.add_argument(
        "--tier-memory", default=None, help="JSON dict domain→tier to seed the escalation memory"
    )
    p.add_argument("--settings-json", default=None, help="extra Scrapy settings as JSON")
    # snapshot
    p.add_argument("--urls", default=None, help="JSON list of URLs")
    p.add_argument("--kind", default="list", choices=["list", "detail", "page"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _parse(argv if argv is not None else sys.argv[1:])
    _install_reactor()

    from scrapy.crawler import CrawlerProcess

    from scrapy_awesome.config import get_paths
    from scrapy_awesome.crawl.settings import build_settings
    from scrapy_awesome.crawl.spider import RecipeSpider, SnapshotSpider
    from scrapy_awesome.recipe.io import load_recipe
    from scrapy_awesome.recipe.models import FetchConfig, Recipe

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = get_paths()

    if args.recipe:
        recipe = load_recipe(args.recipe)
    else:
        # snapshot without a recipe: a minimal stand-in just for settings
        recipe = Recipe.model_validate(
            {
                "seeds": ["https://example.invalid/"],
                "page_type": "single",
                "fields": [{"name": "title", "extract": {"css": "title::text"}}],
                "fetch": FetchConfig().model_dump(mode="json"),
            }
        )

    extra: dict = {}
    if args.settings_json:
        extra.update(json.loads(args.settings_json))
    if args.tier_memory:
        extra["SA_TIER_MEMORY"] = json.loads(args.tier_memory)
    if args.control_url:
        extra["SA_CONTROL_URL"] = args.control_url
        extra["SA_CONTROL_TOKEN"] = args.events_token
    if args.mode == "snapshot":
        # snapshots must not be dropped by dedupe/limits and should be fast
        extra["ITEM_PIPELINES"] = {}
        extra["AUTOTHROTTLE_ENABLED"] = False
        extra["DOWNLOAD_DELAY"] = 0.0

    settings = build_settings(
        recipe,
        run_dir=run_dir,
        httpcache_dir=paths.httpcache,
        jobdir=(run_dir / "jobdir") if (args.mode == "crawl" and args.resume) else None,
        log_level=args.log_level,
        obey_robots=not args.no_robots,
        chrome_executable_path=args.chrome,
        proxies=args.proxy,
        headless=not args.headed,
        httpcache=args.httpcache,
        extra=extra,
    )
    logging.getLogger("scrapy_stealth").setLevel(logging.WARNING)

    process = CrawlerProcess(settings, install_root_handler=True)
    common = {
        "run_id": args.run_id,
        "run_dir": str(run_dir),
        "tier": args.tier,
        "storage_state": args.storage_state,
        "headless": not args.headed,
        "events_url": args.events_url,
        "events_token": args.events_token,
    }
    if args.mode == "crawl":
        if not args.recipe:
            print("--recipe is required for crawl mode", file=sys.stderr)
            return 2
        process.crawl(
            RecipeSpider,
            recipe_path=args.recipe,
            max_pages=args.max_pages,
            max_items=args.max_items,
            **common,
        )
    else:
        if not args.urls:
            print("--urls is required for snapshot mode", file=sys.stderr)
            return 2
        process.crawl(
            SnapshotSpider, urls=args.urls, kind=args.kind, recipe_path=args.recipe, **common
        )
    process.start()  # blocks until the crawl finishes
    stats_file = run_dir / "stats.json"
    if stats_file.exists():
        try:
            reason = json.loads(stats_file.read_text()).get("reason")
            return (
                0 if reason in ("finished", "max_items", "stopped", "closespider_itemcount") else 3
            )
        except json.JSONDecodeError:
            return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
