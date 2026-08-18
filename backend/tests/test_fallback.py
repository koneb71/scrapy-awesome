"""Per-page LLM fallback + agent hand-off (failed pages), with a fake extract provider."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.llm.base import Budget, LLMError, ModelInfo, Usage
from scrapy_awesome.llm.fallback import json_schema, rows_from_values
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store.db import reset_store
from scrapy_awesome.store.models import FailedPageRow

TOKEN = "fb-test"


class FakeExtractProvider:
    """Reads the markdown it is given and 'extracts' widget titles/prices with a regex."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake")]

    async def run_turn(self, **kw: Any) -> Any:  # pragma: no cover - not used here
        raise NotImplementedError

    async def extract_json(self, *, model, system, prompt, schema, budget: Budget):
        import re

        self.calls.append({"model": model, "schema": schema, "prompt": prompt[:200]})
        if self.fail:
            raise LLMError("Anthropic API error 401: bad key")
        usage = Usage(input_tokens=2000, output_tokens=200, cost_usd=0.02, calls=1)
        budget.charge(usage.cost_usd)
        titles = re.findall(r"Widget (\d\d)", prompt)
        prices = re.findall(r"£(\d+\.\d\d)", prompt)
        if "items" in (schema.get("properties") or {}):
            items = [
                {"title": f"Widget {t}", "price": float(p)}
                for t, p in zip(titles, prices, strict=False)
            ]
            return {"items": items}, usage
        return {"description": "from the fallback"}, usage


def test_schema_and_rows_from_values():
    rec = Recipe.model_validate(
        {
            "name": "x",
            "seeds": ["http://x/"],
            "list": {"container": "li"},
            "detail": {"enabled": True, "link": {"css": "a"}},
            "fields": [
                {"name": "title", "extract": {"css": "h3"}},
                {"name": "price", "type": "price", "extract": {"css": ".p"}},
                {"name": "in_stock", "type": "bool", "extract": {"css": ".s"}},
                {"name": "description", "scope": "detail", "extract": {"css": "p"}},
            ],
        }
    )
    ls = json_schema(rec, "list")
    assert ls["properties"]["items"]["items"]["properties"]["price"]["type"] == ["number", "null"]
    assert set(ls["properties"]["items"]["items"]["required"]) == {"title", "price", "in_stock"}
    ds = json_schema(rec, "detail")
    assert list(ds["properties"]) == ["description"]
    page = FailedPageRow(
        id="p1", run_id="r", url="http://x/?page=2", kind="list", html_path="", tier="http"
    )
    rows = rows_from_values(
        rec,
        page,
        {"items": [{"title": "A", "price": "£1.50", "in_stock": "yes"}, {"title": None}]},
        "llm",
    )
    assert (
        rows[0]["price"] == 1.5
        and rows[0]["in_stock"] is True
        and rows[0]["_provenance"]["price"] == "llm"
    )
    assert (
        rows[0]["_url"].startswith("http://x/?page=2#llm-")
        and rows[0]["_page_url"] == "http://x/?page=2"
    )
    assert rows[1]["title"] is None
    dpage = FailedPageRow(
        id="p2",
        run_id="r",
        url="http://x/i/1",
        kind="detail",
        html_path="",
        base_row={"title": "A", "_url": "http://x/i/1", "_provenance": {"title": "primary"}},
    )
    drows = rows_from_values(rec, dpage, {"description": "hello"}, "agent")
    assert drows[0]["description"] == "hello" and drows[0]["title"] == "A"
    assert drows[0]["_provenance"] == {"title": "primary", "description": "agent"}


def _serve(provider):
    import socket
    import threading
    from contextlib import closing

    import uvicorn

    reset_store()
    paths = get_paths().ensure()
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(
        token=TOKEN, paths=paths, base_url=base, provider_factory=lambda name: provider
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return app, server, t, base


def _wait_done(c: httpx.Client, run_id: str) -> dict:
    dl = time.time() + 90
    while time.time() < dl:
        d = c.get(f"/api/runs/{run_id}").json()
        if d["status"] in ("finished", "failed", "stopped", "cancelled"):
            return d
        time.sleep(0.3)
    raise AssertionError("run did not finish")


@pytest.mark.integration
def test_llm_fallback_recovers_failed_pages(fixture_server):
    provider = FakeExtractProvider()
    _app, server, t, base = _serve(provider)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=90) as c:
            # a recipe whose container no longer matches → every list page fails
            rec = {
                "name": "broken",
                "seeds": [fixture_server.url("/static/")],
                "list": {"container": "article.gone"},
                "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 2},
                "fields": [
                    {"name": "title", "extract": {"css": "h3 a", "attr": "title"}},
                    {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
                ],
                "limits": {"download_delay": 0.05, "per_run_llm_budget_usd": 1.0},
            }
            rid = c.post("/api/recipes", json=rec).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid, "max_pages": 2}).json()
            d = _wait_done(c, run["id"])
            assert d["status"] == "finished"
            # give the async fallback workers a moment
            dl = time.time() + 20
            while time.time() < dl:
                fp = c.get(f"/api/runs/{run['id']}/failed").json()
                if fp["counts"].get("recovered", 0) >= 2:
                    break
                time.sleep(0.2)
            assert fp["counts"] == {"recovered": 2}, fp
            assert all(
                p["provider"].startswith("anthropic/") and p["rows_added"] == 5 for p in fp["pages"]
            )
            rows = c.get(f"/api/runs/{run['id']}/items?limit=50").json()
            assert rows["total"] == 10
            r0 = rows["items"][0]
            assert r0["title"].startswith("Widget") and isinstance(r0["price"], float)
            assert r0["_provenance"] == {"title": "llm", "price": "llm"} and r0["_tier"] == "http"
            stats = c.get(f"/api/runs/{run['id']}").json()["stats"]
            assert (
                stats["llm"]["pages"] == 2
                and stats["llm"]["rows"] == 10
                and stats["llm"]["cost_usd"] == pytest.approx(0.04)
            )
            assert len(provider.calls) == 2
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_agent_handoff_when_llm_unavailable(fixture_server):
    """No key (provider raises) → pages stay for the agent; get markdown + submit rows."""
    provider = FakeExtractProvider(fail=True)
    _app, server, t, base = _serve(provider)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=90) as c:
            rec = {
                "name": "broken2",
                "seeds": [fixture_server.url("/static/")],
                "list": {"container": "article.gone"},
                "fields": [
                    {"name": "title", "extract": {"css": "h3 a", "attr": "title"}},
                    {"name": "price", "type": "price", "extract": {"css": ".p"}},
                ],
                "limits": {"download_delay": 0.05},
            }
            rid = c.post("/api/recipes", json=rec).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid, "max_pages": 1}).json()
            _wait_done(c, run["id"])
            dl = time.time() + 20
            while time.time() < dl:
                fp = c.get(f"/api/runs/{run['id']}/failed").json()
                if fp["counts"].get("failed", 0) >= 1:
                    break
                time.sleep(0.2)
            assert fp["counts"] == {"failed": 1} and "401" in fp["pages"][0]["error"]
            pid = fp["pages"][0]["id"]
            detail = c.get(f"/api/runs/{run['id']}/failed/{pid}").json()
            assert "Widget 01" in detail["markdown"] and [f["name"] for f in detail["fields"]] == [
                "title",
                "price",
            ]
            r = c.post(
                f"/api/runs/{run['id']}/failed/{pid}/rows",
                json={
                    "rows": [
                        {"title": "Widget 01", "price": "£11.50", "junk": 1},
                        {"title": "Widget 02", "price": 13},
                    ]
                },
            )
            assert r.status_code == 200 and r.json() == {
                "page_id": pid,
                "rows_added": 2,
                "dropped_fields": ["junk"],
            }
            rows = c.get(f"/api/runs/{run['id']}/items").json()["items"]
            assert (
                len(rows) == 2
                and rows[0]["price"] == 11.5
                and rows[0]["_provenance"]["title"] == "agent"
            )
            assert c.get(f"/api/runs/{run['id']}/failed").json()["counts"] == {"recovered": 1}
            assert (
                c.post(f"/api/runs/{run['id']}/failed/{pid}/rows", json={"rows": []}).status_code
                == 409
            )
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


class FakeAIFieldsProvider(FakeExtractProvider):
    async def extract_json(self, *, model, system, prompt, schema, budget: Budget):
        import json as _json

        self.calls.append({"prompt": prompt[:100]})
        usage = Usage(input_tokens=500, output_tokens=100, cost_usd=0.01, calls=1)
        budget.charge(usage.cost_usd)
        rows_json = prompt.split("Rows (JSON):\n", 1)[1]
        rows = _json.loads(rows_json)
        return {
            "rows": [
                {"i": r["i"], "summary": f"{r.get('title')} for {r.get('price')}"} for r in rows
            ]
        }, usage


@pytest.mark.integration
def test_ai_fields_computed_after_run(fixture_server):
    provider = FakeAIFieldsProvider()
    _app, server, t, base = _serve(provider)
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=90) as c:
            rec = {
                "name": "ai fields",
                "seeds": [fixture_server.url("/static/")],
                "list": {"container": "article.product_pod"},
                "fields": [
                    {"name": "title", "extract": {"css": "h3 a", "attr": "title"}},
                    {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
                    {"name": "summary", "extract": {"llm": "One short line: title and price"}},
                ],
                "limits": {"download_delay": 0.05},
            }
            rid = c.post("/api/recipes", json=rec).json()["id"]
            run = c.post("/api/runs", json={"recipe_id": rid, "max_pages": 1}).json()
            _wait_done(c, run["id"])
            dl = time.time() + 20
            while time.time() < dl:
                rows = c.get(f"/api/runs/{run['id']}/items").json()["items"]
                if rows and all(r.get("summary") for r in rows):
                    break
                time.sleep(0.2)
            assert (
                len(rows) == 5
                and rows[0]["summary"] == f"{rows[0]['title']} for {rows[0]['price']}"
            )
            assert rows[0]["_provenance"]["summary"] == "llm"
            stats = c.get(f"/api/runs/{run['id']}").json()["stats"]
            assert stats["llm"]["ai_field_rows"] == 5 and stats["llm"]["cost_usd"] == pytest.approx(
                0.01
            )
            # explicit recompute endpoint (only-missing → nothing to do, no calls)
            n_calls = len(provider.calls)
            out = c.post(f"/api/runs/{run['id']}/ai-fields").json()
            assert out["rows"] == 0 and len(provider.calls) == n_calls
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
