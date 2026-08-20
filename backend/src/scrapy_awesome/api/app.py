"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from scrapy_awesome import __version__
from scrapy_awesome.api import credentials
from scrapy_awesome.api.auth import COOKIE_NAME, AuthState, is_authenticated
from scrapy_awesome.api.bus import EventBus
from scrapy_awesome.api.manager import RunManager
from scrapy_awesome.config import Paths, UserSettings, get_paths
from scrapy_awesome.llm.designer import ChatManager
from scrapy_awesome.llm.fallback import FallbackRunner
from scrapy_awesome.scheduler.service import Scheduler
from scrapy_awesome.store import Store, get_store

logger = logging.getLogger(__name__)

PUBLIC_PREFIXES = ("/health", "/auth", "/assets/", "/favicon", "/vite.svg")
# The door itself cannot be behind the lock: the login page has to ask who is configured and post
# a password before it has a session. `/api/auth/password` checks its own session.
PUBLIC_API_PATHS = ("/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/logout")


def find_ui_dir() -> Path | None:
    """Built frontend: env override, packaged `scrapy_awesome/ui`, or repo `frontend/dist`."""
    env = os.environ.get("SCRAPY_AWESOME_UI_DIR")
    candidates = [Path(env)] if env else []
    here = Path(__file__).resolve()
    if getattr(sys, "frozen", False):  # PyInstaller bundle: <bundle>/_internal/ui
        candidates.append(Path(getattr(sys, "_MEIPASS", ".")) / "ui")
    candidates += [
        here.parent.parent / "ui",  # packaged wheel
        here.parents[4]
        / "frontend"
        / "dist",  # repo checkout: <repo>/backend/src/scrapy_awesome/api/app.py
    ]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        app = request.app
        app.state.last_activity = time.monotonic()
        protected = (path.startswith("/api/") or path.startswith("/ws/")) and (
            path not in PUBLIC_API_PATHS
        )
        if protected and not is_authenticated(request, app.state.auth):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return await call_next(request)


def create_app(
    *,
    token: str | None = None,
    paths: Paths | None = None,
    store: Store | None = None,
    settings: UserSettings | None = None,
    base_url: str = "http://127.0.0.1",
    dev_cors: bool = False,
    ui_dir: Path | None = None,
    provider_factory: Any = None,
) -> FastAPI:
    paths = (paths or get_paths()).ensure()
    settings = settings or UserSettings.load(paths)
    store = store or get_store(paths)
    token = token or secrets.token_urlsafe(32)
    auth = AuthState(token=token)
    bus = EventBus()
    manager = RunManager(
        store=store, bus=bus, auth=auth, paths=paths, settings=settings, base_url=base_url
    )
    scheduler = Scheduler(store=store, manager=manager, bus=bus, settings=settings)
    fallback = FallbackRunner(
        store=store,
        bus=bus,
        paths=paths,
        settings=settings,
        manager=manager,
        provider_factory=provider_factory,
    )
    chats = ChatManager(
        store=store,
        bus=bus,
        paths=paths,
        settings=settings,
        base_url=base_url,
        token=token,
        provider_factory=provider_factory,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bus.bind_loop(asyncio.get_running_loop())
        # the manager needs the real bound URL (port may be chosen at bind time)
        manager.base_url = app.state.base_url.rstrip("/")
        chats.base_url = app.state.base_url.rstrip("/")
        await manager.start()
        await scheduler.start()
        await fallback.start()
        try:
            yield
        finally:
            await fallback.stop()
            await scheduler.stop()
            await chats.shutdown()
            await manager.shutdown()

    app = FastAPI(
        title="scrapy-awesome",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.paths = paths
    app.state.settings = settings
    app.state.store = store
    app.state.auth = auth
    app.state.bus = bus
    app.state.manager = manager
    app.state.chats = chats
    app.state.scheduler = scheduler
    app.state.fallback = fallback
    app.state.base_url = base_url
    app.state.last_activity = time.monotonic()
    app.state.started_at = time.time()

    if dev_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(AuthMiddleware)

    # ---- public --------------------------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "active_runs": manager.active_crawls,
            "uptime": round(time.time() - app.state.started_at, 1),
        }

    @app.get("/auth")
    def auth_exchange(request: Request, token: str = "", next: str = "/") -> Any:
        """Machine token → session cookie, for the desktop shell on a machine with no login set.

        Once someone has chosen a username and password, that is the way in: a token in a URL is
        exactly the thing they asked to stop chasing, and it would otherwise be a way around the
        password for anyone who can read the URL out of a shell history or a log.
        """
        if credentials.configured():
            return RedirectResponse(url="/login", status_code=303)
        if not auth.token_ok(token):
            return JSONResponse({"detail": "bad token"}, status_code=401)
        sid = auth.new_session()
        target = next if next.startswith("/") else "/"
        resp = RedirectResponse(url=target, status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            sid,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
        return resp

    # ---- routers -------------------------------------------------------------------------
    from scrapy_awesome.api.routes import (
        agent,
        chat,
        internal,
        pages,
        preview,
        recipes,
        runs,
        schedules,
        sessions,
        ws,
    )
    from scrapy_awesome.api.routes import (
        auth as auth_routes,
    )
    from scrapy_awesome.api.routes import (
        fallback as fallback_routes,
    )
    from scrapy_awesome.api.routes import (
        settings as settings_routes,
    )

    app.include_router(auth_routes.router, prefix="/api")
    app.include_router(settings_routes.router, prefix="/api")
    app.include_router(recipes.router, prefix="/api")
    app.include_router(pages.router, prefix="/api")
    app.include_router(preview.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(schedules.router, prefix="/api")
    app.include_router(fallback_routes.router, prefix="/api")
    app.include_router(internal.router, prefix="/internal")
    app.include_router(ws.router)

    # ---- static UI (SPA fallback) --------------------------------------------------------
    ui = ui_dir or find_ui_dir()
    if ui:
        app.mount("/assets", StaticFiles(directory=ui / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> Any:
            candidate = ui / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(ui / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        def no_ui() -> Any:
            return JSONResponse(
                {
                    "detail": "UI not built. Run `pnpm build` in frontend/ or use the API at /api/docs.",
                    "api_docs": "/api/docs",
                }
            )

    return app
