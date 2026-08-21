"""Starting a crawl from a list of URLs, or from the site's own sitemap.

A list page is a convenience the site happens to offer; these two are what you reach for when it
does not — 300 product URLs in a spreadsheet, or an index of everything the site publishes.
"""

from __future__ import annotations

import httpx
import pytest

from scrapy_awesome.crawl import sitemap
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store.db import reset_store
from tests.fixtures import sites
from tests.test_api_mode import TOKEN, _serve, _wait

DETAIL_RECIPE = {
    "name": "one row per page",
    "page_type": "single",
    "fields": [
        {"name": "title", "scope": "page", "extract": {"css": "h1"}, "required": True},
        {"name": "price", "type": "price", "scope": "page", "extract": {"css": ".price_color"}},
    ],
    "limits": {"download_delay": 0.02, "max_pages": 5},
}


def test_a_pasted_list_survives_however_it_was_pasted():
    messy = """
      https://a.com/1 , https://a.com/2
      "https://a.com/3"   <https://a.com/4>
      not-a-url          https://a.com/1
    """
    assert sitemap.clean_urls(messy) == [f"https://a.com/{i}" for i in (1, 2, 3, 4)]
    assert sitemap.clean_urls(["https://a.com/x"] * 5) == ["https://a.com/x"]
    assert sitemap.clean_urls("https://a.com/1 https://a.com/2", limit=1) == ["https://a.com/1"]


def test_an_index_is_followed_and_a_urlset_is_read():
    kind, entries = sitemap.parse(sites.sitemap_index().encode(), url="https://a.com/sitemap.xml")
    assert kind == "sitemapindex" and len(entries) == sites.SITEMAP_PARTS
    assert entries[0].loc == "https://a.com/sitemap-items-0.xml"  # relative locs resolve

    kind, entries = sitemap.parse(sites.sitemap_urlset(0).encode(), url="https://a.com/s.xml")
    assert kind == "urlset" and entries[0].lastmod  # lastmod is what an incremental run needs

    items = sitemap.select(entries, include=r"/item/\d+$")
    assert len(items) == len(entries) - 1  # the about page is filtered out
    assert sitemap.select(entries, exclude=r"/item/") == [
        e for e in entries if "/item/" not in e.loc
    ]
    assert len(sitemap.select(entries, limit=3)) == 3


def test_a_gzipped_sitemap_is_read_as_xml():
    import gzip

    body = gzip.compress(sites.sitemap_urlset(0).encode())
    kind, entries = sitemap.parse(body, url="https://a.com/sitemap.xml.gz")
    assert kind == "urlset" and entries


def test_robots_points_at_the_real_sitemap():
    robots = "User-agent: *\nDisallow: /admin\nSitemap: /sitemap-items-0.xml\n"
    assert sitemap.sitemaps_in_robots(robots, "https://a.com") == [
        "https://a.com/sitemap-items-0.xml"
    ]


@pytest.mark.integration
def test_a_list_of_urls_is_crawled_one_row_per_page(fixture_server):
    server, t, base = _serve()
    urls = [fixture_server.url(f"/static/item/{i}") for i in (1, 2, 3)]
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=180
        ) as c:
            recipe = {
                **DETAIL_RECIPE,
                "seeds": [fixture_server.url("/static/")],
                "source": {"kind": "urls", "urls": urls},
            }
            assert Recipe.model_validate(recipe).ready
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            assert _wait(c, run["id"])["status"] == "finished"

            items = c.get(f"/api/runs/{run['id']}/items?limit=50").json()
            assert items["total"] == len(urls)  # the pasted list is the page budget, not max_pages
            assert {r["_url"] for r in items["items"]} == set(urls)
            assert all(r["title"] for r in items["items"])
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_sitemap_index_seeds_the_whole_catalogue(fixture_server):
    """The index is followed, both parts are read, the filter keeps the about pages out."""
    server, t, base = _serve()
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                **DETAIL_RECIPE,
                "seeds": [fixture_server.url("/static/")],
                "source": {
                    "kind": "sitemap",
                    "sitemap": fixture_server.url("/sitemap.xml"),
                    "include": r"/item/\d+$",
                    "max_urls": 12,
                },
                "limits": {"download_delay": 0.02, "max_pages": 3, "max_items": 100},
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            assert _wait(c, run["id"])["status"] == "finished"

            items = c.get(f"/api/runs/{run['id']}/items?limit=50").json()
            assert items["total"] == 12  # max_urls, honoured across both parts of the index
            assert all("/item/" in r["_url"] for r in items["items"])  # no about pages
            assert all(r["title"] for r in items["items"])
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_preview_samples_the_pages_the_source_will_crawl(fixture_server):
    """Preview must walk in through the same door as the run — the sitemap's first hits, not the
    seed page, which for a sitemap recipe is only there to name the site."""
    server, t, base = _serve()
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                **DETAIL_RECIPE,
                "seeds": [fixture_server.url("/static/")],
                "source": {
                    "kind": "sitemap",
                    "sitemap": fixture_server.url("/sitemap.xml"),
                    "include": r"/item/\d+$",
                },
            }
            d = c.post("/api/preview/samples", json={"recipe": recipe}).json()
            urls = [s["url"] for s in d["samples"]]
            assert all("/item/" in u for u in urls) and len(urls) == 2
            rep = d["report"]
            assert rep["ok"] and len(rep["rows"]) == 2  # one row per page, both sampled pages
            assert rep["fields"]["title"]["fill_rate"] == 1.0
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_missing_sitemap_falls_back_to_the_one_robots_names(fixture_server):
    server, t, base = _serve()
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                **DETAIL_RECIPE,
                "seeds": [fixture_server.url("/static/")],
                "source": {
                    "kind": "sitemap",
                    "sitemap": fixture_server.url("/no-such-sitemap.xml"),
                    "include": r"/item/\d+$",
                    "max_urls": 3,
                },
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            assert _wait(c, run["id"])["status"] == "finished"
            items = c.get(f"/api/runs/{run['id']}/items?limit=50").json()
            assert items["total"] == 3  # found through robots.txt's Sitemap: line
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
