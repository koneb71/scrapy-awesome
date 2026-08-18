"""Agent-facing page views (outline / markdown / search) and pick-request hand-offs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scrapy_awesome.api.app import create_app
from scrapy_awesome.snapshot.fold import fold_html
from scrapy_awesome.snapshot.markdown import to_markdown
from scrapy_awesome.snapshot.search import search_text
from scrapy_awesome.store.db import reset_store

HTML = (
    """<!doctype html><html><head><title>Shop – page 1</title>
<meta name="description" content="Widgets for sale"><script>var x=1</script>
<script type="application/ld+json">{"@type":"ItemList"}</script></head>
<body><nav class="top"><a href="/">Home</a></nav>
<section id="results">
"""
    + "".join(
        f'<article class="card item-{i}" data-id="{i}"><h3><a href="/item/{i}" title="Widget {i:02d}">Widget {i:02d}</a></h3>'
        f'<p class="price">£{10 + i}.50</p><p class="stock">{"In stock" if i % 4 else "Out of stock"}</p></article>'
        for i in range(1, 13)
    )
    + """</section>
<ul class="pager"><li class="current">Page 1 of 3</li><li class="next"><a href="/?page=2">next</a></li></ul>
<footer><p>"""
    + "lorem ipsum " * 40
    + """</p></footer></body></html>"""
)


def test_fold_collapses_siblings_and_truncates_text():
    out = fold_html(HTML, keep_siblings=2, text_limit=40)
    assert "<title> Shop – page 1" in out
    assert 'meta description="Widgets for sale"' in out
    assert "ld+json" in out
    assert "<script" not in out and "var x" not in out
    assert out.count('data-id="') == 2  # first two items shown …
    assert "+10 more <article.card> siblings" in out  # … the rest collapsed
    assert "…" in out  # long footer text truncated
    assert "<li.next>" in out and 'href="/?page=2"' in out
    small = fold_html(HTML, max_chars=300)
    assert len(small) <= 400 and "truncated" in small


def test_markdown_fit_and_full():
    fit = to_markdown(HTML, "http://x/", fit=True)
    full = to_markdown(HTML, "http://x/", fit=False)
    assert "Widget 01" in fit or "Widget 01" in full
    assert "£11.50" in full
    assert "var x" not in full
    assert len(to_markdown(HTML, fit=False, max_chars=1000)) <= 1030


def test_search_text_relative_to_container():
    m = search_text(HTML, "£11.50", container="article.card")
    assert m and m[0]["in_container"] is True
    assert m[0]["relative_css"] == "p.price"
    assert m[0]["container_items"] == 12 and m[0]["container_fill"] == 12
    # attribute hits
    a = search_text(HTML, "/item/2", container="article.card")
    assert a and a[0]["attr"] == "href" and a[0]["relative_css"].endswith("a")
    assert search_text(HTML, "does-not-exist") == []
    # without a container: plain page-level css path
    n = search_text(HTML, "Page 1 of 3")
    assert n and n[0]["css"].endswith("li.current") and "in_container" not in n[0]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    reset_store()
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    app = create_app(token="t")
    with TestClient(app, headers={"Authorization": "Bearer t"}) as c:
        yield c


def test_pick_request_roundtrip(client: TestClient):
    r = client.post("/api/picks", json={"prompt": "click the price", "field_name": "price"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get(f"/api/picks/{pid}?wait=0.2").json()["status"] == "pending"
    assert [p["id"] for p in client.get("/api/picks?status=pending").json()] == [pid]
    # a second request supersedes the first
    r2 = client.post("/api/picks", json={"prompt": "click the title"})
    assert client.get(f"/api/picks/{pid}").json()["status"] == "cancelled"
    pid2 = r2.json()["id"]
    bad = client.post(f"/api/picks/{pid2}/answer", json={"examples": []})
    assert bad.status_code == 422
    ok = client.post(
        f"/api/picks/{pid2}/answer",
        json={
            "relative_selector": "h3 a",
            "container": "article.card",
            "examples": ["Widget 01"],
            "matches": 12,
        },
    )
    assert ok.json()["status"] == "answered" and ok.json()["answer"]["relative_selector"] == "h3 a"
    assert client.post(f"/api/picks/{pid2}/answer", json={"cancelled": True}).status_code == 409
    o = client.post("/api/ui/open", json={"route": "recipes"}).json()
    assert o["route"] == "/recipes" and o["opened"] is True
