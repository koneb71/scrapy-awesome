"""Login sessions API: open a headed window, poll status, list/delete, refresh."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scrapy_awesome.store import SessionRow, Store, iso

router = APIRouter(tags=["sessions"])


class SessionIn(BaseModel):
    name: str = ""
    url: str


def session_out(row: SessionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "start_url": row.start_url,
        "domain": row.domain,
        "status": row.status,
        "cookies": row.cookies,
        "error": row.error,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "last_used_at": iso(row.last_used_at),
    }


def _profile_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--login-window"]
    return [sys.executable, "-m", "scrapy_awesome.browser_session.profile"]


async def _launch(request: Request, row: SessionRow) -> None:
    store: Store = request.app.state.store
    paths = request.app.state.paths
    out = paths.sessions / row.id
    out.mkdir(parents=True, exist_ok=True)
    log = (out / "window.log").open("ab")
    proc = await asyncio.create_subprocess_exec(
        *_profile_cmd(),
        "--id",
        row.id,
        "--url",
        row.start_url,
        "--out",
        str(out),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    log.close()
    procs: dict[str, asyncio.subprocess.Process] = request.app.state.__dict__.setdefault(
        "session_procs", {}
    )
    procs[row.id] = proc

    async def watch() -> None:
        code = await proc.wait()
        procs.pop(row.id, None)
        _sync_status(store, row.id, out, exit_code=code)

    task = asyncio.create_task(watch(), name=f"session-{row.id}")
    request.app.state.__dict__.setdefault("session_tasks", set()).add(task)
    task.add_done_callback(request.app.state.__dict__["session_tasks"].discard)


def _sync_status(
    store: Store, session_id: str, out: Path, *, exit_code: int | None = None
) -> SessionRow | None:
    row = store.get_session(session_id)
    if row is None:
        return None
    sp = out / "status.json"
    if sp.exists():
        try:
            st = json.loads(sp.read_text())
        except json.JSONDecodeError:
            st = {}
        status = st.get("status") or row.status
        row.status = status
        row.cookies = int(st.get("cookies") or row.cookies or 0)
        if status == "ready":
            row.storage_state_path = str(out / "storage_state.json")
            row.error = None
        elif status == "failed":
            row.error = st.get("note") or "login window closed before a session was saved"
    if exit_code not in (None, 0) and row.status != "ready":
        row.status = "failed"
        row.error = row.error or f"login window exited with code {exit_code}"
    return store.upsert_session(row)


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    paths = request.app.state.paths
    out = []
    for row in store.list_sessions():
        if row.status == "pending":
            row = _sync_status(store, row.id, paths.sessions / row.id) or row
        out.append(session_out(row))
    return out


class ImportIn(BaseModel):
    browser: str
    domain: str  # e.g. "example.com" (subdomains included)
    name: str = ""


@router.get("/sessions/import/browsers")
def import_browsers() -> dict[str, Any]:
    from scrapy_awesome.browser_session import cookies as ck

    return {"available": ck.available(), "browsers": list(ck.BROWSERS)}


@router.post("/sessions/import", status_code=201)
def import_session(request: Request, body: ImportIn) -> dict[str, Any]:
    """Create a login session from cookies of a locally installed browser (optional extra)."""
    from scrapy_awesome.browser_session import cookies as ck

    store: Store = request.app.state.store
    if not ck.available():
        raise HTTPException(
            501,
            "cookie import needs the optional dependency: pip install 'scrapy-awesome[cookies]'",
        )
    domain = (
        body.domain.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    )
    if not domain:
        raise HTTPException(422, "domain required")
    try:
        cookies = ck.import_cookies(body.browser, [domain])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # locked DB, keychain denied, …
        raise HTTPException(400, f"could not read {body.browser} cookies: {exc}") from exc
    if not cookies:
        raise HTTPException(404, f"no cookies for {domain} in {body.browser}")
    sid = uuid.uuid4().hex[:10]
    dest = request.app.state.paths.sessions / sid / "storage_state.json"
    n = ck.write_storage_state(cookies, dest)
    row = SessionRow(
        id=sid,
        name=body.name or f"{domain} ({body.browser})",
        start_url=f"https://{domain}/",
        domain=domain,
        storage_state_path=str(dest),
        status="ready",
        cookies=n,
    )
    return session_out(store.upsert_session(row))


@router.post("/sessions", status_code=201)
async def create_session(request: Request, body: SessionIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(422, "url must be http(s)")
    sid = uuid.uuid4().hex[:10]
    domain = urlsplit(body.url).hostname or ""
    row = SessionRow(
        id=sid, name=body.name or domain, start_url=body.url, domain=domain, status="pending"
    )
    row = store.upsert_session(row)
    await _launch(request, row)
    return session_out(row)


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = store.get_session(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    row = _sync_status(store, session_id, request.app.state.paths.sessions / session_id) or row
    return session_out(row)


@router.post("/sessions/{session_id}/refresh")
async def refresh_session(request: Request, session_id: str) -> dict[str, Any]:
    """Re-open the window (existing profile) to renew an expired login."""
    store: Store = request.app.state.store
    row = store.get_session(session_id)
    if not row:
        raise HTTPException(404, "session not found")
    row.status = "pending"
    row.error = None
    row = store.upsert_session(row)
    await _launch(request, row)
    return session_out(row)


@router.delete("/sessions/{session_id}")
def delete_session(request: Request, session_id: str) -> dict[str, Any]:
    store: Store = request.app.state.store
    if not store.get_session(session_id):
        raise HTTPException(404, "session not found")
    procs = request.app.state.__dict__.get("session_procs", {})
    proc = procs.get(session_id)
    if proc:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    store.delete_session(session_id)
    shutil.rmtree(request.app.state.paths.sessions / session_id, ignore_errors=True)
    return {"id": session_id, "deleted": True, "at": iso(datetime.now(UTC))}
