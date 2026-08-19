"""Platform detection + API mode: scoring, robots, confirmation, recipe patching, and crawls
against the fake Shopify / WordPress fixtures (including every fallback path)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.extract.engine import api_blobs, extract_list_items, html_selector
from scrapy_awesome.recipe.models import ApiConfig, Recipe
from scrapy_awesome.snapshot.jsonblobs import extract_json_blobs
from scrapy_awesome.snapshot.platform import (
    ApiOffer,
    ProbeResult,
    apply_offer,
    best,
    confirm,
    detect,
    offer_patch,
    probe_urls,
    robots_allows,
    shopify_bases,
    shopify_collection,
    shopify_store_root,
)
from scrapy_awesome.store.db import reset_store
from tests.fixtures import sites

TOKEN = "api-mode-test"
SHOP_HEADERS = {
    "powered-by": "Shopify",
    "server-timing": 'theme;desc="987654321", pageType;desc="collection"',
    "shopify-complexity-score": "12",
    "set-cookie": "_shopify_y=abc; path=/",
}


# ------------------------------------------------------------------------------- detection
def test_detects_shopify_from_headers_and_body():
    page = sites.shopify_page(1, "/shop")
    m = best("http://x/shop/", page, SHOP_HEADERS)
    assert m is not None and m.platform == "shopify" and m.score >= 20
    assert m.extras["myshopify_host"] == "fixture-shop.myshopify.com"
    assert m.extras["currency"] == "GBP"  # /products.json omits it; the theme carries it
    # the body alone is enough, because the myshopify origin is self-identifying
    body_only = best("http://x/shop/", page, {})
    assert body_only is not None and body_only.detected


def test_detection_needs_a_strong_signal_not_three_weak_ones():
    """A WordPress page that embeds a Shopify buy button is not a Shopify store."""
    page = sites.wp_page("/wp") + '<script src="https://cdn.shopify.com/buy-button.js"></script>'
    by_platform = {m.platform: m for m in detect("http://x/", page, {})}
    assert by_platform["wordpress"].detected is True
    assert by_platform["shopify"].detected is False  # 2 points, no strong signal
    assert best("http://x/", page, {}).platform == "wordpress"
    # and a plain page detects nothing at all
    assert best("http://x/", sites.list_page(1, "/static"), {}) is None


def test_woocommerce_requires_wordpress_underneath():
    woo_markup = '<body class="woocommerce"><meta name="generator" content="WooCommerce 9.1">'
    assert [m.platform for m in detect("http://x/", woo_markup, {})] == []
    with_wp = sites.wp_page("/wp") + woo_markup
    assert any(m.platform == "woocommerce" for m in detect("http://x/", with_wp, {}))


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/products.json", True),
        ("/search/suggest.json", False),  # `Disallow: /search` matches by prefix
        ("/collections/all/products.json", False),  # `/collections/*/products*`
        ("/admin/api/products.json", False),
    ],
)
def test_robots_prefix_traps(path: str, allowed: bool):
    rt = "User-agent: *\nDisallow: /admin\nDisallow: /search\nDisallow: /collections/*/products*\n"
    assert robots_allows(rt, f"https://shop.example{path}") is allowed
    assert robots_allows("", "https://shop.example/products.json") is True  # unreachable = allow


# ---------------------------------------------------------------------------- confirmation
def _probe(
    url: str, status: int = 200, ctype: str = "application/json", text: str = "{}"
) -> ProbeResult:
    return ProbeResult(url=url, status=status, content_type=ctype, text=text, final_url=url)


def test_confirm_requires_a_real_product_list():
    m = best("http://x/shop/", sites.shopify_page(1, "/shop"), SHOP_HEADERS)
    origin = "http://x/shop"
    urls = probe_urls(m, origin)
    ok = {
        urls[0]: _probe(urls[0], text=json.dumps({"id": 1, "currency": "GBP"})),
        urls[1]: _probe(urls[1], text=json.dumps(sites.shopify_products_json(1, 1))),
    }
    offer, why = confirm(m, origin, ok)
    assert offer is not None and offer.currency == "GBP" and why == ""

    # a 404 HTML page (a headless storefront, or a store with the endpoint switched off)
    bad = dict(ok)
    bad[urls[1]] = _probe(urls[1], status=404, ctype="text/html", text="<h1>Not found</h1>")
    offer, why = confirm(m, origin, bad)
    assert offer is None and "did not return a product list" in why
    # ...and a 200 that is really the frontend's app shell
    shell = dict(ok)
    shell[urls[1]] = _probe(urls[1], ctype="text/html", text="<!doctype html><div id=app>")
    assert confirm(m, origin, shell)[0] is None
    # ...and a redirect away from the endpoint
    moved = dict(ok)
    moved[urls[1]] = ProbeResult(urls[1], 200, "application/json", "{}", "http://x/blogs/discover")
    assert confirm(m, origin, moved)[0] is None
    # ...and no response at all
    assert confirm(m, origin, {})[0] is None


def test_platforms_without_an_api_are_named_but_not_offered():
    page = '<html><body>Static.SQUARESPACE_CONTEXT = {}; <img src="https://static1.squarespace.com/a.jpg">'
    m = best("http://x/", page, {})
    assert m is not None and m.platform == "squarespace" and m.detected
    assert probe_urls(m, "http://x") == []
    assert confirm(m, "http://x", {})[0] is None


# --------------------------------------------------------------------------- recipe patches
def _html_recipe(seed: str = "http://x/shop/") -> dict:
    return {
        "name": "shop",
        "seeds": [seed],
        "list": {"container": "div.grid__item"},
        "pagination": {"kind": "next_link", "selector": "a.pagination__next", "max_pages": 5},
        "fields": [
            {"name": "title", "extract": {"css": "span.product-card__title"}, "required": True},
            {"name": "price", "type": "price", "extract": {"css": "span.price::text"}},
            {"name": "mine", "extract": {"css": ".custom"}},
        ],
    }


def test_a_page_budget_written_for_html_does_not_truncate_the_catalogue():
    """20 pages of 10 is a sane HTML budget; the same numbers against pages of 250 would stop a
    catalogue walk at 1,000 products, which is not what "read the API" means."""
    offer = ApiOffer("shopify", "Shopify", "http://x/shop/products.json", "because")
    patch = offer_patch(offer, "http://x/shop", {})

    r = Recipe.model_validate(apply_offer(_html_recipe(), patch))  # limits left at the defaults
    assert (r.limits.max_pages, r.limits.max_items) == (100, 25_000)
    assert r.limits.max_pages * (r.api.paging.page_size or 0) >= r.limits.max_items

    chosen = _html_recipe()
    chosen["limits"] = {"max_items": 200, "max_pages": 3}
    r2 = Recipe.model_validate(apply_offer(chosen, patch))
    assert (r2.limits.max_pages, r2.limits.max_items) == (3, 200)  # a decision, not an accident


def test_switching_drops_a_detail_hop_nothing_reads_any_more():
    offer = ApiOffer("shopify", "Shopify", "http://x/shop/products.json", "because")
    patch = offer_patch(offer, "http://x/shop", {})

    base = _html_recipe()
    base["detail"] = {"enabled": True, "link": {"css": "a"}}  # auto-enabled by page analysis
    r = Recipe.model_validate(apply_offer(base, patch))
    assert r.detail.enabled is False  # the list payload is complete; per-row fetches buy nothing

    wanted = _html_recipe()
    wanted["detail"] = {"enabled": True, "link": {"css": "a"}}
    wanted["fields"].append(
        {"name": "spec", "scope": "detail", "extract": {"css": "#spec::text"}},
    )
    r2 = Recipe.model_validate(apply_offer(wanted, patch))
    assert r2.detail.enabled is True  # a detail field is asked for, so the hop stays


@pytest.mark.parametrize(
    "url,bases",
    [
        # the storefront root: one candidate, the whole catalogue
        ("https://s.com/", ["https://s.com"]),
        ("https://s.com/products/blue-hat", ["https://s.com"]),
        ("https://s.com/search?q=hat", ["https://s.com"]),
        # a collection page means *that* collection — with the catalogue behind it
        ("https://s.com/collections/sale", ["https://s.com/collections/sale", "https://s.com"]),
        ("https://s.com/collections/sale/", ["https://s.com/collections/sale", "https://s.com"]),
        (
            "https://s.com/collections/sale/products/blue-hat",
            ["https://s.com/collections/sale", "https://s.com"],
        ),
        # stores on a sub-path (a locale, or a shop mounted under a marketing site)
        ("https://s.com/shop/", ["https://s.com/shop"]),
        (
            "https://s.com/en-gb/collections/sale",
            ["https://s.com/en-gb/collections/sale", "https://s.com/en-gb"],
        ),
    ],
)
def test_the_endpoint_follows_the_url_you_pasted(url: str, bases: list[str]):
    """`<url>/products.json` is only right if `<url>` is the right base — a collection page has an
    endpoint of its own, and a product page has none, so it falls back to the catalogue."""
    assert shopify_bases(url) == bases


def test_a_collection_endpoint_still_links_to_the_storefront():
    base = "https://s.com/collections/sale"
    assert shopify_collection(base) == "sale" and shopify_store_root(base) == "https://s.com"
    offer = ApiOffer("shopify", "Shopify", f"{base}/products.json", "because")
    r = Recipe.model_validate(
        apply_offer(_html_recipe("https://s.com/collections/sale"), offer_patch(offer, base, {}))
    )
    assert r.api is not None
    assert r.api.url_template == f"{base}/products.json?limit={{limit}}&page={{page}}"
    assert "collections/sale" in (r.api.note or "")
    # /collections/sale/products/<handle> is not a product page; the storefront root is
    assert r.field("url").extract.template == "https://s.com/products/{value}"


def test_patch_keeps_html_selectors_as_alternates():
    offer = ApiOffer("shopify", "Shopify", "http://x/shop/products.json", "because", currency="GBP")
    merged = apply_offer(_html_recipe(), offer_patch(offer, "http://x/shop", {}))
    r = Recipe.model_validate(merged)
    assert r.ready and r.api is not None and r.api.platform == "shopify"
    assert r.list_.container == "json:body.products[*]"
    assert r.list_.alternates == ["div.grid__item"]  # HTML fallback, tried after the API path
    title = r.field("title")
    assert (
        title.extract.json_path == "$.title"
        and title.alternates[0].css == "span.product-card__title"
    )
    assert title.required is True  # user's flag survives the switch
    assert r.field("mine").extract.css == ".custom"  # fields we did not generate are kept
    assert r.pagination.kind == "next_link"  # still there for the HTML fallback
    assert r.field("url").extract.template == "http://x/shop/products/{value}"


def test_variant_granularity_expands_rows():
    offer = ApiOffer("shopify", "Shopify", "e", "r", granularity="variant")
    merged = apply_offer(_html_recipe(), offer_patch(offer, "http://x/shop", {}))
    r = Recipe.model_validate(merged)
    assert r.api.explode == "variants"
    body = sites.shopify_products_json(1, 250)
    items, _ = extract_list_items(r, "", "http://x/shop/products.json", json_blobs=api_blobs(body))
    # 20 products, of which every 5th has two variants
    assert len(items) == 24
    w5 = [i.values for i in items if i.values["title"] == "Widget 05"]
    assert [v["variant"] for v in w5] == ["Small", "Large"]
    assert [v["available"] for v in w5] == [True, False]
    assert w5[0]["price"] == 17.5 and w5[0]["sku"] == "W05-S"


def test_product_granularity_types_and_urls():
    offer = ApiOffer("shopify", "Shopify", "e", "r")
    r = Recipe.model_validate(apply_offer(_html_recipe(), offer_patch(offer, "http://x/shop", {})))
    body = sites.shopify_products_json(1, 3)
    items, which = extract_list_items(
        r, "", "http://x/shop/products.json", json_blobs=api_blobs(body)
    )
    assert which == "primary" and len(items) == 3
    v = items[0].values
    assert v["title"] == "Widget 01"
    assert v["price"] == 11.5 and isinstance(v["price"], float)  # '11.50' string decimal → number
    assert v["url"] == "http://x/shop/products/widget-01"  # built from the handle
    assert v["tags"] == ["a", "b"] and v["available"] is True
    assert v["image"].startswith("https://cdn.shopify.com/")


# ------------------------------------------------------------------------------- primitives
def test_api_config_render_and_validation():
    api = ApiConfig(
        url_template="https://x/p.json?limit={limit}&page={page}",
        paging={"kind": "page", "page_size": 250},
    )
    assert api.render(page=3) == ("https://x/p.json?limit=250&page=3", None)
    with pytest.raises(ValueError, match=r"\{page\}"):
        ApiConfig(url_template="https://x/p.json", paging={"kind": "page"})
    with pytest.raises(ValueError, match="cursor_path"):
        ApiConfig(url_template="https://x/g?after={cursor}", paging={"kind": "cursor"})
    with pytest.raises(ValueError, match="body_template requires method POST"):
        ApiConfig(url_template="https://x/g", body_template="{}", paging={"kind": "none"})


def test_json_documents_never_reach_the_html_parser():
    """parsel promotes a JSON body to type='json', whose .css()/.xpath() raise."""
    assert extract_json_blobs('{"products": [1, 2]}') == {}
    sel = html_selector('{"products": [1]}', "http://x/")
    assert sel.type == "html" and sel.css("div").get() is None
    assert html_selector("<div class=a>hi</div>").css("div.a::text").get() == "hi"


def test_api_blobs_exposes_body_and_top_level_keys():
    blobs = api_blobs({"products": [{"title": "a"}], "count": 1})
    assert blobs["body"]["count"] == 1 and blobs["products"][0]["title"] == "a"
    assert api_blobs([1, 2])["body"] == [1, 2]  # bare arrays (WP REST) stay addressable


def test_max_items_becomes_a_page_budget(tmp_path):
    from scrapy_awesome.crawl.spider import RecipeSpider
    from scrapy_awesome.recipe.io import save_recipe

    rec = Recipe.model_validate(
        {
            "name": "b",
            "seeds": ["http://x/"],
            "list": {"container": "json:body.products[*]"},
            "api": {
                "url_template": "http://x/p.json?limit={limit}&page={page}",
                "paging": {"kind": "page", "page_size": 250},
            },
            "fields": [{"name": "t", "extract": {"json_path": "$.title"}}],
            "limits": {"max_pages": 50},
        }
    )
    path = save_recipe(rec, tmp_path / "r.json")
    spider = RecipeSpider(recipe_path=str(path), run_id="t", run_dir=str(tmp_path), max_items="100")
    assert spider.api_max_pages == 1  # ceil(100 / 250): "give me 100" must not walk a catalogue
    spider = RecipeSpider(recipe_path=str(path), run_id="t", run_dir=str(tmp_path), max_items="600")
    assert spider.api_max_pages == 3


# ------------------------------------------------------------------------------ integration
def _serve():
    import socket
    import threading
    from contextlib import closing

    import uvicorn

    reset_store()
    paths = get_paths().ensure()
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(token=TOKEN, paths=paths, base_url=base)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, t, base


def _wait(c: httpx.Client, run_id: str) -> dict:
    dl = time.time() + 120
    while time.time() < dl:
        d = c.get(f"/api/runs/{run_id}").json()
        if d["status"] in ("finished", "failed", "stopped", "cancelled"):
            return d
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def _recipe_for(fixture, prefix: str) -> dict:
    r = _html_recipe(fixture.url(f"{prefix}/"))
    r["fields"] = r["fields"][:2]  # title, price
    r["limits"] = {"download_delay": 0.05, "max_pages": 10}
    return r


@pytest.mark.integration
def test_shopify_autodetected_and_scraped_through_its_api(fixture_server):
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            rows = c.post(
                "/api/pages/snapshot", json={"urls": [fixture_server.url("/shop/")], "kind": "list"}
            ).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["detected"] and plat["platform"] == "shopify" and plat["probed"]
            assert plat["api"]["currency"] == "GBP"  # from /meta.json
            assert "products.json" in plat["api"]["endpoint"]

            sw = c.post(
                f"/api/pages/{rows[0]['id']}/use-api",
                json={"recipe": _recipe_for(fixture_server, "/shop")},
            ).json()
            assert sw["ready"] and sw["platform"] == "shopify"
            rid = c.post("/api/recipes", json=sw["recipe"]).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            d = _wait(c, run["id"])
            assert d["status"] == "finished"
            items = c.get(f"/api/runs/{run['id']}/items?limit=100").json()
            assert items["total"] == len(sites.CATALOG) == 20  # the API sees the whole catalogue
            assert len({r["_url"] for r in items["items"]}) == 20  # each row has its own identity
            first = items["items"][0]
            assert first["price"] == 11.5 and first["sku"] == "W01" and first["vendor"]
            assert first["url"].endswith("/products/widget-01")
            assert first["_tier"] == "http"  # JSON endpoints never escalate to a browser
            # two data pages + the empty stop page, not one request per product
            pages = [e for e in c.get(f"/api/runs/{run['id']}/events?types=page&tail=20").json()]
            assert [e["kind"] for e in pages] == ["api", "api"]
            assert [e["items"] for e in pages] == [20, 0]

            # the verdict is remembered, so the next snapshot does not re-probe
            again = c.post(
                "/api/pages/snapshot", json={"urls": [fixture_server.url("/shop/")], "kind": "list"}
            ).json()
            assert again[0]["analysis"]["platform"].get("cached") is True
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_collection_url_scrapes_that_collection_not_the_whole_store(fixture_server):
    """Paste /collections/<handle> and the endpoint under it is what gets read — the catalogue
    would silently hand back products that are not in the collection you asked for."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    gadgets = sites.shopify_collection_items("gadgets")
    assert 0 < len(gadgets) < len(sites.CATALOG)  # the whole point of the test
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            url = fixture_server.url("/shop/collections/gadgets")
            rows = c.post("/api/pages/snapshot", json={"urls": [url], "kind": "list"}).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["detected"] and plat["api"]["endpoint"].endswith(
                "/shop/collections/gadgets/products.json"
            )
            assert "gadgets" in plat["reason"]

            html_recipe = _recipe_for(fixture_server, "/shop")
            html_recipe["seeds"] = [url]
            sw = c.post(f"/api/pages/{rows[0]['id']}/use-api", json={"recipe": html_recipe}).json()
            assert sw["ready"]
            rid = c.post("/api/recipes", json=sw["recipe"]).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            assert _wait(c, run["id"])["status"] == "finished"

            items = c.get(f"/api/runs/{run['id']}/items?limit=100").json()
            assert items["total"] == len(gadgets)
            titles = {r["title"] for r in items["items"]}
            assert titles == {g["title"] for g in gadgets}
            # a collection endpoint still links to /products/<handle>, not under the collection
            assert all("/shop/products/" in r["url"] for r in items["items"])
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_robots_blocked_collection_falls_back_to_the_catalogue(fixture_server):
    """Some stores disallow /collections/*/products* but publish /products.json. Dropping the
    blocked candidate is right; refusing API mode outright would be needlessly strict."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            url = fixture_server.url("/shop/collections/private")
            rows = c.post("/api/pages/snapshot", json={"urls": [url], "kind": "list"}).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["api"] is not None, plat["reason"]
            assert plat["api"]["endpoint"] == fixture_server.url("/shop/products.json")
            assert "robots.txt disallows" in plat["api"]["robots_note"]

            sw = c.post(
                f"/api/pages/{rows[0]['id']}/use-api",
                json={"recipe": {**_recipe_for(fixture_server, "/shop"), "seeds": [url]}},
            ).json()
            recipe = Recipe.model_validate(sw["recipe"])
            assert recipe.api is not None
            assert recipe.api.url_template.startswith(fixture_server.url("/shop/products.json"))
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_preview_reads_the_api_the_run_will_read(fixture_server):
    """The preview gate must fetch what the crawl fetches — otherwise "preview == run" is a lie
    for every API recipe, and the grid shows an empty page-1 of HTML."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            rows = c.post(
                "/api/pages/snapshot", json={"urls": [fixture_server.url("/shop/")], "kind": "list"}
            ).json()
            html_recipe = _html_recipe(fixture_server.url("/shop/"))
            html_recipe["limits"] = {"download_delay": 0.05, "max_pages": 10}
            sw = c.post(f"/api/pages/{rows[0]['id']}/use-api", json={"recipe": html_recipe}).json()
            # the Analyze tab's page, cached against the recipe the way the editor does it
            seed = c.post(
                "/api/pages/snapshot",
                json={"urls": [fixture_server.url("/shop/")], "recipe": sw["recipe"]},
            ).json()[0]
            d = c.post("/api/preview/samples", json={"recipe": sw["recipe"]}).json()

            urls = [s["url"] for s in d["samples"]]
            assert all("products.json" in u for u in urls) and len(urls) == 2
            assert [s["status"] for s in d["samples"]] == [200, 200]

            rep = d["report"]
            assert rep["ok"] is True and len(rep["rows"]) == len(sites.CATALOG) == 20
            assert rep["rows"][0]["title"] == "Widget 01" and rep["rows"][0]["price"] == 11.5
            # same row identity the run will store, so the dedupe key previews honestly
            assert rep["rows"][0]["_url"].endswith("/products/widget-01")
            assert len({r["_url"] for r in rep["rows"]}) == 20
            assert not [i for i in rep["issues"] if i["level"] == "error"]

            codes = {i["code"]: i for i in rep["issues"]}
            # the empty page 2 is how the API says "that is all", not a broken container
            assert "api_last_page" in codes and "container_missing" not in codes
            # a leftover page selector cannot read JSON: say so, but do not fail the gate
            assert codes["fallback_only_field"]["field"] == "mine"
            assert codes["next_link_missing"]["level"] == "info"  # the API pages itself

            # preview replaces its own samples but keeps the page the API stands in for, so the
            # Analyze tab (platform card, container heuristics) still has something to read
            cached = c.get(f"/api/pages?recipe_id={sw['recipe']['id']}").json()
            assert seed["id"] in [p["id"] for p in cached]
            assert [p["id"] for p in cached if p["id"] != seed["id"]]  # the API pages too
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_refuses_when_the_endpoint_is_off_and_when_robots_forbids_it(fixture_server):
    """Detection alone never switches a recipe: the confirmation probe has the last word."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            # (a) storefront looks like Shopify, but products.json is disabled → 404 HTML
            rows = c.post(
                "/api/pages/snapshot",
                json={"urls": [fixture_server.url("/shop-locked/")], "kind": "list"},
            ).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["detected"] and plat["platform"] == "shopify"
            assert plat["api"] is None and "did not return a product list" in plat["reason"]
            r = c.post(
                f"/api/pages/{rows[0]['id']}/use-api",
                json={"recipe": _recipe_for(fixture_server, "/shop-locked")},
            )
            assert r.status_code == 409  # nothing to switch to

            # (b) robots.txt disallows the endpoint although the page itself is allowed
            rows = c.post(
                "/api/pages/snapshot",
                json={"urls": [fixture_server.url("/shop-blocked/")], "kind": "list"},
            ).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["detected"] and plat["api"] is None
            assert "robots.txt disallows" in plat["reason"]

            # the HTML recipe still works — that is the point of not switching
            rid = c.post("/api/recipes", json=_recipe_for(fixture_server, "/shop-blocked")).json()[
                "id"
            ]
            d = _wait(c, c.post("/api/runs", json={"recipe_id": rid}).json()["id"])
            assert d["status"] == "finished"
            assert d["items"] == len(sites.CATALOG)  # 4 pages x 5 on the storefront
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_api_recipe_falls_back_to_html_when_the_endpoint_dies(fixture_server):
    """A recipe switched to the API keeps its selectors; if the endpoint stops answering mid-run
    the crawl finishes with them instead of failing."""
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            offer = ApiOffer("shopify", "Shopify", "e", "r")
            recipe = apply_offer(
                _recipe_for(fixture_server, "/shop-locked"),  # its products.json 404s
                offer_patch(offer, fixture_server.url("/shop-locked"), {}),
            )
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid}).json()
            d = _wait(c, run["id"])
            assert d["status"] == "finished"
            items = c.get(f"/api/runs/{run['id']}/items?limit=50").json()
            assert items["total"] == len(sites.CATALOG)  # everything the storefront exposes
            row = items["items"][0]
            assert row["title"] == "Widget 01" and row["price"] == 11.5
            # the values came from the HTML alternates, and the row says so
            assert row["_provenance"]["title"].startswith("alt")
            logs = c.get(f"/api/runs/{run['id']}/events?types=log&tail=20").json()
            assert any("fell back to HTML" in (e.get("msg") or "") for e in logs)
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_wordpress_rest_api_is_detected_and_paginated(fixture_server):
    server, t, base = _serve()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=180) as c:
            rows = c.post(
                "/api/pages/snapshot", json={"urls": [fixture_server.url("/wp/")], "kind": "list"}
            ).json()
            plat = rows[0]["analysis"]["platform"]
            assert plat["platform"] == "wordpress" and plat["api"]["label"] == "WordPress"
            sw = c.post(
                f"/api/pages/{rows[0]['id']}/use-api",
                json={
                    "recipe": {
                        "name": "blog",
                        "seeds": [fixture_server.url("/wp/")],
                        "list": {"container": "article.post"},
                        "fields": [{"name": "title", "extract": {"css": "h2.entry-title"}}],
                        "limits": {"download_delay": 0.05},
                    }
                },
            ).json()
            rec = sw["recipe"]
            assert rec["list"]["container"] == "json:body"  # the response is a bare array
            rid = c.post("/api/recipes", json=rec).json()["id"]
            d = _wait(c, c.post("/api/runs", json={"recipe_id": rid}).json()["id"])
            assert d["status"] == "finished"
            items = c.get(f"/api/runs/{d['id']}/items?limit=100").json()
            assert items["total"] == 20 and items["items"][0]["title"] == "Widget 01"
            assert items["items"][0]["url"].startswith("https://fixture.wp/")
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
