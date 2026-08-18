"""Environment diagnostics: `scrapy-awesome doctor`.

Each check returns a Check(name, status, detail). Nothing here mutates the system.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scrapy_awesome import __version__
from scrapy_awesome.config import SECRET_ENV, SecretStore, UserSettings, get_paths

Status = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str


def _pkg_version(dist: str) -> str | None:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


def find_chrome() -> str | None:
    """Best-effort system Chrome/Chromium location for scrapy-stealth's browser driver."""
    env = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if env and Path(env).exists():
        return env
    candidates: list[str] = []
    system = platform.system()
    if system == "Darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif system == "Windows":
        for base in (
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if base:
                candidates.append(
                    str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
                )
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
        ):
            p = shutil.which(name)
            if p:
                return p
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def find_patchright_browser() -> str | None:
    """Locate an installed Chromium for patchright/playwright."""
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        roots.append(Path(env))
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        roots.append(home / "Library" / "Caches" / "ms-playwright")
    elif system == "Windows":
        roots.append(Path(os.environ.get("LOCALAPPDATA", str(home))) / "ms-playwright")
    else:
        roots.append(home / ".cache" / "ms-playwright")
    for root in roots:
        if root.exists():
            hits = sorted(p.name for p in root.iterdir() if p.name.startswith("chromium"))
            if hits:
                return f"{root} ({', '.join(hits)})"
    return None


def claude_auth_status() -> dict | None:
    """Return the JSON from `claude auth status`, or None if the CLI is missing/unauthenticated."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "auth", "status"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return None


def run_checks() -> list[Check]:
    checks: list[Check] = []
    paths = get_paths()
    settings = UserSettings.load(paths)

    checks.append(Check("scrapy-awesome", "ok", f"v{__version__} · data dir {paths.root}"))
    py_ok = sys.version_info >= (3, 12)
    checks.append(
        Check(
            "python",
            "ok" if py_ok else "fail",
            f"{platform.python_version()} ({sys.executable})",
        )
    )

    for dist, module in (
        ("scrapy", "scrapy"),
        ("scrapy-stealth", "scrapy_stealth"),
        ("scrapy-playwright", "scrapy_playwright"),
        ("patchright", "patchright"),
        ("curl_cffi", "curl_cffi"),
        ("anthropic", "anthropic"),
        ("google-genai", "google.genai"),
        ("mcp", "mcp"),
    ):
        v = _pkg_version(dist)
        ok = v is not None and _importable(module)
        checks.append(Check(dist, "ok" if ok else "fail", v or "not installed"))

    chrome = settings.crawl.chrome_executable_path or find_chrome()
    checks.append(
        Check(
            "chrome (scrapy-stealth browser tier)",
            "ok" if chrome else "warn",
            chrome or "not found — set Settings → chrome_executable_path or install Google Chrome",
        )
    )
    pw = find_patchright_browser()
    checks.append(
        Check(
            "chromium (patchright interactive tier)",
            "ok" if pw else "warn",
            pw or "not found — run: uv run patchright install chromium",
        )
    )

    store = SecretStore(paths)
    checks.append(Check("secrets backend", "ok", store.backend_name()))
    for name in ("anthropic_api_key", "gemini_api_key"):
        value, source = store.get(name)  # type: ignore[arg-type]
        if value:
            masked = value[:6] + "…" + value[-4:] if len(value) > 12 else "set"
            checks.append(Check(name, "ok", f"{masked} (from {source})"))
        else:
            checks.append(
                Check(name, "warn", f"not set (Settings, or env {SECRET_ENV[name]}) — optional")
            )

    auth = claude_auth_status()
    if auth is None:
        checks.append(
            Check(
                "claude cli",
                "warn",
                "not installed / not logged in — optional (needed only for the MCP plugin path or CLI-login mode)",
            )
        )
    else:
        checks.append(
            Check(
                "claude cli",
                "ok" if auth.get("loggedIn") else "warn",
                f"loggedIn={auth.get('loggedIn')} method={auth.get('authMethod')} plan={auth.get('subscriptionType')}",
            )
        )

    checks.append(
        Check(
            "llm roles",
            "ok",
            f"designer={settings.llm.designer.provider}/{settings.llm.designer.model} · "
            f"fallback={settings.llm.fallback.provider}/{settings.llm.fallback.model}",
        )
    )
    return checks
