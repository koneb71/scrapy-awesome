# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-dir frozen build of the `scrapy-awesome` server + worker.

Build (from backend/):   uv run --group freeze pyinstaller packaging/scrapy-awesome.spec --noconfirm
Output:                  backend/dist/scrapy-awesome/scrapy-awesome  (+ _internal/)

The same binary is the CLI, the HTTP server (`serve`), the crawl worker (`--worker`)
and the headed login window (`--login-window`) — see scrapy_awesome.cli.main.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

BACKEND = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
REPO = BACKEND.parent
UI_DIST = Path(os.environ.get("SCRAPY_AWESOME_UI_DIR") or REPO / "frontend" / "dist")

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []


def _all(pkg: str) -> None:
    d, b, h = collect_all(pkg)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# Packages that load their own submodules by dotted string (Scrapy settings, uvicorn
# loops/protocols, keyring backends, twisted reactors, sqlalchemy dialects, ...) or ship
# data files (scrapy templates, dateparser data, tldextract snapshot, patchright driver).
for pkg in (
    "scrapy_awesome",
    "scrapy",
    "scrapy_stealth",
    "scrapy_playwright",
    "patchright",  # includes driver/ (node + package) — ~130 MB, needed for the interactive tier
    "curl_cffi",
    "wreq",
    "nodriver",
    "parsel",
    "w3lib",
    "tldextract",
    "price_parser",
    "dateparser",
    "dateparser_data",
    "trafilatura",
    "justext",
    "courlan",
    "htmldate",
    "uvicorn",
    "sqlalchemy",
    "sqlmodel",
    "keyring",
    "anthropic",
    "google.genai",
    "mcp",
    "apscheduler",
    "openpyxl",
    "yaml",
    "lxml",
    "claude_agent_sdk",  # optional: "use my Claude Code login" designer (bundles a claude CLI)
    "rookiepy",  # optional: cookie import
):
    try:
        _all(pkg)
    except Exception as exc:  # optional / platform-specific packages
        print(f"[spec] skip {pkg}: {exc}", file=sys.stderr)

# Twisted: reactors + web + internet are needed; skip the big optional trees.
hiddenimports += collect_submodules(
    "twisted",
    filter=lambda n: ".test" not in n
    and not n.startswith(("twisted.conch", "twisted.mail", "twisted.words", "twisted.trial", "twisted.news")),
)
hiddenimports += ["zope.interface", "service_identity", "OpenSSL", "cryptography", "idna", "hyperlink"]

# scrapy-playwright imports the upstream `playwright` package for its types (the browser itself
# comes from Patchright via our provider) — modules only, NOT its 130 MB driver.
hiddenimports += collect_submodules("playwright")

# importlib.metadata.version(...) look-ups: Scrapy's startup banner (LOG_VERSIONS), doctor,
# __version__, keyring entry points, scrapy-playwright/-stealth banners, trafilatura UA.
for dist in (
    "scrapy-awesome",
    "scrapy",
    "scrapy-stealth",
    "scrapy-playwright",
    "playwright",
    "patchright",
    "curl_cffi",
    "wreq",
    "lxml",
    "cssselect",
    "parsel",
    "w3lib",
    "Twisted",
    "pyOpenSSL",
    "cryptography",
    "trafilatura",
    "websockets",
    "anthropic",
    "google-genai",
    "mcp",
    "keyring",
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "sqlmodel",
    "sqlalchemy",
    "typer",
    "rich",
):
    try:
        datas += copy_metadata(dist)
    except Exception as exc:
        print(f"[spec] no metadata for {dist}: {exc}", file=sys.stderr)

# The Agent SDK ships a ~300 MB `claude` binary; the desktop app relies on the user's own Claude
# Code install instead (the provider only makes sense with one), so drop the bundled copy.
datas = [d for d in datas if "claude_agent_sdk" not in d[0] or "_bundled" not in d[0]]

# Built React UI → <bundle>/_internal/ui (found by scrapy_awesome.api.app.find_ui_dir).
if (UI_DIST / "index.html").exists():
    datas.append((str(UI_DIST), "ui"))
else:
    print(f"[spec] WARNING: UI not built ({UI_DIST}); the frozen server will run API-only", file=sys.stderr)

block_cipher = None

a = Analysis(
    [str(BACKEND / "packaging" / "entry.py")],
    pathex=[str(BACKEND / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(BACKEND / "packaging" / "hooks")],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "pytest",
        "numpy",
        "pandas",
        "botocore",
        "boto3",
        "PIL",
        "scrapy_awesome.tests",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scrapy-awesome",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("SA_CODESIGN_IDENTITY"),
    entitlements_file=os.environ.get("SA_ENTITLEMENTS"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="scrapy-awesome",
)
