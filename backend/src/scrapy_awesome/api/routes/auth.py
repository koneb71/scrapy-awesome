"""Sign in, sign out, and set the password for the local UI.

`/api/auth/status`, `/setup` and `/login` are reachable without a session — they are the door.
Everything else here needs one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from scrapy_awesome.api import credentials
from scrapy_awesome.api.auth import COOKIE_NAME, AuthState, is_authenticated

router = APIRouter(tags=["auth"], prefix="/auth")

COOKIE_MAX_AGE = 60 * 60 * 24 * 30


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)
    username: str | None = Field(default=None, max_length=64)


def _sign_in(request: Request, response: Response) -> None:
    auth: AuthState = request.app.state.auth
    auth.record_success()
    response.set_cookie(
        COOKIE_NAME,
        auth.new_session(),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


@router.get("/status")
def status_(request: Request) -> dict[str, Any]:
    """What the login page needs before anyone types anything: is a login configured, and am I in?"""
    auth: AuthState = request.app.state.auth
    creds = credentials.load()
    return {
        "configured": creds is not None,
        "authenticated": is_authenticated(request, auth),
        "username": creds.username if creds else None,
        "locked_for": round(auth.lockout_remaining(), 1),
        "min_password": credentials.MIN_PASSWORD,
    }


@router.post("/setup")
def setup(request: Request, response: Response, body: LoginIn) -> dict[str, Any]:
    """First run: choose the username and password. Refused once one exists — changing it then
    goes through `/password`, which proves you know the current one."""
    if credentials.configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "a login is already configured")
    try:
        creds = credentials.save(body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    _sign_in(request, response)
    return {"username": creds.username}


@router.post("/login")
def login(request: Request, response: Response, body: LoginIn) -> dict[str, Any]:
    auth: AuthState = request.app.state.auth
    remaining = auth.lockout_remaining()
    if remaining > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many failed attempts — try again in {int(remaining) + 1}s",
        )
    if not credentials.configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "no login is configured yet")
    if not credentials.verify(body.username, body.password):
        locked = auth.record_failure()
        detail = "wrong username or password"
        if locked:
            detail += f" — too many attempts, locked for {int(locked) + 1}s"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)
    _sign_in(request, response)
    creds = credentials.load()
    return {"username": creds.username if creds else body.username}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    auth: AuthState = request.app.state.auth
    auth.drop_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(request: Request, response: Response, body: PasswordIn) -> dict[str, Any]:
    """Change the password (and optionally the username), proving the current one first."""
    auth: AuthState = request.app.state.auth
    if not is_authenticated(request, auth):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    creds = credentials.load()
    if creds is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no login is configured yet")
    if not credentials.verify(creds.username, body.current_password):
        auth.record_failure()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is wrong")
    username = (body.username or creds.username).strip()
    try:
        saved = credentials.save(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # Every other session was signed in under the old password; end them, keep this one.
    auth.sessions.clear()
    _sign_in(request, response)
    return {"username": saved.username}


@router.post("/revoke-sessions")
def revoke_sessions(request: Request) -> dict[str, int]:
    """End every browser session. Reached with the machine token by `scrapy-awesome passwd`, so a
    password changed from the terminal does not leave an already-open browser signed in."""
    auth: AuthState = request.app.state.auth
    count = len(auth.sessions)
    auth.sessions.clear()
    return {"revoked": count}
