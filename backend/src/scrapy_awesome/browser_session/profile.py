"""'Log in once' window.

Runs as a subprocess: `python -m scrapy_awesome.browser_session.profile --id ID --url URL --out DIR`.
Opens a *headed* Patchright Chromium with a persistent profile under DIR/profile, injects a small
floating "Done — save session" bar on every page, and saves `storage_state.json` (cookies + local
storage) periodically and when the user clicks Done or closes the window. Status is written to
DIR/status.json so the server can poll it. Credentials are never seen by this code — the user types
them into the real site; only the resulting session state is stored, locally.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

DONE_BAR_JS = """
(() => {
  if (window.top !== window) return;
  const mk = () => {
    if (document.getElementById('__sa_bar')) return;
    const bar = document.createElement('div');
    bar.id = '__sa_bar';
    bar.style.cssText = 'position:fixed;z-index:2147483647;bottom:16px;right:16px;background:#111;color:#fff;'
      + 'font:14px system-ui,sans-serif;padding:10px 14px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.35);'
      + 'display:flex;gap:10px;align-items:center';
    bar.innerHTML = '<span>scrapy-awesome: log in, then</span>';
    const b = document.createElement('button');
    b.textContent = 'Done — save session';
    b.style.cssText = 'background:#16a34a;color:#fff;border:0;border-radius:8px;padding:6px 10px;cursor:pointer;font-weight:600';
    b.onclick = () => { window.__sa_done = true; b.textContent = 'Saving…'; };
    bar.appendChild(b);
    (document.body || document.documentElement).appendChild(bar);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mk); else mk();
  setInterval(mk, 1500);
})();
"""


def _write_status(out: Path, **data: object) -> None:
    tmp = out / "status.tmp"
    tmp.write_text(json.dumps({"ts": time.time(), **data}))
    tmp.replace(out / "status.json")


async def capture(session_id: str, url: str, out: Path, *, timeout: float = 900) -> int:
    from patchright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "storage_state.json"
    _write_status(out, status="pending", session_id=session_id)
    headless = os.environ.get("SA_LOGIN_HEADLESS") == "1"  # tests only
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(out / "profile"),
            headless=headless,
            channel="chrome" if _has_chrome() else None,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await ctx.add_init_script(DONE_BAR_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        closed = asyncio.Event()
        ctx.on("close", lambda: closed.set())
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # keep the window open even if the first load fails
            _write_status(out, status="pending", session_id=session_id, note=f"initial load: {exc}")

        started = time.time()
        last_save = 0.0
        done = False
        while not closed.is_set() and time.time() - started < timeout:
            await asyncio.sleep(1.0)
            # any page may have the Done flag set (the user can navigate/open tabs)
            for pg in list(ctx.pages):
                try:
                    if await pg.evaluate("() => !!window.__sa_done"):
                        done = True
                        break
                except Exception:
                    continue
            if done or time.time() - last_save > 5:
                try:
                    await ctx.storage_state(path=str(state_path))
                    last_save = time.time()
                    _write_status(
                        out,
                        status="pending",
                        session_id=session_id,
                        saved=True,
                        cookies=_cookie_count(state_path),
                    )
                except Exception:
                    pass
            if done:
                break
        with contextlib.suppress(Exception):
            if not closed.is_set():
                await ctx.storage_state(path=str(state_path))
        with contextlib.suppress(Exception):
            await ctx.close()
    ok = state_path.exists()
    _write_status(
        out,
        status="ready" if ok else "failed",
        session_id=session_id,
        cookies=_cookie_count(state_path) if ok else 0,
        domain=urlsplit(url).hostname,
    )
    return 0 if ok else 1


def _cookie_count(state_path: Path) -> int:
    try:
        return len(json.loads(state_path.read_text()).get("cookies", []))
    except Exception:
        return 0


def _has_chrome() -> bool:
    from scrapy_awesome.doctor import find_chrome

    return find_chrome() is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--timeout", type=float, default=float(os.environ.get("SA_LOGIN_TIMEOUT", "900"))
    )
    args = ap.parse_args(argv)
    return asyncio.run(capture(args.id, args.url, Path(args.out), timeout=args.timeout))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
