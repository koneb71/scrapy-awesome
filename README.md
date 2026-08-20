# scrapy-awesome

**Local-first, AI-assisted, interactive web scraper.** Paste a URL, say what you want and which
fields, preview the result, run the crawl, export JSON / CSV / Excel — everything runs on your machine.

- Built on **Scrapy**, with **scrapy-stealth** (TLS-impersonated HTTP + real Chrome via CDP) and
  **Playwright/Patchright** for interaction-heavy pages. Fetching escalates automatically:
  `http → browser → interactive`.
- **Claude and Gemini are optional accelerators.** They help *design* a recipe (fields, selectors,
  pagination) against cached sample pages; the crawl itself is deterministic and costs zero tokens.
- **Uses the site's own API when there is one.** Paste a Shopify (or WooCommerce/WordPress) URL and
  the platform is detected and its public JSON endpoint confirmed automatically — complete fields in
  a handful of requests, with the CSS selectors kept as fallbacks. See `docs/api-mode.md`.
- **Recipes are plain data** (JSON/YAML) — hand-editable, versioned, shareable.
- Works with your **Claude Code / Claude Desktop / Gemini CLI** through an MCP server (subscription
  users), or with an **API key** inside the app.
- Desktop app (Tauri) and a plain web mode (`scrapy-awesome serve`) share the same UI.

> Status: early development. Working today: the engine + CLI, the local server, the full UI flow
> (New → Analyze → Fields (with point-and-click picker) → Preview → Plan → Run → Export), login
> sessions, a frozen (PyInstaller) build, the **MCP server + Claude Code plugin** (`/scrape`)
> so Claude Code / Claude Desktop / Gemini CLI subscribers can drive the app with their own plan,
> the **in-app AI designer** (Claude or Gemini with your API key), and **schedules** (cron/interval
> re-runs with diffs + notifications, `scrapy-awesome service install` to run in the background),
> plus robustness: **self-healing selectors**, drift alerts, per-page **LLM fallback** / agent
> hand-off for pages selectors can't read, **AI fields**, a **Tauri desktop shell** (`desktop/`),
> recipe versions, standalone **Scrapy project export**, a Claude Desktop **MCPB** manifest and the
> opt-in "use my Claude Code login" toggle. See `docs/` (`robustness.md`, `providers.md`,
> `auth-modes.md`, `api-mode.md`, `signing-in.md`, `recipe-format.md`, `event-protocol.md`).

## Quick start (web mode)

```bash
cd frontend && pnpm install && pnpm build      # builds the UI once into frontend/dist
cd ../backend && uv sync
uv run scrapy-awesome serve                     # opens the UI; first run asks you to create a login
```

The first time the UI opens it asks you to create a **username and password** for this machine;
after that `/login` is the way in. `uv run scrapy-awesome passwd` sets or resets it from the
terminal, and `uv run scrapy-awesome open` opens the UI of a server that is already running in the
background (`service install`, the desktop app), optionally at a route: `scrapy-awesome open
/recipes`. Machine clients — the MCP server, the CLI, crawl workers — use the per-process token in
`server.json` instead. Details: `docs/signing-in.md`.

### With your Claude / Gemini subscription (MCP)

```bash
claude plugin add /path/to/scrapy-awesome/plugin        # adds the /scrape skill + MCP server
# or just the MCP server:
claude mcp add --scope user scrapy-awesome -- uv run --project /path/to/scrapy-awesome/backend scrapy-awesome mcp
```

Then in Claude Code: `/scrape https://books.toscrape.com/ title, price, rating; open each book for the description`.
The agent builds and validates the recipe in the app (which opens on your machine), asks you to
click when unsure (`request_pick`), runs the crawl, and exports CSV/XLSX/JSON. Gemini CLI and
Claude Desktop snippets: **Settings → Connect your agent** in the app, or `plugin/README.md`.
Details: `docs/auth-modes.md`.

### In-app designer (API key, or your Claude Code login)

Settings → *AI providers* → paste an Anthropic or Google (Gemini) key — **or** Settings → *Advanced*
→ enable "use my Claude Code login" (gray zone, read the note; runs on your subscription, no key).
Then on **New**, keep "Design with AI" on: the assistant fetches the page, tests selectors, saves and
validates the recipe while you watch — chat with it in the editor's **AI designer** panel ("Fix
with AI" on any weak column). Details: `docs/providers.md`, `docs/auth-modes.md`.

Or headless, from a recipe file:

```bash
uv run scrapy-awesome preview examples/books-toscrape.yaml     # validate on sample pages, no crawl
uv run scrapy-awesome run examples/books-toscrape.yaml -f xlsx  # crawl + export
```

## Layout

| Path | What |
|---|---|
| `backend/` | Python package `scrapy_awesome` — crawl engine, local server, MCP server, CLI (uv project) |
| `frontend/` | React + Vite + TypeScript + Tailwind + shadcn/ui |
| `desktop/` | Tauri v2 desktop shell wrapping the frozen sidecar (see `desktop/README.md`) |
| `plugin/` | Claude Code plugin (`/scrape` skill + MCP config); Claude Desktop / Gemini CLI snippets |
| `mcpb/` | Claude Desktop extension manifest (points at the desktop app's bundled binary) |
| `docs/` | Architecture notes, recipe format, spikes |

## Development

```bash
# backend
cd backend
uv sync
uv run scrapy-awesome doctor          # toolchain / browsers / keys check
uv run pytest -q                      # unit tests
uv run pytest -q -m integration       # spawns worker subprocesses + browsers

# frontend
cd frontend
pnpm install
pnpm dev                              # http://localhost:5173 (proxies /api to the Python server)
```

Optional browsers: `uv run patchright install chromium` (interactive tier). The stealth browser tier
uses your installed Google Chrome.

### Schedules & background service

Recipe → **Plan & run → Schedule** (hourly / daily / weekly / custom cron). Every scheduled run is
diffed against the previous one (`+new −gone ~changed`, by the recipe's dedupe key) and you get a
notification. Schedules fire while the app server is running; to keep it running without a
terminal:

```bash
uv run scrapy-awesome service install     # launchd (macOS) / systemd --user (Linux) / prints schtasks (Windows)
uv run scrapy-awesome service status
```

Retention caps (runs / cached pages per recipe, days) live in Settings → Notifications & storage.

### Frozen build (desktop sidecar)

```bash
cd backend
uv sync --group freeze
uv run --group freeze pyinstaller packaging/scrapy-awesome.spec --noconfirm   # → dist/scrapy-awesome/
uv run python packaging/smoke_test.py                                         # doctor + fixture crawl + serve
```

One binary is the CLI, the server (`serve`), the crawl worker (`--worker`) and the login window
(`--login-window`); it bundles the built UI and Patchright's driver (~300 MB one-dir). CI runs the
smoke test on macOS, Linux and Windows.

## License

MIT — see `LICENSE`.
