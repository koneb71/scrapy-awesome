"""`scrapy-awesome serve`: bind loopback, write server.json, print a ready line, run uvicorn.

server.json protocol (see docs/event-protocol.md): `{pid, port, token, url, started_at}` written
atomically (tmp+rename, 0600) under the data dir; consumers check `GET /health` before trusting it.
The ready line on stdout (`{"port":…, "token":…, "url":…}`) is the Tauri sidecar handoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import stat
import sys
import threading
import time
import webbrowser
from contextlib import closing
from pathlib import Path
from typing import Any

import uvicorn

from scrapy_awesome.config import Paths, get_paths


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_server_json(paths: Paths, info: dict[str, Any]) -> Path:
    paths.ensure()
    p = paths.server_json
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2))
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(p)
    return p


def read_server_json(paths: Paths | None = None) -> dict[str, Any] | None:
    paths = paths or get_paths()
    p = paths.server_json
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return None


def server_alive(info: dict[str, Any] | None, *, timeout: float = 2.0) -> bool:
    if not info or not info.get("port"):
        return False
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{info['port']}/health", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def find_running_server(paths: Paths | None = None) -> dict[str, Any] | None:
    info = read_server_json(paths)
    return info if server_alive(info) else None


def serve(
    *,
    port: int = 0,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    idle_exit: int | None = None,
    ppid_watch: bool = False,
    dev: bool = False,
    log_level: str = "info",
    ready_line: bool = True,
) -> int:
    """Blocking. Returns the process exit code."""
    from scrapy_awesome.api.app import create_app

    paths = get_paths().ensure()
    existing = find_running_server(paths)
    if existing and not port:
        print(
            f"a server is already running at {existing['url']} (pid {existing.get('pid')})",
            file=sys.stderr,
        )
        if open_browser:
            webbrowser.open(f"{existing['url']}/auth?token={existing['token']}")
        return 0

    # Bind + listen *before* announcing: the ready line / server.json must mean "connect now
    # works" (the sidecar and MCP auto-start connect immediately). Also makes port=0 race-free.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        print(f"cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 1
    sock.listen(128)
    sock.set_inheritable(True)
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    base_url = f"http://{host}:{port}"
    app = create_app(token=token, paths=paths, base_url=base_url, dev_cors=dev)

    info = {
        "pid": os.getpid(),
        "port": port,
        "host": host,
        "url": base_url,
        "token": token,
        "started_at": time.time(),
        "version": app.version,
    }
    write_server_json(paths, info)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        loop="asyncio",
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        # Never let a lingering browser WebSocket pin the process on SIGTERM
        # (matters for the desktop sidecar and `--idle-exit`).
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)

    stop_reason: dict[str, str] = {}

    def watchdog() -> None:
        parent = os.getppid()
        while not server.should_exit:
            time.sleep(1.0)
            if ppid_watch and os.getppid() != parent:
                stop_reason["why"] = "parent exited"
                server.should_exit = True
                return
            if idle_exit:
                idle = time.monotonic() - app.state.last_activity
                if idle > idle_exit and app.state.manager.active_crawls == 0:
                    stop_reason["why"] = f"idle for {int(idle)}s"
                    server.should_exit = True
                    return

    threading.Thread(target=watchdog, daemon=True, name="serve-watchdog").start()

    if ready_line:
        print(
            json.dumps({"port": port, "token": token, "url": base_url, "pid": os.getpid()}),
            flush=True,
        )
    auth_url = f"{base_url}/auth?token={token}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(auth_url)).start()
    else:
        print(f"open {auth_url}", file=sys.stderr, flush=True)

    try:
        if sys.platform == "win32":
            # Proactor loop is required for asyncio subprocesses on Windows
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            sock.close()
        with contextlib.suppress(OSError):
            current = read_server_json(paths)
            if current and current.get("pid") == os.getpid():
                paths.server_json.unlink()
        if stop_reason:
            print(f"server stopped: {stop_reason['why']}", file=sys.stderr)
    return 0
