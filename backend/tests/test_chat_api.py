"""In-app designer chat: sessions API, streamed events over WebSocket, transcript persistence,
budget/usage bookkeeping, cancel — driven by a fake provider (no network)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.llm.base import Budget, LLMError, ModelInfo, ToolSpec, TurnResult, Usage, emit
from scrapy_awesome.store.db import reset_store

TOKEN = "chat-test-token"


class FakeProvider:
    """Streams a canned reply; optionally calls one tool by name (through the real ToolSpec fn)."""

    name = "fake"

    def __init__(
        self, *, call_tool: str | None = None, tool_args: dict | None = None, slow: float = 0.0
    ):
        self.call_tool = call_tool
        self.tool_args = tool_args or {}
        self.slow = slow
        self.seen_history: list[Any] = []
        self.seen_user: str = ""

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake-1")]

    async def run_turn(
        self,
        *,
        model,
        system,
        history,
        user_message,
        tools,
        effort,
        budget: Budget,
        on_event,
        max_iterations=40,
    ):
        self.seen_history = list(history)
        self.seen_user = user_message
        text = ""
        for chunk in ("Hello ", "from ", "the fake."):
            await asyncio.sleep(self.slow)
            text += chunk
            await emit(on_event, {"t": "text_delta", "text": chunk})
        tool_calls = 0
        if self.call_tool:
            spec: ToolSpec = next(t for t in tools if t.name == self.call_tool)
            await emit(
                on_event, {"t": "tool_call", "id": "c1", "name": spec.name, "input": self.tool_args}
            )
            result = await spec.fn(**self.tool_args)
            await emit(
                on_event,
                {
                    "t": "tool_result",
                    "name": spec.name,
                    "ok": True,
                    "summary": f"{len(result)} items",
                },
            )
            text += f" Found {len(result)} recipes."
            tool_calls = 1
        usage = Usage(input_tokens=1000, output_tokens=100, cost_usd=0.0075, calls=1)
        budget.charge(usage.cost_usd)
        await emit(on_event, {"t": "usage", **usage.to_dict()})
        await emit(on_event, {"t": "done", "text": text, "stop_reason": "end_turn"})
        return TurnResult(
            text=text,
            history=[
                *history,
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": text},
            ],
            usage=usage,
            stop_reason="end_turn",
            tool_calls=tool_calls,
        )


def _app(provider: Any):
    reset_store()
    return create_app(token=TOKEN, provider_factory=lambda name: provider)


@pytest.fixture
def client():
    fake = FakeProvider()
    app = _app(fake)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        c.fake = fake  # type: ignore[attr-defined]
        yield c


def _wait_idle(c: TestClient, chat_id: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = c.get(f"/api/chats/{chat_id}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("turn did not finish")


def test_chat_roundtrip_persists_and_streams(client: TestClient):
    r = client.post("/api/chats", json={"title": ""})
    assert r.status_code == 201
    chat = r.json()
    assert (
        chat["provider"] == "anthropic"
        and chat["model"] == "claude-opus-5"
        and chat["status"] == "idle"
    )
    cid = chat["id"]

    with client.websocket_connect(f"/ws/chats/{cid}") as ws:
        r = client.post(
            f"/api/chats/{cid}/messages", json={"content": "Scrape http://x/ for names"}
        )
        assert r.status_code == 202 and r.json()["status"] == "running"
        assert r.json()["title"].startswith("Scrape http://x/")
        seen = []
        deadline = time.time() + 10
        while time.time() < deadline:
            ev = json.loads(ws.receive_text())
            if ev.get("t") == "ping":
                continue
            seen.append(ev["t"])
            if ev.get("t") == "turn_end":
                assert ev["usage"]["cost_usd"] == pytest.approx(0.0075)
                break
        assert (
            seen[0] == "turn_start"
            and seen.count("text_delta") == 3
            and "usage" in seen
            and "done" in seen
        )

    d = _wait_idle(client, cid)
    assert d["status"] == "idle" and d["error"] is None
    msgs = d["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Hello from the fake." and msgs[1]["stop_reason"] == "end_turn"
    assert d["usage"]["calls"] == 1 and d["usage"]["cost_usd"] == pytest.approx(0.0075)
    # the dynamic context block reached the provider, prepended to the user text
    fake = client.fake  # type: ignore[attr-defined]
    assert fake.seen_user.startswith("[context] robots.txt: respected") and fake.seen_user.endswith(
        "for names"
    )

    # second turn carries provider-native history and accumulates usage
    client.post(f"/api/chats/{cid}/messages", json={"content": "and prices"})
    d = _wait_idle(client, cid)
    assert len(d["messages"]) == 4 and d["usage"]["calls"] == 2
    assert [m["role"] for m in fake.seen_history] == ["user", "assistant"]
    assert client.get("/api/chats").json()[0]["id"] == cid
    assert client.delete(f"/api/chats/{cid}").json()["deleted"] is True
    assert client.get(f"/api/chats/{cid}").status_code == 404


def test_chat_concurrent_turn_rejected_and_cancel():
    fake = FakeProvider(slow=0.3)
    app = _app(fake)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        cid = c.post("/api/chats", json={}).json()["id"]
        assert c.post(f"/api/chats/{cid}/messages", json={"content": "go"}).status_code == 202
        assert c.post(f"/api/chats/{cid}/messages", json={"content": "again"}).status_code == 409
        assert c.post(f"/api/chats/{cid}/cancel").json()["cancelled"] is True
        d = _wait_idle(c, cid)
        assert d["messages"][-1]["stop_reason"] == "cancelled"
        assert c.post(f"/api/chats/{cid}/cancel").json()["cancelled"] is False


def test_chat_missing_key_is_a_400():
    def factory(name: str):
        raise LLMError("No Anthropic API key. Add one in Settings → AI providers.")

    reset_store()
    app = create_app(token=TOKEN, provider_factory=factory)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        cid = c.post("/api/chats", json={"provider": "anthropic"}).json()["id"]
        r = c.post(f"/api/chats/{cid}/messages", json={"content": "hi"})
        assert r.status_code == 400 and "API key" in r.json()["detail"]
        assert c.post("/api/chats", json={"provider": "nope"}).status_code == 422


def test_llm_models_fallback_without_key(client: TestClient):
    d = client.get("/api/llm/models?provider=gemini").json()
    assert d["source"] == "fallback" and "gemini-3.7-flash" in [m["id"] for m in d["models"]]
    assert client.get("/api/llm/models?provider=x").status_code == 422


@pytest.mark.integration
def test_chat_tool_call_over_loopback(fixture_server):
    """The designer's tools go through the real HTTP API of the same server."""
    import socket
    import threading
    from contextlib import closing

    import uvicorn

    reset_store()
    paths = get_paths().ensure()
    fake = FakeProvider(call_tool="list_recipes")
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(token=TOKEN, paths=paths, base_url=base, provider_factory=lambda name: fake)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    import httpx

    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30
        ) as c:
            c.post(
                "/api/recipes",
                json={
                    "name": "r1",
                    "seeds": [fixture_server.url("/static/")],
                    "fields": [{"name": "t", "extract": {"css": "h3"}}],
                    "list": {"container": "article"},
                },
            )
            cid = c.post("/api/chats", json={}).json()["id"]
            assert c.post(f"/api/chats/{cid}/messages", json={"content": "list"}).status_code == 202
            deadline = time.time() + 20
            while time.time() < deadline:
                d = c.get(f"/api/chats/{cid}").json()
                if d["status"] != "running":
                    break
                time.sleep(0.1)
            assert d["status"] == "idle", d
            a = d["messages"][-1]
            assert a["content"].endswith("Found 1 recipes.")
            assert a["tool_calls"] == [
                {"name": "list_recipes", "input": {}, "ok": True, "summary": "1 items"}
            ]
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_fake_designer_builds_and_validates_recipe(fixture_server, monkeypatch: pytest.MonkeyPatch):
    """SA_FAKE_LLM=1: the offline designer walks fetch → save → validate through the loopback tools,
    the recipe lands in the store, `recipe_saved` is broadcast and the transcript is persisted."""
    import socket
    import threading
    from contextlib import closing

    import httpx
    import uvicorn

    monkeypatch.setenv("SA_FAKE_LLM", "1")
    reset_store()
    paths = get_paths().ensure()
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(token=TOKEN, paths=paths, base_url=base)  # default factory → fake (env)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60
        ) as c:
            assert c.get("/api/settings").json()["fake_llm"] is True
            cid = c.post("/api/chats", json={}).json()["id"]
            r = c.post(
                f"/api/chats/{cid}/messages",
                json={"content": f"Scrape {fixture_server.url('/static/')} — titles and prices"},
            )
            assert r.status_code == 202
            deadline = time.time() + 90
            while time.time() < deadline:
                d = c.get(f"/api/chats/{cid}").json()
                if d["status"] != "running":
                    break
                time.sleep(0.2)
            assert d["status"] == "idle", d
            a = d["messages"][-1]
            names = [tc["name"] for tc in a["tool_calls"]]
            assert names == ["fetch_page", "save_recipe", "validate_recipe"]
            assert all(tc["ok"] for tc in a["tool_calls"])
            assert "passes" in a["content"] and "title 100%" in a["content"]
            recipes = c.get("/api/recipes").json()
            assert (
                len(recipes) == 1
                and recipes[0]["recipe"]["list"]["container"] == "article.product_pod"
            )
            assert {f["name"] for f in recipes[0]["recipe"]["fields"]} >= {"title", "price"}
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


def test_settings_change_applies_to_new_chats(client: TestClient):
    """PUT /api/settings must reach the chat manager (and friends), not just the run manager."""
    r = client.put(
        "/api/settings",
        json={"llm": {"designer": {"provider": "gemini", "model": "gemini-3.7-flash"}}},
    )
    assert r.status_code == 200
    chat = client.post("/api/chats", json={}).json()
    assert chat["provider"] == "gemini" and chat["model"] == "gemini-3.7-flash"
    app = client.app  # type: ignore[attr-defined]
    assert app.state.chats.settings is app.state.settings
    assert app.state.scheduler.settings is app.state.settings
    assert app.state.fallback.settings is app.state.settings
