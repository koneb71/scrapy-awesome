"""Integration tests: real worker subprocesses against the local fixture sites.

Run with:  uv run pytest -q -m integration
They launch Chrome (scrapy-stealth browser tier) and Patchright (interactive tier).
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from scrapy_awesome.crawl.runner import fetch_snapshots, run_crawl, stop_run
from scrapy_awesome.recipe.models import Recipe

pytestmark = pytest.mark.integration

FIELDS = [
    {"name": "title", "extract": {"css": "h3 a", "attr": "title"}, "required": True},
    {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
]


def _recipe(base: str, path: str, **extra) -> Recipe:
    data = {
        "name": path,
        "seeds": [base + path],
        "list": {"container": "article.product_pod"},
        "fields": FIELDS,
        "limits": {"download_delay": 0.0},
    }
    data.update(extra)
    return Recipe.model_validate(data)


def test_static_list_detail_pagination(fixture_server, run_dir: Path):
    r = _recipe(
        fixture_server.base_url,
        "/static/",
        detail={"enabled": True, "link": {"css": "h3 a"}},
        pagination={"kind": "next_link", "selector": "li.next a", "max_pages": 5},
        fields=[
            *FIELDS,
            {
                "name": "description",
                "scope": "detail",
                "extract": {"css": "#product_description ~ p::text"},
            },
            {
                "name": "tags",
                "type": "list",
                "scope": "detail",
                "extract": {"css": "ul.tags li::text"},
            },
        ],
    )
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=180)
    assert res.exit_code == 0, (run_dir / "worker.log").read_text()[-3000:]
    items = res.items()
    assert len(items) == 15
    assert res.stats["reason"] == "finished"
    assert res.stats["pages"] == 18  # 3 list + 15 detail
    by_title = {i["title"]: i for i in items}
    assert by_title["Widget 07"]["description"].startswith("Widget 07 is a fine widget")
    assert by_title["Widget 07"]["tags"] == ["a", "b"]
    assert all(i["_tier"] == "http" for i in items)
    assert all(i["_provenance"]["title"] == "primary" for i in items)
    ev = Counter(e["t"] for e in res.events())
    assert ev["item"] == 15 and ev["done"] == 1 and ev["page"] == 18


def test_blocker_escalates_once_then_remembers(fixture_server, run_dir: Path):
    r = _recipe(
        fixture_server.base_url,
        "/blocker/",
        pagination={"kind": "next_link", "selector": "li.next a", "max_pages": 3},
    )
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=240)
    assert res.exit_code == 0, (run_dir / "worker.log").read_text()[-3000:]
    items = res.items()
    assert len(items) == 15
    assert Counter(i["_tier"] for i in items) == {"browser": 15}
    assert res.stats["escalations"] == {"http->browser": 1}
    assert res.stats["tier_memory"] == {"127.0.0.1": "browser"}
    blocked = [e for e in res.events() if e["t"] == "blocked"]
    assert len(blocked) == 1 and blocked[0]["reason"] == "cf_mitigated"


def test_spa_needs_browser(fixture_server, run_dir: Path):
    r = _recipe(fixture_server.base_url, "/spa/")
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=240)
    assert res.exit_code == 0, (run_dir / "worker.log").read_text()[-3000:]
    items = res.items()
    assert len(items) == 5 and all(i["_tier"] == "browser" for i in items)
    assert res.stats["escalations"] == {"http->browser": 1}


def test_infinite_scroll_interactive(fixture_server, run_dir: Path):
    r = _recipe(
        fixture_server.base_url,
        "/infinite/",
        fetch={
            "actions": [
                {"kind": "wait_for", "selector": "article.product_pod"},
                {"kind": "scroll_until_stable"},
            ]
        },
    )
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=240)
    assert res.exit_code == 0, (run_dir / "worker.log").read_text()[-3000:]
    items = res.items()
    assert len(items) == 20 and all(i["_tier"] == "interactive" for i in items)
    assert res.stats["escalations"] == {}


def test_embedded_json_container(fixture_server, run_dir: Path):
    r = Recipe.model_validate(
        {
            "name": "embedded",
            "seeds": [fixture_server.url("/embedded/")],
            "list": {"container": "json:__NEXT_DATA__.props.pageProps.products[*]"},
            "fields": [
                {"name": "title", "extract": {"json_path": "title"}},
                {"name": "price", "type": "price", "extract": {"json_path": "price"}},
                {"name": "in_stock", "type": "bool", "extract": {"json_path": "in_stock"}},
            ],
            "limits": {"download_delay": 0.0},
        }
    )
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=180)
    assert res.exit_code == 0, (run_dir / "worker.log").read_text()[-3000:]
    items = res.items()
    assert len(items) == 5
    assert (
        items[0]["title"] == "Widget 01"
        and items[0]["price"] == 11.5
        and items[0]["in_stock"] is True
    )
    assert all(i["_tier"] == "http" for i in items)


def test_max_items_limit(fixture_server, run_dir: Path):
    r = _recipe(
        fixture_server.base_url,
        "/static/",
        pagination={"kind": "next_link", "selector": "li.next a", "max_pages": 5},
    )
    res = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=180, max_items=7)
    assert res.exit_code == 0
    assert res.stats["reason"] == "max_items"
    assert 7 <= len(res.items()) <= 10  # closes at 7; a page in flight may add up to 5 more


def test_stop_and_resume(fixture_server, run_dir: Path):
    """Stop via control file after the first items, then resume with the same JOBDIR."""
    r = _recipe(
        fixture_server.base_url,
        "/static/",
        detail={"enabled": True, "link": {"css": "h3 a"}},
        pagination={"kind": "next_link", "selector": "li.next a", "max_pages": 5},
        fields=[
            *FIELDS,
            {
                "name": "description",
                "scope": "detail",
                "extract": {"css": "#product_description ~ p::text"},
            },
        ],
        limits={"download_delay": 0.4, "concurrency_per_domain": 1},
    )

    def stopper() -> None:
        deadline = time.time() + 60
        items = run_dir / "items.jsonl"
        while time.time() < deadline:
            if items.exists() and items.stat().st_size > 0:
                stop_run(run_dir)
                return
            time.sleep(0.2)

    t = threading.Thread(target=stopper, daemon=True)
    t.start()
    res1 = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=180, resume=True)
    t.join()
    assert res1.stats["reason"] == "stopped"
    n1 = len(res1.items())
    assert 0 < n1 < 15
    (run_dir / "control.json").unlink()
    res2 = run_crawl(r, run_dir=run_dir, obey_robots=False, timeout=180, resume=True)
    assert res2.stats["reason"] == "finished"
    items = res2.items()
    assert len(items) == 15, f"resumed to {len(items)} items"
    assert len({i["title"] for i in items}) == 15


def test_snapshots(fixture_server, run_dir: Path):
    r = _recipe(fixture_server.base_url, "/blocker/")
    snaps = fetch_snapshots(
        [
            fixture_server.url("/static/"),
            fixture_server.url("/blocker/"),
            fixture_server.url("/embedded/"),
        ],
        run_dir=run_dir,
        recipe=r,
        obey_robots=False,
    )
    assert [s["status"] for s in snaps] == [200, 200, 200]
    assert snaps[0]["tier"] == "http"
    assert snaps[1]["tier"] == "browser"  # escalated
    assert "__NEXT_DATA__" in snaps[2]["blobs"]
    assert "article" in snaps[1]["html"]
    ev = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert Counter(e["t"] for e in ev)["snapshot"] == 3
