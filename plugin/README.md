# scrapy-awesome — Claude Code plugin & MCP server

Use **your own Claude / Gemini subscription** to drive the local scrapy-awesome app: the agent
designs a scrape recipe with the app's tools, you watch and correct it in the app UI, the crawl
runs locally and exports CSV / JSON / XLSX. No API key is entered in the app; the app never sees
your login.

The MCP server (`scrapy-awesome mcp`) starts the local app server on first use (idle-exits after
30 min without activity) and exposes 23 tools — `fetch_page`, `search_page`, `test_selector`,
`list_json_blobs`, `save_recipe`, `validate_recipe`, `request_pick` (asks *you* to click an
element), `start_run`, `run_status`, `get_rows`, `export_run`, `open_ui`, …

## Claude Code

Plugin (adds the `/scrape` skill + MCP server) from a checkout of this repo:

```bash
claude plugin add /path/to/scrapy-awesome/plugin
```

Or just the MCP server (no skill):

```bash
claude mcp add --scope user scrapy-awesome -- uv run --project /path/to/scrapy-awesome/backend scrapy-awesome mcp
```

Then in Claude Code: `/scrape https://books.toscrape.com/ title, price, rating; open each book for the description`.

The app's **Settings → Connect your agent** card prints these commands with the right absolute
paths for your machine (and shows whether `claude` is logged in).

## Claude Desktop

Settings → Developer → Edit Config → add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "scrapy-awesome": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/scrapy-awesome/backend", "scrapy-awesome", "mcp"]
    }
  }
}
```

(A one-click `.mcpb` bundle pointing at the desktop app's bundled binary comes with the desktop release.)

## Gemini CLI

```bash
gemini mcp add scrapy-awesome uv run --project /path/to/scrapy-awesome/backend scrapy-awesome mcp
```

or add the same `mcpServers` block to `~/.gemini/settings.json`.

## Notes

- Everything is local: `127.0.0.1` only, token-authenticated, data in your user data dir
  (`scrapy-awesome doctor` prints it).
- `request_pick` opens the app and asks you to click the element the agent is unsure about — the
  answer flows back to the agent as a selector.
- Logs: MCP server → stderr; auto-started app server → `<data dir>/logs/server-autostart.log`.
