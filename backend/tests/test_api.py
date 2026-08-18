"""API tests. The fast ones use TestClient without spawning workers; the integration one drives a
whole crawl through the server (snapshot → preview → run → WS → export) against the fixture site."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.store.db import reset_store

TOKEN = "test-token"

RECIPE = {
    "name": "api static",
    "seeds": ["http://127.0.0.1:1/static/"],  # replaced per test
    "list": {"container": "article.product_pod"},
    "detail": {"enabled": True, "link": {"css": "h3 a"}},
    "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 5},
    "fields": [
        {"name": "title", "extract": {"css": "h3 a", "attr": "title"}, "required": True},
        {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
        {
            "name": "description",
            "scope": "detail",
            "extract": {"css": "#product_description ~ p::text"},
        },
    ],
    "limits": {"download_delay": 0.0},
}


@pytest.fixture
def client():
    reset_store()
    paths = get_paths().ensure()
    app = create_app(token=TOKEN, paths=paths, base_url="http://127.0.0.1:0")
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c
    reset_store()


def test_health_and_auth(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"]
    anon = TestClient(client.app)
    assert anon.get("/api/recipes").status_code == 401
    r = anon.get("/auth", params={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and "sa_session" in r.cookies
    anon.cookies.set("sa_session", r.cookies["sa_session"])
    assert anon.get("/api/recipes").status_code == 200
    assert anon.get("/auth", params={"token": "nope"}).status_code == 401


def test_recipe_crud_and_versions(client: TestClient):
    r = client.post("/api/recipes", json=RECIPE)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["version"] == 1
    # timestamps must carry an explicit UTC marker (SQLite drops tzinfo; browsers
    # would otherwise parse them as local time)
    assert r.json()["created_at"].endswith("Z")
    body = dict(RECIPE, name="renamed")
    r = client.put(f"/api/recipes/{rid}", json=body)
    assert r.status_code == 200 and r.json()["version"] == 2
    assert r.json()["incompatible_with_resume"] == []
    body2 = json.loads(json.dumps(body))
    body2["pagination"] = {"kind": "none"}
    r = client.put(f"/api/recipes/{rid}", json=body2)
    assert r.json()["incompatible_with_resume"] == ["pagination"]
    vs = client.get(f"/api/recipes/{rid}/versions").json()
    assert [v["version"] for v in vs] == [3, 2, 1]
    r = client.post(f"/api/recipes/{rid}/rollback/1")
    assert r.json()["version"] == 4 and r.json()["recipe"]["name"] == "api static"
    assert client.get(f"/api/recipes/{rid}/export?fmt=yaml").text.startswith("version: 1")
    bad = dict(RECIPE, fields=[{"name": "Bad Name", "extract": {"css": "a"}}])
    assert client.post("/api/recipes/validate", json=bad).json()["ok"] is False
    assert client.post("/api/recipes", json=bad).status_code == 422
    assert client.delete(f"/api/recipes/{rid}").json()["archived"] is True
    assert client.get("/api/recipes").json() == []


def test_settings_and_secrets(client: TestClient):
    r = client.get("/api/settings")
    assert (
        r.status_code == 200 and r.json()["settings"]["llm"]["designer"]["provider"] == "anthropic"
    )
    r = client.put(
        "/api/settings",
        json={"llm": {"designer": {"provider": "gemini", "model": "gemini-3.7-flash"}}},
    )
    assert r.json()["settings"]["llm"]["designer"]["provider"] == "gemini"
    assert (
        client.get("/api/settings").json()["settings"]["llm"]["designer"]["model"]
        == "gemini-3.7-flash"
    )
    r = client.put("/api/settings/secrets/gemini_api_key", json={"value": "AIzaSy-test-1234567890"})
    assert r.status_code == 200 and r.json()["source"] in ("keyring", "file")
    assert client.get("/api/settings").json()["secrets"]["gemini_api_key"]["set"] is True
    client.delete("/api/settings/secrets/gemini_api_key")
    assert client.get("/api/settings").json()["secrets"]["gemini_api_key"]["set"] is False
    assert client.put("/api/settings/secrets/nope", json={"value": "x"}).status_code == 404
    assert isinstance(client.get("/api/settings/doctor").json(), list)


@pytest.fixture
def live_server(fixture_server):
    """Real uvicorn server (thread) so worker subprocesses can POST events back."""
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
    app = create_app(token=TOKEN, paths=paths, base_url=base)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield base
    server.should_exit = True
    t.join(timeout=10)
    reset_store()


@pytest.mark.integration
def test_full_flow_through_server(live_server, fixture_server):
    import httpx
    from websockets.sync.client import connect as ws_connect

    c = httpx.Client(
        base_url=live_server, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
    )
    recipe = json.loads(json.dumps(RECIPE))
    recipe["seeds"] = [fixture_server.url("/static/")]

    # 1. samples + preview (in-process validation over engine-fetched pages)
    r = c.post("/api/preview/samples", json={"recipe": recipe})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["ok"] is True
    assert rep["fields"]["title"]["fill_rate"] == 1.0
    assert rep["fields"]["description"]["n_filled"] >= 1
    samples = r.json()["samples"]
    assert {s["kind"] for s in samples} == {"list", "detail"}
    sid = samples[0]["id"]

    a = c.post(f"/api/pages/{sid}/analyze").json()
    assert a["page_type"] == "list" and a["containers"][0]["selector"] == "article.product_pod"
    html = c.get(f"/api/pages/{sid}/render").text
    assert "<base href=" in html and "<script" not in html.lower()
    sel = c.post(f"/api/pages/{sid}/selector", json={"selector": ".price_color::text"}).json()
    assert sel["matches"] == 5 and sel["values"][0].startswith("£")
    rel = c.post(
        f"/api/pages/{sid}/selector",
        json={"selector": "h3 a", "attr": "title", "container": "article.product_pod"},
    ).json()
    assert rel["fill_rate"] == 1.0

    # 2. save recipe, start a run, watch it over WS, then export
    rid = c.post("/api/recipes", json=recipe).json()["id"]
    r = c.post("/api/runs", json={"recipe_id": rid})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    seen = {"item": 0, "done": 0, "status": []}
    ws_url = live_server.replace("http", "ws") + f"/ws/runs/{run_id}"
    with ws_connect(ws_url, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        deadline = time.time() + 120
        while time.time() < deadline:
            msg = json.loads(ws.recv(timeout=60))
            if msg["t"] == "item":
                seen["item"] += 1
            elif msg["t"] == "done":
                seen["done"] += 1
            elif msg["t"] == "status":
                seen["status"].append(msg["status"])
                if msg["status"] in ("finished", "failed", "stopped", "cancelled"):
                    break
    assert seen["done"] == 1 and seen["item"] == 15, seen
    assert seen["status"][-1] == "finished"

    run = c.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "finished" and run["items"] == 15
    items = c.get(f"/api/runs/{run_id}/items?limit=5").json()
    assert items["total"] == 15 and len(items["items"]) == 5
    assert "description" in items["items"][0]
    ev = c.get(f"/api/runs/{run_id}/events?types=page,done").json()
    assert any(e["t"] == "done" for e in ev)
    r = c.post(f"/api/runs/{run_id}/export", json={"fmt": "csv"})
    assert r.status_code == 200 and r.json()["rows"] == 15
    dl = c.get(r.json()["download"])
    assert dl.status_code == 200 and dl.text.count("\n") >= 15
    assert c.get("/api/settings/tier-memory").json() == {}
    assert c.get("/api/runs").json()[0]["id"] == run_id

    # 3. stop + resume through the API
    slow = json.loads(json.dumps(recipe))
    slow["limits"] = {"download_delay": 0.4, "concurrency_per_domain": 1}
    rid2 = c.post("/api/recipes", json=dict(slow, id="slowrecipe1")).json()["id"]
    run2 = c.post("/api/runs", json={"recipe_id": rid2}).json()["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        if c.get(f"/api/runs/{run2}/items?limit=1").json()["total"] > 0:
            break
        time.sleep(0.2)
    assert c.post(f"/api/runs/{run2}/stop").json()["status"] == "stopping"
    deadline = time.time() + 60
    while time.time() < deadline and c.get(f"/api/runs/{run2}").json()["status"] in (
        "running",
        "stopping",
    ):
        time.sleep(0.3)
    st = c.get(f"/api/runs/{run2}").json()
    assert st["status"] == "stopped", st
    n_stopped = c.get(f"/api/runs/{run2}/items?limit=1").json()["total"]
    assert 0 < n_stopped < 15
    r = c.post(f"/api/runs/{run2}/resume")
    assert r.status_code == 201 or r.status_code == 200, r.text
    deadline = time.time() + 120
    while time.time() < deadline and c.get(f"/api/runs/{run2}").json()["status"] in (
        "queued",
        "running",
        "stopping",
    ):
        time.sleep(0.3)
    final = c.get(f"/api/runs/{run2}").json()
    assert final["status"] == "finished" and final["items"] == 15, final
    titles = {i["title"] for i in c.get(f"/api/runs/{run2}/items?limit=100").json()["items"]}
    assert len(titles) == 15


@pytest.mark.integration
def test_login_session_storage_state(live_server, fixture_server, tmp_path):
    """A ready session's storage_state is applied by the interactive tier (fixture needs a cookie)."""
    import httpx

    from scrapy_awesome.store import SessionRow, get_store

    c = httpx.Client(
        base_url=live_server, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
    )
    state = tmp_path / "storage_state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "ok",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        )
    )
    store = get_store()
    store.upsert_session(
        SessionRow(
            id="sess1",
            name="fixture",
            start_url=fixture_server.url("/login/"),
            domain="127.0.0.1",
            storage_state_path=str(state),
            status="ready",
            cookies=1,
        )
    )
    assert c.get("/api/sessions").json()[0]["status"] == "ready"
    recipe = json.loads(json.dumps(RECIPE))
    recipe["id"] = "loginrecipe1"
    recipe["seeds"] = [fixture_server.url("/login/private")]
    recipe["fetch"] = {"session": "sess1"}
    recipe["detail"] = {"enabled": False}
    recipe["fields"] = recipe["fields"][:2]
    recipe["pagination"] = {"kind": "none"}
    rid = c.post("/api/recipes", json=recipe).json()["id"]
    run_id = c.post("/api/runs", json={"recipe_id": rid}).json()["id"]
    deadline = time.time() + 120
    while time.time() < deadline and c.get(f"/api/runs/{run_id}").json()["status"] in (
        "queued",
        "running",
    ):
        time.sleep(0.3)
    run = c.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "finished", run
    items = c.get(f"/api/runs/{run_id}/items").json()
    assert items["total"] == 5 and items["items"][0]["_tier"] == "interactive"


@pytest.mark.integration
def test_login_window_capture_headless(live_server, fixture_server, monkeypatch):
    """The capture subprocess writes status/storage_state (headless test mode, short timeout)."""
    import httpx

    monkeypatch.setenv("SA_LOGIN_HEADLESS", "1")
    monkeypatch.setenv("SA_LOGIN_TIMEOUT", "6")
    c = httpx.Client(
        base_url=live_server, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120
    )
    r = c.post("/api/sessions", json={"name": "fx", "url": fixture_server.url("/login/")})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    deadline = time.time() + 90
    status = "pending"
    while time.time() < deadline:
        status = c.get(f"/api/sessions/{sid}").json()["status"]
        if status in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert status == "ready", c.get(f"/api/sessions/{sid}").json()
    assert c.delete(f"/api/sessions/{sid}").json()["deleted"] is True


def test_scrapy_project_export(client: TestClient, tmp_path):
    """The standalone Scrapy project export is a valid zip with a runnable layout."""
    import io
    import zipfile

    rid = client.post("/api/recipes", json=RECIPE).json()["id"]
    r = client.get(f"/api/recipes/{rid}/export?fmt=scrapy")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    root = names[0].split("/")[0]
    assert f"{root}/scrapy.cfg" in names and f"{root}/README.md" in names
    pkg = next(n for n in names if n.endswith("/settings.py")).split("/")[1]
    assert (
        f"{root}/{pkg}/recipe.json" in names and f"{root}/{pkg}/spiders/recipe_spider.py" in names
    )
    recipe = json.loads(z.read(f"{root}/{pkg}/recipe.json"))
    assert recipe["name"] == RECIPE["name"]
    settings_src = z.read(f"{root}/{pkg}/settings.py").decode()
    assert "build_settings(" in settings_src and "load_recipe(" in settings_src
    # the generated settings module actually imports and produces Scrapy settings
    z.extractall(tmp_path)
    import importlib.util
    import sys

    sys.path.insert(0, str(tmp_path / root))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{pkg}.settings", tmp_path / root / pkg / "settings.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert pkg == mod.BOT_NAME and "DOWNLOADER_MIDDLEWARES" in mod.__dict__
    finally:
        sys.path.remove(str(tmp_path / root))
