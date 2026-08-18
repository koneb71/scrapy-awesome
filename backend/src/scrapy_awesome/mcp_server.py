"""`scrapy-awesome mcp` — stdio MCP server for Claude Code / Claude Desktop / Gemini CLI.

This is the *compliant subscription path*: the user's own Claude (or Gemini) client does the
thinking with the user's own plan; this process only exposes the app's tools. It holds no LLM
keys and never talks to a model.

Rules of the road:
* stdout is the MCP channel — nothing else may write to it (logs → stderr; the auto-started
  server gets its own log file and no inherited stdio);
* the HTTP server is started lazily on the first tool call and shared with the browser UI, so the
  person sees everything the agent does, live;
* long-running/human steps (`request_pick`, `validate_recipe`, `run_status`) are plain awaited
  tools with generous timeouts — MCP clients tolerate that; polling variants exist too.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from scrapy_awesome import __version__
from scrapy_awesome.tools.client import ServerClient, ToolError, ensure_server
from scrapy_awesome.tools.core import TOOL_NAMES, Tools

log = logging.getLogger("scrapy_awesome.mcp")

INSTRUCTIONS = """scrapy-awesome: local-first web scraper (Scrapy + stealth HTTP/Chrome + Playwright).
You design a *recipe* (fields + selectors, pagination, detail pages) against cached sample pages, validate it, then run a deterministic crawl and export JSON/CSV/XLSX. The person can watch and edit everything in the app UI, which opens on their machine.

Typical flow:
1. fetch_page(url) → read `analysis` (containers, field guesses, pagination) and `outline`.
2. Confirm selectors with search_page / test_selector (use `container` for per-item fields); prefer embedded JSON (list_json_blobs) when the data lives there.
3. save_recipe({...}) with fields the person asked for → validate_recipe(id) → fix issues → repeat until ok.
4. start_run(id) (small max_pages first if unsure) → run_status / get_rows → export_run(id, "csv"|"xlsx"|"json", dest=...).
When a selector is ambiguous, ask the person to click it: request_pick(prompt). Use open_ui to show them a recipe or run.
Never invent values; every field must come from a selector or JSON path. Respect the person's scope (site, fields, limits)."""

_mcp = FastMCP(
    "scrapy-awesome",
    instructions=INSTRUCTIONS,
    log_level="WARNING",
)


class LazyClient(ServerClient):
    """A ServerClient that finds/starts the local server on first use (never at import/startup,
    so `claude mcp list`/handshakes stay instant and side-effect free)."""

    def __init__(self, *, auto_start: bool = True) -> None:
        self._ready = False
        self._auto_start = auto_start
        self._lock = asyncio.Lock()
        self.base_url = ""
        self.token = ""

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            info = await asyncio.to_thread(ensure_server, None, auto_start=self._auto_start)
            ServerClient.__init__(self, info["url"], info["token"], timeout=600.0)
            self._ready = True
            log.info("connected to %s", self.base_url)

    async def request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        await self._ensure()
        return await ServerClient.request(self, method, path, **kw)


_client = LazyClient()
_tools = Tools(_client)


def _register() -> None:
    for name in TOOL_NAMES:
        fn = getattr(_tools, name)
        doc = inspect.getdoc(fn) or name
        _mcp.add_tool(fn, name=name, description=doc)


_register()


def main(argv: list[str] | None = None) -> int:
    """Run the stdio server (blocking)."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s"
    )
    log.info("scrapy-awesome mcp v%s (stdio)", __version__)
    with contextlib.suppress(KeyboardInterrupt):
        _mcp.run(transport="stdio")
    return 0


__all__ = ["ToolError", "main"]
