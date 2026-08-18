"""FastAPI app + threaded uvicorn runner serving the fixture sites.

Usage (tests):
    with FixtureServer() as srv:
        srv.url("/static/")

Usage (manual):
    uv run python -m tests.fixtures.server  # prints the base URL and blocks
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
