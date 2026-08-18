"""MCP stdio server: an MCP *client* spawns `scrapy-awesome mcp`, which auto-starts the local app
server and drives the fixture site end to end — the exact path Claude Code / Gemini CLI use."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import pytest

from scrapy_awesome.config import get_paths
from scrapy_awesome.tools.client import stop_server

pytestmark = pytest.mark.integration


def _text(result: Any) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _json(result: Any) -> Any:
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(_text(result))


async def _flow(fixture_base: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "SA_NO_BROWSER": "1", "SA_INTEGRATION": "1"}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "scrapy_awesome", "mcp"], env=env
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"fetch_page", "save_recipe", "validate_recipe", "start_run", "export_run"} <= names

        # 1. status → auto-starts the HTTP server
        st = _json(await session.call_tool("app_status", {}))
        assert st["url"].startswith("http://127.0.0.1:") and st["version"]

        # 2. fetch a page through the engine
        page = _json(
            await session.call_tool(
                "fetch_page", {"url": f"{fixture_base}/static/", "kind": "list"}
            )
        )
        assert page["status"] == 200 and page["tier"] == "http"
        assert "article.product_pod" in page["outline"]
        assert page["analysis"]["containers"][0]["selector"] == "article.product_pod"
        pid = page["page_id"]

        # 3. selectors by example
        s = _json(await session.call_tool("search_page", {"page_id": pid, "text": "£11.50"}))
        assert s["matches"][0]["relative_css"] == "p.price_color"
        t = _json(
            await session.call_tool(
                "test_selector",
                {
                    "page_id": pid,
                    "selector": "h3 a",
                    "attr": "title",
                    "container": "article.product_pod",
                },
            )
        )
        assert t["container_matches"] == 5 and t["fill_rate"] == 1.0

        # 4. save + validate
        recipe = {
            "name": "MCP fixture",
            "seeds": [f"{fixture_base}/static/"],
            "intent": "titles and prices",
            "list": {"container": "article.product_pod"},
            "detail": {"enabled": True, "link": {"css": "h3 a"}},
            "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 3},
            "fields": [
                {"name": "title", "extract": {"css": "h3 a", "attr": "title"}, "required": True},
                {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
                {
                    "name": "description",
                    "scope": "detail",
                    "extract": {"css": "#product_description ~ p::text"},
                },
            ],
            "limits": {"download_delay": 0.05},
        }
        saved = _json(await session.call_tool("save_recipe", {"recipe": recipe}))
        assert saved["ready"] is True and saved["version"] == 1
        rid = saved["id"]
        rep = _json(await session.call_tool("validate_recipe", {"recipe_id": rid}))
        assert rep["ok"] is True and rep["row_count"] >= 5
        assert rep["fields"]["price"]["fill_rate"] == 1.0
        assert any(s["kind"] == "detail" for s in rep["samples"])

        # bad recipe → readable error, not a crash
        bad = await session.call_tool(
            "save_recipe",
            {"recipe": {**recipe, "fields": [{"name": "Bad Name", "extract": {"css": "a"}}]}},
        )
        assert bad.isError and "invalid" in _text(bad)

        # 5. run → wait → rows → export
        run = _json(await session.call_tool("start_run", {"recipe_id": rid, "max_pages": 2}))
        st2 = _json(
            await session.call_tool("run_status", {"run_id": run["id"], "wait_seconds": 90})
        )
        assert st2["status"] == "finished", st2
        assert st2["items"] >= 5
        rows = _json(await session.call_tool("get_rows", {"run_id": run["id"], "limit": 3}))
        assert rows["total"] >= 5 and rows["rows"][0]["title"].startswith("Widget")
        exp = _json(await session.call_tool("export_run", {"run_id": run["id"], "format": "csv"}))
        assert exp["rows"] >= 5 and exp["path"].endswith(".csv")

        # 6. human-in-the-loop pick: agent asks, "UI" answers over HTTP, agent reads the answer
        pick = _json(
            await session.call_tool(
                "request_pick",
                {
                    "prompt": "click the price",
                    "recipe_id": rid,
                    "field_name": "price",
                    "wait_seconds": 1,
                },
            )
        )
        assert pick["status"] == "pending"
        info = json.loads(get_paths().server_json.read_text())
        async with httpx.AsyncClient(
            base_url=info["url"], headers={"Authorization": f"Bearer {info['token']}"}
        ) as http:
            pend = (await http.get("/api/picks", params={"status": "pending"})).json()
            assert [p["id"] for p in pend] == [pick["id"]]
            r = await http.post(
                f"/api/picks/{pick['id']}/answer",
                json={
                    "relative_selector": "p.price_color",
                    "container": "article.product_pod",
                    "examples": ["£11.50"],
                },
            )
            assert r.status_code == 200
        got = _json(await session.call_tool("get_pick", {"pick_id": pick["id"], "wait_seconds": 5}))
        assert got["status"] == "answered" and got["answer"]["relative_selector"] == "p.price_color"


def test_mcp_stdio_end_to_end(fixture_server):
    try:
        asyncio.run(asyncio.wait_for(_flow(fixture_server.base_url), timeout=300))
    finally:
        assert stop_server(get_paths()), "auto-started server did not stop"
