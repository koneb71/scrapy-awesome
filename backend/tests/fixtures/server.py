"""FastAPI app + threaded uvicorn runner serving the fixture sites.

Usage (tests):
    with FixtureServer() as srv:
        srv.url("/static/")

Usage (manual):
    uv run python -m tests.fixtures.server  # prints the base URL and blocks
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import closing

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from tests.fixtures import sites


def build_app() -> FastAPI:
    app = FastAPI(title="scrapy-awesome fixtures")

    # ---- static -----------------------------------------------------------------------
    @app.get("/static/", response_class=HTMLResponse)
    def static_index(page: int = 1) -> str:
        return sites.list_page(page, "/static")

    @app.get("/static/item/{item_id}", response_class=HTMLResponse)
    def static_item(item_id: int) -> str:
        return sites.detail_page(item_id, "/static")

    # ---- redesigned (same catalog, different markup) — self-heal tests -------------
    @app.get("/redesign/", response_class=HTMLResponse)
    def redesign_index(page: int = 1) -> str:
        return sites.list_page(page, "/redesign", title="Static list", redesigned=True)

    @app.get("/redesign/item/{item_id}", response_class=HTMLResponse)
    def redesign_item(item_id: int) -> str:
        return sites.detail_page(item_id, "/redesign")

    # ---- xhr: a page that fetches its list (and pings analytics) ------------------------
    @app.get("/xhr/", response_class=HTMLResponse)
    def xhr_index() -> str:
        return sites.xhr_page("/xhr")

    @app.get("/xhr/api/items")
    def xhr_items(page: int = 1, limit: int = 5) -> Response:
        return Response(
            json.dumps(sites.xhr_api(page, limit)), media_type="application/json; charset=utf-8"
        )

    @app.post("/xhr/collect")
    def xhr_collect() -> Response:
        # the decoy: JSON, an array of objects, fetched by the same page
        return Response(
            json.dumps({"events": [{"t": 1, "n": "pageview"}, {"t": 2}, {"t": 3}, {"t": 4}]}),
            media_type="application/json",
        )

    @app.get("/xhr/item/{item_id}", response_class=HTMLResponse)
    def xhr_item(item_id: int) -> str:
        return sites.detail_page(item_id, "/xhr")

    # ---- spa --------------------------------------------------------------------------
    @app.get("/spa/", response_class=HTMLResponse)
    def spa_index() -> str:
        return sites.spa_page("/spa")

    @app.get("/spa/item/{item_id}", response_class=HTMLResponse)
    def spa_item(item_id: int) -> str:
        return sites.detail_page(item_id, "/spa")

    # ---- embedded json ----------------------------------------------------------------
    @app.get("/embedded/", response_class=HTMLResponse)
    def embedded_index() -> str:
        return sites.embedded_json_page("/embedded")

    # ---- blocker (JS challenge) -------------------------------------------------------
    @app.get("/blocker/")
    def blocker_index(request: Request, page: int = 1) -> Response:
        if request.cookies.get(sites.CHALLENGE_COOKIE) != "passed":
            return HTMLResponse(
                sites.challenge_page(),
                status_code=403,
                headers={"cf-mitigated": "challenge", "server": "cloudflare"},
            )
        return HTMLResponse(sites.list_page(page, "/blocker", title="Blocker list"))

    @app.get("/blocker/item/{item_id}")
    def blocker_item(request: Request, item_id: int) -> Response:
        if request.cookies.get(sites.CHALLENGE_COOKIE) != "passed":
            return HTMLResponse(
                sites.challenge_page(), status_code=403, headers={"cf-mitigated": "challenge"}
            )
        return HTMLResponse(sites.detail_page(item_id, "/blocker"))

    # ---- infinite scroll --------------------------------------------------------------
    @app.get("/infinite/", response_class=HTMLResponse)
    def infinite_index() -> str:
        return sites.infinite_page("/infinite")

    @app.get("/infinite/item/{item_id}", response_class=HTMLResponse)
    def infinite_item(item_id: int) -> str:
        return sites.detail_page(item_id, "/infinite")

    # ---- login ------------------------------------------------------------------------
    @app.get("/login/", response_class=HTMLResponse)
    def login_get(error: int = 0) -> str:
        return sites.login_form("/login", error=bool(error))

    @app.post("/login/")
    def login_post(username: str = Form(...), password: str = Form(...)) -> Response:
        if username == "alice" and password == "secret":
            resp = RedirectResponse(url="/login/private", status_code=303)
            resp.set_cookie("session", "ok", httponly=True)
            return resp
        return RedirectResponse(url="/login/?error=1", status_code=303)

    @app.get("/login/private")
    def login_private(request: Request, page: int = 1) -> Response:
        if request.cookies.get("session") != "ok":
            return RedirectResponse(url="/login/", status_code=302)
        return HTMLResponse(sites.list_page(page, "/login/private", title="Private list"))

    @app.get("/login/private/item/{item_id}")
    def login_private_item(request: Request, item_id: int) -> Response:
        if request.cookies.get("session") != "ok":
            return RedirectResponse(url="/login/", status_code=302)
        return HTMLResponse(sites.detail_page(item_id, "/login/private"))

    # ---- fake Shopify store (+ a variant with the JSON API disabled) -------------------
    def _shop_headers() -> dict[str, str]:
        # What a real store sends in Aug 2026: X-ShopId / X-Shopify-Stage are gone; powered-by,
        # server-timing and the _shopify_* cookies are what a detector can actually rely on.
        return {
            "powered-by": "Shopify",
            "server-timing": 'cfRequestDuration;dur=12, theme;desc="987654321", pageType;desc="collection"',
            "shopify-complexity-score": "12",
            "set-cookie": "_shopify_y=8f3c1b2e-fixture; path=/; SameSite=Lax",
            "x-dc": "gcp-europe-west1,gcp-europe-west4",
        }

    def _json(
        payload: object, *, headers: dict[str, str] | None = None, status: int = 200
    ) -> Response:
        return Response(
            json.dumps(payload),
            status_code=status,
            media_type="application/json; charset=utf-8",
            headers=headers,
        )

    for prefix in ("/shop", "/shop-locked", "/shop-blocked"):
        locked = prefix == "/shop-locked"

        # JSON endpoints first: `/products/<handle>.json` must win over the HTML `/products/<handle>`
        @app.get(f"{prefix}/products.json")
        def shop_products_json(limit: int = 30, page: int = 1, _locked: bool = locked) -> Response:
            if _locked:  # some stores turn the endpoint off — it 404s with an HTML page
                return HTMLResponse(
                    "<h1>404 Not Found</h1>", status_code=404, headers=_shop_headers()
                )
            return _json(sites.shopify_products_json(page, limit), headers=_shop_headers())

        @app.get(f"{prefix}/collections/{{collection}}/products.json")
        def shop_collection_products_json(
            collection: str, limit: int = 30, page: int = 1, _locked: bool = locked
        ) -> Response:
            if _locked:
                return HTMLResponse(
                    "<h1>404 Not Found</h1>", status_code=404, headers=_shop_headers()
                )
            return _json(
                sites.shopify_products_json(page, limit, collection), headers=_shop_headers()
            )

        @app.get(f"{prefix}/products/{{handle}}.json")
        def shop_product_json(handle: str, _locked: bool = locked) -> Response:
            if _locked:
                return HTMLResponse(
                    "<h1>404 Not Found</h1>", status_code=404, headers=_shop_headers()
                )
            data = sites.shopify_product_json(handle)
            if data is None:
                return _json({"errors": "Not Found"}, status=404)
            return _json(data, headers=_shop_headers())

        @app.get(f"{prefix}/meta.json")
        def shop_meta_json(_locked: bool = locked) -> Response:
            if _locked:
                return HTMLResponse(
                    "<h1>404 Not Found</h1>", status_code=404, headers=_shop_headers()
                )
            return _json(
                {
                    "id": sites.SHOP_ID,
                    "name": "Fixture Shop",
                    "city": "London",
                    "country": "GB",
                    "currency": "GBP",
                    "domain": sites.SHOP_DOMAIN,
                    "money_format": "£{{amount}}",
                },
                headers=_shop_headers(),
            )

        @app.get(f"{prefix}/cart.js")
        def shop_cart_js(_locked: bool = locked) -> Response:
            return _json(sites.shopify_cart_js(), headers=_shop_headers())

        # storefront HTML
        @app.get(f"{prefix}/", response_class=HTMLResponse)
        def shop_index(page: int = 1, _p: str = prefix) -> Response:
            return HTMLResponse(sites.shopify_page(page, _p), headers=_shop_headers())

        @app.get(f"{prefix}/collections/{{collection}}", response_class=HTMLResponse)
        def shop_collection(collection: str, page: int = 1, _p: str = prefix) -> Response:
            return HTMLResponse(sites.shopify_page(page, _p, collection), headers=_shop_headers())

        @app.get(f"{prefix}/products/{{handle}}", response_class=HTMLResponse)
        def shop_product_page(handle: str, _p: str = prefix) -> Response:
            html = sites.shopify_product_page(handle, _p)
            if html is None:
                return HTMLResponse("<h1>Not found</h1>", status_code=404)
            return HTMLResponse(html, headers=_shop_headers())

    # ---- fake WordPress site (core REST API) --------------------------------------------
    @app.get("/wp/", response_class=HTMLResponse)
    def wp_index() -> str:
        return sites.wp_page("/wp")

    @app.get("/wp/wp-json/")
    def wp_root() -> Response:
        return _json(
            {
                "name": "Fixture Blog",
                "description": "posts about widgets",
                "url": "/wp",
                "namespaces": ["wp/v2", "wp-site-health/v1"],
                "routes": {"/wp/v2/posts": {"methods": ["GET"]}},
            }
        )

    @app.get("/wp/wp-json/wp/v2/posts")
    def wp_posts(page: int = 1, per_page: int = sites.WP_POSTS_PER_PAGE) -> Response:
        posts, total, pages = sites.wp_posts(page, per_page)
        if page > pages:  # WP answers 400 rest_post_invalid_page_number past the end
            return _json(
                {
                    "code": "rest_post_invalid_page_number",
                    "message": "The page number requested is larger than the number of pages available.",
                    "data": {"status": 400},
                },
                status=400,
                headers={"x-wp-total": str(total), "x-wp-totalpages": str(pages)},
            )
        return _json(posts, headers={"x-wp-total": str(total), "x-wp-totalpages": str(pages)})

    # ---- conditional requests (incremental runs) ----------------------------------------
    @app.get("/etag/item/{item_id}", response_class=HTMLResponse)
    def etag_item(item_id: int, request: Request) -> Response:
        """Answers 304 when the client already has this version — the whole point of an
        incremental re-run."""
        html = sites.detail_page(item_id, "/etag")
        tag = f'"item-{item_id}-v1"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        return HTMLResponse(html, headers={"ETag": tag, "Cache-Control": "no-cache"})

    @app.get("/etag/sitemap.xml")
    def etag_sitemap() -> Response:
        urls = "".join(
            f"<url><loc>/etag/item/{i}</loc><lastmod>2026-08-01</lastmod></url>"
            for i in range(1, 6)
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
        )
        return Response(body, media_type="application/xml")

    # ---- sitemaps -----------------------------------------------------------------------
    @app.get("/sitemap.xml")
    def sitemap_index() -> Response:
        return Response(sites.sitemap_index(), media_type="application/xml")

    @app.get("/sitemap-items-{part}.xml")
    def sitemap_part(part: int) -> Response:
        return Response(sites.sitemap_urlset(part), media_type="application/xml")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        # Modelled on Shopify's own template (the /shop-blocked/ group is the one the fallback
        # test relies on: robots may forbid the API while allowing the page).
        return """User-agent: *
Disallow: /admin
Disallow: /cart
Disallow: /checkouts/
Disallow: /*/checkouts
Disallow: /recommendations/products
Disallow: /shop-blocked/products.json
Disallow: /shop/collections/private/products
Sitemap: /sitemap-items-0.xml
"""

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    return app


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FixtureServer:
    """Runs the fixture app in a daemon thread. Context-manager friendly."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def url(self, path: str) -> str:
        return self.base_url + path

    def start(self) -> FixtureServer:
        config = uvicorn.Config(build_app(), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="fixture-server")
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fixture server failed to start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


if __name__ == "__main__":  # pragma: no cover
    srv = FixtureServer(port=8765).start()
    print(srv.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
