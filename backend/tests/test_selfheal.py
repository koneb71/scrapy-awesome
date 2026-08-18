"""Self-healing selectors: fingerprints (unit) and a run that heals after a 'redesign' (integration)."""

from __future__ import annotations

import time

import httpx
import pytest
from parsel import Selector

from scrapy_awesome.api.app import create_app
from scrapy_awesome.config import get_paths
from scrapy_awesome.extract.engine import extract_list_items, select_containers
from scrapy_awesome.extract.fingerprint import (
    compute_fingerprints,
    find_heal,
    heal_field,
    relocate,
    similarity,
    value_shape,
)
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store.db import reset_store
from tests.fixtures import sites

TOKEN = "heal-test"


def _recipe(seed: str = "http://x/static/") -> Recipe:
    return Recipe.model_validate(
        {
            "name": "heal",
            "seeds": [seed],
            "list": {"container": "article.product_pod"},
            "fields": [
                {"name": "title", "extract": {"css": "h3 a", "attr": "title"}, "required": True},
                {"name": "price", "type": "price", "extract": {"css": ".price_color::text"}},
                {"name": "availability", "extract": {"css": "p.availability::text"}},
            ],
        }
    )


def test_value_shape_and_similarity():
    assert value_shape("£11.50") == "£0.0" and value_shape("In stock") == "a a"
    assert value_shape("2026-08-18") == "0-0-0"
    a = {
        "tag": "p",
        "classes": ["price"],
        "attrs": {},
        "path": ["p"],
        "text": {"len": 1, "digits": True, "currency": True},
        "shape": "£0.0",
        "leaf": True,
    }
    b = dict(a)
    assert similarity(a, b) >= 0.9
    c = {**a, "tag": "span", "classes": ["amount"], "path": ["div", "span"]}
    assert 0.4 < similarity(a, c) < 0.6  # content-only evidence


def test_fingerprints_relocate_after_redesign():
    old_html = sites.list_page(1, "/static")
    new_html = sites.list_page(1, "/redesign", redesigned=True)
    rec = _recipe()
    fps = compute_fingerprints(rec, old_html, "http://x/static/")
    assert set(fps) == {"title", "price", "availability"}
    assert fps["price"]["classes"] == ["price_color"] and fps["price"]["shape"] == "£0.0"
    # the old selectors fail on the redesigned page
    items, _ = extract_list_items(rec, new_html, "http://x/redesign/")
    assert len(items) == 5 and not any(it.values.get("price") for it in items)
    sel = Selector(text=new_html)
    nodes, _ = select_containers(sel, "article.product_pod", [], None)
    price = relocate(nodes, fps["price"])
    assert price and price[0].selector == "span.amount" and price[0].fill == 1.0
    title = find_heal(nodes, next(f for f in rec.fields if f.name == "title"), fps["title"])
    assert title is not None and title.attr is None and title.fill == 1.0
    avail = find_heal(
        nodes, next(f for f in rec.fields if f.name == "availability"), fps["availability"]
    )
    assert avail is not None and avail.selector.startswith("p.stock")
    # apply the heals: everything fills again
    healed = list(rec.fields)
    for name, cand in (("price", price[0]), ("title", title), ("availability", avail)):
        i = next(i for i, f in enumerate(healed) if f.name == name)
        healed[i] = heal_field(healed[i], cand.selector, cand.attr)
    rec.fields = healed
    items, _ = extract_list_items(rec, new_html, "http://x/redesign/")
    assert all(
        it.values.get("price") and it.values.get("title") and it.values.get("availability")
        for it in items
    )
    assert items[0].values["price"] == 11.5 and items[0].values["title"] == "Widget 01"
    # old primary kept as an alternate → the *old* markup still works with the healed recipe
    items_old, _ = extract_list_items(rec, old_html, "http://x/static/")
    assert all(it.values.get("price") for it in items_old)


@pytest.mark.integration
def test_run_heals_after_redesign(fixture_server):
    """Validate on /static/ (fingerprints stored on the recipe row), then crawl /redesign/:
    the worker relocates the fields, emits `healed`, and rows come out filled."""
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
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=hdr, timeout=90) as c:
            rec = _recipe(fixture_server.url("/static/")).to_dict()
            rec["limits"] = {"download_delay": 0.05, "max_pages": 1}
            rid = c.post("/api/recipes", json=rec).json()["id"]
            rec["id"] = rid
            # design-time validation → fingerprints remembered on the recipe row
            r = c.post(
                "/api/preview/samples", json={"recipe": rec, "with_page2": False, "detail_pages": 0}
            )
            assert r.status_code == 200, r.text
            assert set(r.json()["fingerprints"]) == {"title", "price", "availability"}
            # the site "redesigns": point the recipe at the new markup and run
            rec["seeds"] = [fixture_server.url("/redesign/")]
            c.put(f"/api/recipes/{rid}", json=rec)
            run = c.post("/api/runs", json={"recipe_id": rid, "max_pages": 1}).json()
            dl = time.time() + 90
            while time.time() < dl:
                d = c.get(f"/api/runs/{run['id']}").json()
                if d["status"] in ("finished", "failed", "stopped", "cancelled"):
                    break
                time.sleep(0.3)
            assert d["status"] == "finished", d
            healed = d["stats"].get("healed") or []
            assert {h["field"] for h in healed} == {"title", "price", "availability"}, healed
            price = next(h for h in healed if h["field"] == "price")
            assert price["new"]["css"] == "span.amount" and price["fill"] == 1.0
            rows = c.get(f"/api/runs/{run['id']}/items?limit=10").json()["items"]
            assert len(rows) == 5 and all(r["price"] and r["title"] for r in rows)
            evs = c.get(f"/api/runs/{run['id']}/events?types=healed&tail=10").json()
            assert len(evs) == 3
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
