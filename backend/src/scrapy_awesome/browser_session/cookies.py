"""Optional: import cookies from a locally installed browser into a login session
(`pip install scrapy-awesome[cookies]` → rookiepy). Nothing is uploaded anywhere; the result is a
Playwright `storage_state.json` in the sessions dir, same as the headed "log in once" flow.

Support matrix (rookiepy): Chrome, Chromium, Brave, Edge, Opera, Vivaldi, Arc (Chromium-based)
and Firefox / LibreWolf on macOS, Linux and Windows. Safari is macOS-only. Chromium browsers on
Linux/macOS may prompt for the keychain / kwallet password the first time; Windows needs the
browser to be *closed* for the cookie DB to be readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BROWSERS = (
    "chrome",
    "chromium",
    "brave",
    "edge",
    "opera",
    "vivaldi",
    "arc",
    "firefox",
    "librewolf",
    "safari",
)


class CookieImportUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import rookiepy  # noqa: F401
    except ImportError:
        return False
    return True


def import_cookies(browser: str, domains: list[str]) -> list[dict[str, Any]]:
    """Cookies for `domains` from `browser`, as Playwright cookie dicts."""
    try:
        import rookiepy
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise CookieImportUnavailable(
            "cookie import needs the optional dependency: pip install 'scrapy-awesome[cookies]'"
        ) from exc
    fn = getattr(rookiepy, browser, None)
    if fn is None:
        raise ValueError(f"unknown browser {browser!r}; one of {', '.join(BROWSERS)}")
    raw = fn(domains) if domains else fn()
    out = []
    for c in raw:
        same_site = str(c.get("same_site") or c.get("sameSite") or "Lax")
        out.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path") or "/",
                "expires": float(c["expires"]) if c.get("expires") else -1,
                "httpOnly": bool(c.get("http_only", c.get("httpOnly", False))),
                "secure": bool(c.get("secure", False)),
                "sameSite": same_site.capitalize()
                if same_site.lower() in ("lax", "strict", "none")
                else "Lax",
            }
        )
    return out


def write_storage_state(cookies: list[dict[str, Any]], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
    with __import__("contextlib").suppress(OSError):
        dest.chmod(0o600)
    return len(cookies)
