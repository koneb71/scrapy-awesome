# scrapy-awesome desktop (Tauri v2)

A thin native shell around the exact same UI as web mode. On launch it starts the bundled Python
sidecar (`sidecar/scrapy-awesome`, a PyInstaller one-dir build) with `serve --no-open --ppid-watch`,
waits for its JSON ready line, and opens the main window at the authenticated loopback URL — same
origin as the API and WebSockets, so nothing else is needed. Closing the window hides it to the
tray (schedules keep running); **Quit** in the tray stops the sidecar, and `--ppid-watch` makes the
sidecar exit if the shell dies. OS notifications use the Tauri notification plugin (the web UI
detects `window.__TAURI__` and falls back to the browser Notification API otherwise).

## Build

```bash
# 1. UI
cd frontend && pnpm install && pnpm build
# 2. Python sidecar (bundled as a resource → sidecar/)
cd ../backend && uv sync --group freeze && uv run --group freeze pyinstaller packaging/scrapy-awesome.spec --noconfirm
uv run python packaging/smoke_test.py          # optional: doctor + fixture crawl + serve
# 3. Desktop app
cd ../desktop && pnpm install
pnpm tauri build                                # → src-tauri/target/release/bundle/{dmg,macos,nsis,deb,appimage}
```

Dev loop (uses the frozen sidecar from `backend/dist`, or set `SCRAPY_AWESOME_SIDECAR=/path/to/scrapy-awesome`):

```bash
pnpm tauri dev
```

The app shares the web-mode data dir (platformdirs, e.g. `~/Library/Application Support/scrapy-awesome`;
`SCRAPY_AWESOME_HOME` overrides it), so recipes, runs and settings are the same in both.

## Signing / notarization (macOS)

Unsigned builds run locally. For distribution set `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
`APPLE_PASSWORD`, `APPLE_TEAM_ID` (Tauri picks them up) and note the sidecar contains native
dylibs (`curl_cffi`, `wreq`, `lxml`, Patchright's `node`) that must be signed too — the PyInstaller
spec accepts `SA_CODESIGN_IDENTITY` / `SA_ENTITLEMENTS` for that step.
