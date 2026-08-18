"""Authentication for the local server.

* Server token (random, per process) — accepted as `Authorization: Bearer <token>` (MCP client, CLI,
  tests) or exchanged once at `GET /auth?token=…` for an HttpOnly session cookie (browser UI).
* Run tokens — per-run bearer tokens for `/internal/runs/{id}/*` (worker → server).
* `/health` and static assets are public; everything under `/api`, `/ws`, `/internal` is protected.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, WebSocket, status

COOKIE_NAME = "sa_session"


@dataclass
class AuthState:
    token: str
    sessions: dict[str, float] = field(default_factory=dict)  # session id → created_at
    run_tokens: dict[str, str] = field(default_factory=dict)  # run_id → token
    session_ttl: float = 60 * 60 * 24 * 30

    # ---- session cookies ----------------------------------------------------------------
    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = time.time()
        return sid

    def valid_session(self, sid: str | None) -> bool:
        if not sid or sid not in self.sessions:
            return False
        if time.time() - self.sessions[sid] > self.session_ttl:
            del self.sessions[sid]
            return False
        return True

    def token_ok(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(presented, self.token)

    # ---- run tokens ---------------------------------------------------------------------
    def new_run_token(self, run_id: str) -> str:
        t = secrets.token_urlsafe(24)
        self.run_tokens[run_id] = t
        return t

    def run_token_ok(self, run_id: str, presented: str | None) -> bool:
        expected = self.run_tokens.get(run_id)
        return bool(expected and presented) and hmac.compare_digest(presented, expected)


def _bearer(request: Request | WebSocket) -> str | None:
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def is_authenticated(request: Request | WebSocket, auth: AuthState) -> bool:
    if auth.token_ok(_bearer(request)):
        return True
    return auth.valid_session(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request) -> None:
    auth: AuthState = request.app.state.auth
    if not is_authenticated(request, auth):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")


def require_run_token(request: Request, run_id: str) -> None:
    auth: AuthState = request.app.state.auth
    presented = _bearer(request)
    if auth.run_token_ok(run_id, presented) or auth.token_ok(presented):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad run token")
