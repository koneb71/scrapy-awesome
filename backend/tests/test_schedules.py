"""Schedules: trigger math, validation, API; scheduler tick → run → diff → notify (integration)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.scheduler.diff import diff_rows, summary_line
from scrapy_awesome.scheduler.service import compute_next, describe, validate_schedule
from scrapy_awesome.store import ScheduleRow
from scrapy_awesome.store.db import reset_store

TOKEN = "sched-test"


def test_diff_rows_added_removed_changed():
    old = [
        {"_url": "u1", "title": "A", "price": 1},
        {"_url": "u2", "title": "B", "price": 2},
        {"_url": "u3", "title": "C", "price": 3},
    ]
    new = [
        {"_url": "u1", "title": "A", "price": 1},
        {"_url": "u2", "title": "B", "price": 2.5, "_fetched_at": "x"},
        {"_url": "u4", "title": "D", "price": 4},
    ]
    d = diff_rows(old, new)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["samples"]["changed"][0]["fields"] == {"price": {"old": 2, "new": 2.5}}
    assert d["samples"]["added"][0]["_url"] == "u4" and d["samples"]["removed"][0]["_url"] == "u3"
    assert summary_line(d) == "+1 new · −1 gone · ~1 changed · 3 total"
    # composite key
    d2 = diff_rows([{"a": 1, "b": 1, "v": 1}], [{"a": 1, "b": 2, "v": 1}], keys=["a", "b"])
    assert d2["added"] == 1 and d2["removed"] == 1


def test_compute_next_cron_and_interval():
    base = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    cron = ScheduleRow(id="s", recipe_id="r", kind="cron", cron="0 6 * * *", timezone="UTC")
    nxt = compute_next(cron, base)
    assert nxt == datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    every = ScheduleRow(id="s2", recipe_id="r", kind="interval", every_seconds=3600, timezone="UTC")
    assert compute_next(every, base) == base + timedelta(hours=1)
    assert (
        compute_next(ScheduleRow(id="x", recipe_id="r", kind="interval", every_seconds=10), base)
        is None
    )
    assert describe(every) == "every 1 hour(s)" and describe(cron).startswith("cron 0 6 * * *")
    assert validate_schedule("cron", "not a cron", None, None)
    assert validate_schedule("cron", "*/15 * * * *", None, "Europe/Berlin") is None
    assert validate_schedule("interval", None, 30, None)
    assert validate_schedule("cron", "0 6 * * *", None, "Mars/Olympus")


@pytest.fixture
def client():
    reset_store()
    app = create_app(token=TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        yield c
    reset_store()


def test_schedule_api_crud(client: TestClient):
    r = client.post(
        "/api/recipes",
        json={
            "name": "s",
            "seeds": ["http://x/"],
            "list": {"container": "li"},
            "fields": [{"name": "t", "extract": {"css": "a"}}],
        },
    )
    rid = r.json()["id"]
    assert (
        client.post(
            "/api/schedules", json={"recipe_id": "nope", "kind": "cron", "cron": "0 6 * * *"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/schedules", json={"recipe_id": rid, "kind": "cron", "cron": "bad"}
        ).status_code
        == 422
    )
    r = client.post(
        "/api/schedules",
        json={
            "recipe_id": rid,
            "name": "nightly",
            "kind": "cron",
            "cron": "0 6 * * *",
            "timezone": "UTC",
            "max_pages": 3,
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()
    assert (
        s["enabled"] and s["next_run_at"].endswith("Z") and s["describe"] == "cron 0 6 * * * (UTC)"
    )
    sid = s["id"]
    assert [x["id"] for x in client.get(f"/api/schedules?recipe_id={rid}").json()] == [sid]
    p = client.patch(f"/api/schedules/{sid}", json={"enabled": False}).json()
    assert p["enabled"] is False and p["next_run_at"] is None
    p = client.patch(
        f"/api/schedules/{sid}", json={"kind": "interval", "every_seconds": 900, "enabled": True}
    ).json()
    assert p["describe"] == "every 15 min" and p["next_run_at"]
    assert client.patch(f"/api/schedules/{sid}", json={"every_seconds": 5}).status_code == 422
    assert client.delete(f"/api/schedules/{sid}").json()["deleted"] is True
    assert client.get(f"/api/schedules/{sid}").status_code == 404


@pytest.mark.integration
def test_scheduler_runs_diffs_and_notifies(fixture_server):
    """A due schedule starts a crawl at tick; when it finishes the run is diffed against the
    previous finished run of the recipe and a `notify` event reaches the firehose."""
    import socket
    import threading
    from contextlib import closing

    import uvicorn
    from websockets.sync.client import connect as ws_connect

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
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=60) as c:
            recipe = {
                "name": "sched fixture",
                "seeds": [fixture_server.url("/static/")],
                "list": {"container": "article.product_pod"},
                "pagination": {"kind": "next_link", "selector": "li.next a", "max_pages": 1},
                "fields": [
                    {"name": "title", "extract": {"css": "h3 a", "attr": "title"}},
                    {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
                ],
                "limits": {"download_delay": 0.05},
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]

            def wait_done(run_id: str) -> dict:
                dl = time.time() + 90
                while time.time() < dl:
                    r = c.get(f"/api/runs/{run_id}").json()
                    if r["status"] in ("finished", "failed", "stopped", "cancelled"):
                        return r
                    time.sleep(0.3)
                raise AssertionError("run did not finish")

            # a first (manual) run to diff against
            first = c.post("/api/runs", json={"recipe_id": rid, "max_pages": 1}).json()
            r1 = wait_done(first["id"])
            assert r1["status"] == "finished" and r1["items"] == 5

            # schedule that is already due (next_run_at in the past → catch-up on tick)
            s = c.post(
                "/api/schedules",
                json={"recipe_id": rid, "kind": "interval", "every_seconds": 3600, "max_pages": 1},
            ).json()
            sid = s["id"]
            past = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
            # (poke the stored next_run_at back in time via the store — same DB, same process)
            app.state.store.update_schedule(sid, next_run_at=datetime.fromisoformat(past))

            with ws_connect(f"ws://127.0.0.1:{port}/ws/events", additional_headers=hdr) as ws:
                started = c.post("/api/schedules/tick").json()["started"]
                assert len(started) == 1
                run_id = started[0]
                r2 = wait_done(run_id)
                assert r2["status"] == "finished" and r2["schedule_id"] == sid
                # the diff is written by the finish hook right after the status flips → poll
                dl = time.time() + 15
                while time.time() < dl and "diff" not in (r2.get("stats") or {}):
                    time.sleep(0.2)
                    r2 = c.get(f"/api/runs/{run_id}").json()
                assert r2["stats"]["diff"]["against_run_id"] == first["id"]
                assert r2["stats"]["diff"]["unchanged"] == 5 and r2["stats"]["diff"]["added"] == 0
                # notification on the firehose
                notified = None
                dl = time.time() + 20
                while time.time() < dl and notified is None:
                    ev = json.loads(ws.recv(timeout=20))
                    if ev.get("t") == "notify" and ev.get("run_id") == run_id:
                        notified = ev
                assert (
                    notified
                    and "Scheduled run finished" in notified["title"]
                    and "+0 new" in notified["body"]
                )

            sched = c.get(f"/api/schedules/{sid}").json()
            assert sched["last_run_id"] == run_id and sched["last_status"] == "finished"
            assert sched["last_diff"]["unchanged"] == 5
            # next_run_at moved into the future (coalesced from now)
            assert datetime.fromisoformat(
                sched["next_run_at"].replace("Z", "+00:00")
            ) > datetime.now(UTC)
            # a second tick does nothing (not due; and diff endpoint works on demand)
            assert c.post("/api/schedules/tick").json()["started"] == []
            d = c.get(f"/api/runs/{run_id}/diff").json()
            assert d["against_run_id"] == first["id"] and d["diff"]["unchanged"] == 5
            # run-now bypasses the timer; recipe already running → serialization skips ticks
            now_run = c.post(f"/api/schedules/{sid}/run").json()
            assert now_run["status"] in ("queued", "running")
            wait_done(now_run["id"])
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


def test_prune_keeps_newest_and_active(client: TestClient):
    """Retention: keep N newest runs per recipe (never active ones), N newest samples per recipe."""
    from pathlib import Path

    from scrapy_awesome.recipe.models import Recipe

    store = client.app.state.store  # type: ignore[attr-defined]
    rec = Recipe.model_validate(
        {
            "name": "p",
            "seeds": ["http://x/"],
            "list": {"container": "li"},
            "fields": [{"name": "t", "extract": {"css": "a"}}],
        }
    )
    store.save_recipe(rec)
    ids = []
    for i in range(5):
        rid = f"r{i}"
        d = Path(get_paths().runs) / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "x").write_text("x")
        store.create_run(
            run_id=rid, recipe=rec, recipe_version=1, kind="crawl", run_dir=d, token="t", limits={}
        )
        store.update_run(rid, status="running" if i == 4 else "finished")
        store.add_items(rid, [(1, {"_url": "u", "t": "a"})])
        ids.append(rid)
        time.sleep(0.01)
    out = store.prune(keep_runs_per_recipe=2, keep_samples_per_recipe=5, keep_days=90)
    assert out["runs"] == 2  # r0, r1 deleted; r2, r3 kept (2 newest finished); r4 running kept
    left = {r.id for r in store.list_runs()}
    assert left == {"r2", "r3", "r4"}
    assert not (Path(get_paths().runs) / "r0").exists()
    assert store.count_items("r0") == 0 and store.count_items("r3") == 1
    assert client.post("/api/settings/prune").json()["runs"] == 0
    assert client.get("/api/settings/storage").json()["runs"] == 3
