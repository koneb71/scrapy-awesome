"""HTTP client for the local scrapy-awesome server, with auto-start.

Every front door that is *not* the browser UI (the stdio MCP server, the in-app LLM designer's
tools, tests) talks to the server through this client, so there is exactly one implementation of
each capability — the REST API — and the UI updates live no matter who drives it.

Auto-start protocol (see docs/event-protocol.md → "Server handoff"):
  1. read `<data_dir>/server.json`, `GET /health` → reuse if alive;
  2. else take `<data_dir>/server.lock` (O_EXCL + pid, stale-safe), spawn
     `scrapy-awesome serve --no-open --idle-exit N` fully detached (no stdio, own session),
     and wait for server.json + /health.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from scrapy_awesome.config import Paths, get_paths

log = logging.getLogger(__name__)

DEFAULT_IDLE_EXIT = 1800  # seconds without UI/API activity before an auto-started server exits


class ToolError(RuntimeError):
    """A tool call failed in a way the agent should read and act on (bad selector, 404, …)."""


def serve_cmd() -> list[str]:
    """How to start *this same program* as a server."""
    if getattr(sys, "frozen", False):  # PyInstaller binary
        return [sys.executable]
    return [sys.executable, "-m", "scrapy_awesome"]


def _read_json(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json()
    except Exception:
        return None
    return None


def running_server(paths: Paths) -> dict[str, Any] | None:
    info = _read_json(paths.server_json)
    if info and info.get("port") and _health(int(info["port"])):
        return info
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _Lock:
    """Tiny cross-platform O_EXCL lock file holding our pid; stale locks are reclaimed."""

    def __init__(self, path: Path, stale_after: float = 60.0) -> None:
        self.path = path
        self.stale_after = stale_after
        self.held = False

    def acquire(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                self.held = True
                return True
            except FileExistsError:
                # stale? (owner dead, or too old)
                try:
                    owner = int((self.path.read_text() or "0").strip() or 0)
                    age = time.time() - self.path.stat().st_mtime
                except (OSError, ValueError):
                    owner, age = 0, 0.0
                if not _pid_alive(owner) or age > self.stale_after:
                    with contextlib.suppress(OSError):
                        self.path.unlink()
                    continue
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                self.path.unlink()
            self.held = False


def spawn_server(
    paths: Paths, *, idle_exit: int | None = DEFAULT_IDLE_EXIT
) -> subprocess.Popen[bytes]:
    """Start a detached server process; returns immediately."""
    paths.ensure()
    logf = open(paths.logs / "server-autostart.log", "ab")  # noqa: SIM115 - handed to the child
    cmd = [*serve_cmd(), "serve", "--no-open"]
    if idle_exit:
        cmd += ["--idle-exit", str(idle_exit)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": logf,
        "stderr": logf,
        "cwd": str(paths.root),
        "env": {**os.environ, "SCRAPY_AWESOME_HOME": str(paths.root)},
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    logf.close()
    return proc


def ensure_server(
    paths: Paths | None = None,
    *,
    auto_start: bool = True,
    wait: float = 45.0,
    idle_exit: int | None = DEFAULT_IDLE_EXIT,
) -> dict[str, Any]:
    """Return `{url, token, port, pid}` of a healthy server, starting one if allowed."""
    paths = (paths or get_paths()).ensure()
    info = running_server(paths)
    if info:
        return info
    if not auto_start:
        raise ToolError(
            "scrapy-awesome server is not running. Start it with `scrapy-awesome serve` "
            f"(data dir: {paths.root})."
        )
    lock = _Lock(paths.root / "server.lock")
    if not lock.acquire(timeout=wait):
        raise ToolError("could not acquire server start lock")
    try:
        info = running_server(paths)  # someone else may have started it while we waited
        if info:
            return info
        proc = spawn_server(paths, idle_exit=idle_exit)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            info = running_server(paths)
            if info:
                log.info("started scrapy-awesome server at %s (pid %s)", info["url"], info["pid"])
                return info
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        tail = ""
        with contextlib.suppress(OSError):
            tail = (paths.logs / "server-autostart.log").read_text()[-1500:]
        raise ToolError(f"server failed to start (exit={proc.poll()}). Log tail:\n{tail}")
    finally:
        lock.release()


def stop_server(paths: Paths | None = None, *, timeout: float = 15.0) -> bool:
    """Ask the running server (from server.json) to exit; True if it did (or none was running)."""
    import signal

    paths = paths or get_paths()
    info = _read_json(paths.server_json)
    pid = int(info.get("pid") or 0) if info else 0
    if not pid or not _pid_alive(pid):
        return True
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


class ServerClient:
    """Thin async client. All methods raise `ToolError` with the server's message on 4xx/5xx."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    @classmethod
    def connect(
        cls, paths: Paths | None = None, *, auto_start: bool = True, timeout: float = 120.0
    ) -> ServerClient:
        info = ensure_server(paths, auto_start=auto_start)
        return cls(info["url"], info["token"], timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- low level -----------------------------------------------------------------------
    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ToolError(f"server unreachable ({exc.__class__.__name__}: {exc})") from exc
        if r.status_code >= 400:
            detail: Any
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if isinstance(detail, dict | list):
                detail = json.dumps(detail)
            raise ToolError(f"{method} {path} → {r.status_code}: {detail}")
        return r

    async def get(self, path: str, **params: Any) -> Any:
        return (await self.request("GET", path, params=_clean(params))).json()

    async def get_text(self, path: str, **params: Any) -> str:
        return (await self.request("GET", path, params=_clean(params))).text

    async def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return (await self.request("POST", path, json=body, params=_clean(params))).json()

    async def put(self, path: str, body: Any = None) -> Any:
        return (await self.request("PUT", path, json=body)).json()

    async def delete(self, path: str) -> Any:
        return (await self.request("DELETE", path)).json()


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}
