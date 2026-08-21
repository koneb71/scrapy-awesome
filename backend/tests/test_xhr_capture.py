"""Finding the API a page reads itself from: capture, scoring, confirmation, and the crawl.

Platform detection knows Shopify. This is the general case — watch the browser, take the JSON the
page rendered from, and refuse anything that cannot be fetched without the page's session.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot import xhr
from scrapy_awesome.snapshot.platform import apply_offer
from scrapy_awesome.store import get_store
from scrapy_awesome.store.db import reset_store
from tests.fixtures import sites
from tests.test_api_mode import TOKEN, _html_recipe, _serve, _wait

PAGE_TEXT = " ".join(i["title"] for i in sites.CATALOG[:5])


def _cap(url: str, doc: object, method: str = "GET") -> dict:
    body = json.dumps(doc)
    return {
        "url": url,
        "method": method,
        "status": 200,
        "content_type": "application/json",
        "bytes": len(body),
        "body": body,
    }


def test_the_list_on_the_page_beats_the_telemetry_next_to_it():
    real = _cap("https://s.com/api/items?page=1&limit=5", sites.xhr_api(1, 5))
    noise = _cap("https://s.com/collect", {"events": [{"t": 1}, {"t": 2}, {"t": 3}]}, "POST")
    config = _cap("https://s.com/config.json", {"flags": [{"k": "a"}, {"k": "b"}, {"k": "c"}]})

    found = xhr.candidates([noise, config, real], page_text=PAGE_TEXT)
    assert found and found[0].url.endswith("/api/items?page=1&limit=5")
    assert found[0].container == "json:body.results[*]"
    assert "analytics" not in " ".join(found[0].why)
    assert not [c for c in found if "collect" in c.url]  # scored below the floor, not offered


def test_paging_is_read_off_the_query_string():
    doc = sites.xhr_api(1, 5)
    by_page = xhr.paging_for("https://s.com/api?page=1&limit=20", 20, doc)
    assert by_page["kind"] == "page" and by_page["start"] == 1 and by_page["step"] == 1
    assert by_page["page_size"] == 20

    # an offset is a row number: {page} carries it and steps by the page size
    by_offset = xhr.paging_for("https://s.com/api?offset=0&limit=25", 25, doc)
    assert by_offset["kind"] == "page" and by_offset["start"] == 0 and by_offset["step"] == 25

    cursored = xhr.paging_for("https://s.com/api?cursor=abc", 10, {"next_cursor": "def"})
    assert cursored["kind"] == "cursor" and cursored["cursor_path"] == "$.next_cursor"

    assert xhr.paging_for("https://s.com/api/all", 10, doc)["kind"] == "none"


def test_a_candidate_becomes_a_recipe_that_validates():
    real = _cap("https://s.com/api/items?page=1&limit=5", sites.xhr_api(1, 5))
    best = xhr.candidates([real], page_text=PAGE_TEXT)[0]
    r = Recipe.model_validate(apply_offer(_html_recipe("https://s.com/"), xhr.offer_patch(best)))

    assert r.ready and r.api is not None and r.api.platform == "xhr"
    assert r.api.url_template == "https://s.com/api/items?page={page}&limit={limit}"
    assert r.api.paging.kind == "page" and r.api.paging.page_size == 5
    assert r.list_.container == "json:body.results[*]"
    assert r.list_.alternates == ["div.grid__item"]  # the page selectors stay as fallbacks
    # keys become recipe-legal names, with types read off the key and the value
    assert r.field("product_name").type == "text"
    assert r.field("price").type == "price"
    assert r.field("detail_url").type == "url"
    assert r.field("published_at").type == "date"
    assert r.field("in_stock").type == "bool"


@pytest.mark.parametrize(
    "key,expected",
    [("productName", "product_name"), ("SKU", "sku"), ("2ndPrice", "f_2nd_price"), ("__x", "x")],
)
def test_json_keys_become_recipe_legal_names(key: str, expected: str):
    assert xhr.field_name(key) == expected


@pytest.mark.integration
def test_previewing_an_endpoint_never_goes_through_a_browser(fixture_server):
    """Finding the API means browsing the site, which teaches the tier memory "interactive" for
    that host. Chrome answers a JSON URL with a *viewer document*, so a preview that inherited
    that memory would show zero rows for a recipe that crawls perfectly well."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        get_store().remember_tiers({"127.0.0.1": "interactive"})
        with httpx.Client(base_url=base, headers=hdr, timeout=300) as c:
            url = fixture_server.url("/xhr/")
            api_url = fixture_server.url("/xhr/api/items?page=1&limit=5")
            recipe = {
                **_html_recipe(url),
                "seeds": [url],
                "list": {"container": "json:body.results[*]"},
                "api": {
                    "url_template": api_url.replace("page=1", "page={page}"),
                    "paging": {"kind": "page", "start": 1, "step": 1, "page_size": 5},
                },
                "fields": [{"name": "title", "extract": {"json_path": "$.productName"}}],
            }
            d = c.post("/api/preview/samples", json={"recipe": recipe}).json()
            assert [s["tier"] for s in d["samples"]] == ["http", "http"]
            rep = d["report"]
            assert rep["ok"] and len(rep["rows"]) == 10  # page 1 and page 2, five rows each
            assert rep["rows"][0]["title"] == sites.CATALOG[0]["title"]
            assert rep["rows"][5]["title"] == sites.CATALOG[5]["title"]
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_page_that_fetches_its_list_is_read_through_that_endpoint(fixture_server):
    """End to end: browser watches the page, the endpoint is confirmed on its own, and the crawl
    reads JSON instead of a rendered DOM."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=300) as c:
            url = fixture_server.url("/xhr/")
            rows = c.post("/api/pages/snapshot", json={"urls": [url], "kind": "list"}).json()
            found = c.post(f"/api/pages/{rows[0]['id']}/find-api").json()

            assert found["watched"] >= 2  # the list endpoint and the analytics ping
            assert len(found["candidates"]) == 1  # the ping is watched, not offered
            best = found["candidates"][0]
            assert best["url"].endswith("/xhr/api/items?page=1&limit=5")
            assert found["confirmed"] == best["url_template"], found["reason"]

            sw = c.post(
                f"/api/pages/{found['sample_id']}/use-xhr",
                json={
                    "recipe": {**_html_recipe(url), "seeds": [url]},
                    "url_template": best["url_template"],
                },
            ).json()
            assert sw["ready"]
            recipe = sw["recipe"]
            recipe["limits"] = {"download_delay": 0.05, "max_pages": 10}
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            assert _wait(c, run["id"])["status"] == "finished"

            items = c.get(f"/api/runs/{run['id']}/items?limit=100").json()
            assert items["total"] == len(sites.CATALOG)  # every page of the endpoint, not one
            first = items["items"][0]
            assert first["product_name"] == sites.CATALOG[0]["title"]
            assert first["_tier"] == "http"  # the crawl never opens a browser again
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
