"""The dataset: what a recipe knows, as opposed to what one run saw.

Runs are episodes. Price monitoring, stock tracking and "what's new this week" all want the other
shape — one row per item, with when it first appeared, when it last changed, and to what.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store import get_store
from scrapy_awesome.store.db import reset_store
from tests.fixtures import sites
from tests.test_api_mode import TOKEN, _serve, _wait


def _recipe(recipe_id: str) -> Recipe:
    return Recipe.model_validate(
        {
            "id": recipe_id,
            "name": "ds",
            "seeds": ["https://x/"],
            "list": {"container": "li"},
            "fields": [{"name": "price", "type": "number", "extract": {"css": "b"}}],
        }
    )


def _put_run(store, recipe_id: str, run_id: str, rows: list[dict], tmp_path=None) -> None:
    """A real run row (items are foreign-keyed to runs), then its rows."""
    store.create_run(
        run_id=run_id,
        recipe=_recipe(recipe_id),
        recipe_version=1,
        kind="crawl",
        run_dir=Path(tmp_path or "/tmp") / run_id,
        token="t",
        limits={},
    )
    store.add_items(run_id, list(enumerate(rows)))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPY_AWESOME_HOME", str(tmp_path))
    reset_store()
    yield get_store()
    reset_store()


def test_a_row_carries_its_own_history(store):
    _put_run(store, "r1", "run-1", [{"_url": "/a", "price": 10}, {"_url": "/b", "price": 20}])
    first = store.fold_run_into_dataset("r1", "run-1", ["_url"])
    assert first == {"added": 2, "changed": 0, "unchanged": 0, "gone": 0}

    _put_run(store, "r1", "run-2", [{"_url": "/a", "price": 11}, {"_url": "/b", "price": 20}])
    second = store.fold_run_into_dataset("r1", "run-2", ["_url"])
    assert second == {"added": 0, "changed": 1, "unchanged": 1, "gone": 0}

    data = store.dataset("r1")
    by_url = {r["url"]: r for r in data["rows"]}
    assert data["total"] == 2
    assert by_url["/a"]["price"] == 11 and by_url["/a"]["changes"] == 1
    assert by_url["/a"]["runs"] == 2 and by_url["/a"]["last_changed"]
    assert by_url["/b"]["changes"] == 0 and by_url["/b"]["last_changed"] is None
    assert by_url["/a"]["first_seen"] <= by_url["/a"]["last_seen"]

    history = store.dataset_history("r1", "/a")
    assert len(history) == 1 and history[0]["diff"]["price"] == [10, 11]


def test_a_row_that_stops_appearing_is_marked_gone_only_by_a_full_run(store):
    _put_run(store, "r1", "run-1", [{"_url": "/a"}, {"_url": "/b"}])
    store.fold_run_into_dataset("r1", "run-1", ["_url"])

    # an incremental run visits /a only: /b was not looked at, so it is not gone
    _put_run(store, "r1", "run-2", [{"_url": "/a"}])
    assert store.fold_run_into_dataset("r1", "run-2", ["_url"], partial=True)["gone"] == 0
    assert not [r for r in store.dataset("r1")["rows"] if r["gone"]]

    # a full run that does not find /b means /b really is gone
    _put_run(store, "r1", "run-3", [{"_url": "/a"}])
    assert store.fold_run_into_dataset("r1", "run-3", ["_url"])["gone"] == 1
    gone = [r for r in store.dataset("r1")["rows"] if r["gone"]]
    assert [r["url"] for r in gone] == ["/b"]
    assert store.dataset("r1", include_gone=False)["total"] == 1

    # …and if it comes back, it is not gone any more
    _put_run(store, "r1", "run-4", [{"_url": "/a"}, {"_url": "/b"}])
    store.fold_run_into_dataset("r1", "run-4", ["_url"])
    assert store.dataset("r1", include_gone=False)["total"] == 2


def test_history_is_bounded(store):
    for n in range(store.HISTORY_MAX + 5):
        _put_run(store, "r1", f"run-{n}", [{"_url": "/a", "price": n}])
        store.fold_run_into_dataset("r1", f"run-{n}", ["_url"])
    assert len(store.dataset_history("r1", "/a")) == store.HISTORY_MAX
    assert store.dataset("r1")["rows"][0]["changes"] == store.HISTORY_MAX + 4


def test_forgetting_the_dataset_leaves_the_runs_alone(store):
    _put_run(store, "r1", "run-1", [{"_url": "/a"}])
    store.fold_run_into_dataset("r1", "run-1", ["_url"])
    assert store.forget_dataset("r1") == 1
    assert store.dataset("r1")["total"] == 0
    assert list(store.iter_items("run-1"))  # the run's own rows are untouched


@pytest.mark.integration
def test_two_runs_of_the_same_recipe_build_one_dataset(fixture_server):
    server, t, base = _serve()
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                "name": "shop",
                "seeds": [fixture_server.url("/shop/")],
                "list": {"container": "div.grid__item"},
                "fields": [
                    {"name": "title", "extract": {"css": "span.product-card__title"}},
                    {"name": "url", "type": "url", "extract": {"css": "a", "attr": "href"}},
                ],
                "dedupe_key": ["_url"],
                "limits": {"download_delay": 0.02, "max_pages": 2},
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            for _ in range(2):
                run = c.post("/api/runs", json={"recipe_id": rid}).json()
                assert _wait(c, run["id"])["status"] == "finished"

            data = c.get(f"/api/recipes/{rid}/dataset?limit=100").json()
            assert 0 < data["total"] <= len(sites.CATALOG)
            row = data["rows"][0]
            assert row["runs"] == 2 and row["changes"] == 0  # same site, second look, no change
            assert row["first_seen"] and row["last_seen"] and row["title"]

            hist = c.get(f"/api/recipes/{rid}/dataset/history?key={row['key']}").json()
            assert hist["history"] == []
            assert c.delete(f"/api/recipes/{rid}/dataset").json()["forgotten"] == data["total"]
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
