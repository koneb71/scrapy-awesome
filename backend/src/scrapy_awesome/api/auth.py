"""Authentication for the local server.

* **People** sign in with a username and password (`POST /api/auth/login`) and get an HttpOnly
  session cookie. The credentials live in `credentials.json` (scrypt, 0600); see `credentials.py`.
* **Machine clients** — the MCP server, the CLI, the crawl worker posting events back — present the
  per-process token from `server.json` as `Authorization: Bearer <token>`. There is no human in
  those paths to type a password, and the file is readable only by the account running the app.
* Run tokens — per-run bearer tokens for `/internal/runs/{id}/*` (worker → server).
* `GET /auth?token=…` exchanges the machine token for a session cookie. It is how the desktop
  shell opens its window on a machine with no login set yet; once a username and password exist,
  it stops signing browsers in and sends them to the login page instead.
* `/health`, the login endpoints and static assets are public; everything else under `/api`, `/ws`
  and `/internal` is protected.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, WebSocket, status

COOKIE_NAME = "sa_session"


# A local password still deserves a brake: without one, anything that reaches the port can try
# the dictionary at machine speed.
MAX_FAILURES = 8
FAILURE_WINDOW = 300.0  # seconds a failure counts for
LOCKOUT = 60.0  # seconds locked out once the window is full


@dataclass
class AuthState:
    token: str
    sessions: dict[str, float] = field(default_factory=dict)  # session id → created_at
    run_tokens: dict[str, str] = field(default_factory=dict)  # run_id → token
    session_ttl: float = 60 * 60 * 24 * 30
    failures: list[float] = field(default_factory=list)  # recent failed sign-ins
    locked_until: float = 0.0

    # ---- sign-in throttle ---------------------------------------------------------------
    def lockout_remaining(self) -> float:
        return max(0.0, self.locked_until - time.time())

    def record_failure(self) -> float:
        """Remember a failed sign-in; returns the seconds locked out (0 when still allowed)."""
        now = time.time()
        self.failures = [t for t in self.failures if now - t < FAILURE_WINDOW]
        self.failures.append(now)
        if len(self.failures) >= MAX_FAILURES:
            self.locked_until = now + LOCKOUT
            self.failures.clear()
        return self.lockout_remaining()

    def record_success(self) -> None:
        self.failures.clear()
        self.locked_until = 0.0

    def drop_session(self, sid: str | None) -> None:
        if sid:
            self.sessions.pop(sid, None)

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
