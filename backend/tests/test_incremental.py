"""Re-running only what changed.

Two ways a site can say "don't bother": a sitemap `lastmod` we have already crawled (no request at
all), and a `304 Not Modified` answer to a conditional request (a round trip, no body, no parse).
"""

from __future__ import annotations

import httpx
import pytest

from scrapy_awesome.scheduler.diff import diff_rows
from scrapy_awesome.store import get_store
from scrapy_awesome.store.db import reset_store
from tests.test_api_mode import TOKEN, _serve, _wait

RECIPE = {
    "name": "incremental",
    "page_type": "single",
    "fields": [{"name": "title", "scope": "page", "extract": {"css": "h1"}, "required": True}],
    "limits": {"download_delay": 0.02, "max_pages": 20, "max_items": 100},
    "incremental": {"enabled": True},
}


def test_a_partial_run_reports_nothing_as_gone():
    """An incremental run does not fetch unchanged pages, so their rows are missing from it. Read
    naively that is "everything disappeared" — the diff has to know the difference."""
    old = [{"_url": "/a", "price": 1}, {"_url": "/b", "price": 2}]
    new = [{"_url": "/a", "price": 3}]  # /b was skipped, not deleted

    full = diff_rows(old, new)
    assert full["removed"] == 1 and full["changed"] == 1 and full["partial"] is False

    partial = diff_rows(old, new, partial=True)
    assert partial["removed"] == 0 and partial["changed"] == 1 and partial["partial"] is True
    assert partial["added"] == 0


@pytest.mark.integration
def test_the_second_run_skips_what_the_site_says_is_unchanged(fixture_server):
    server, t, base = _serve()
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                **RECIPE,
                "seeds": [fixture_server.url("/etag/")],
                "source": {
                    "kind": "sitemap",
                    "sitemap": fixture_server.url("/etag/sitemap.xml"),
                    "max_urls": 5,
                },
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]

            first = c.post("/api/runs", json={"recipe_id": rid}).json()
            d1 = _wait(c, first["id"])
            assert d1["status"] == "finished" and d1["items"] == 5
            assert (d1["stats"] or {}).get("skipped", 0) == 0  # nothing to skip on a first run
            assert (d1["stats"] or {}).get("page_state") == 5  # …but everything is remembered

            second = c.post("/api/runs", json={"recipe_id": rid}).json()
            d2 = _wait(c, second["id"])
            assert d2["status"] == "finished"
            # every URL's <lastmod> is unchanged, so the second run does not fetch a single page
            assert d2["stats"]["skipped"] == 5
            assert d2["items"] == 0
            pages = c.get(f"/api/runs/{second['id']}/events?types=page&tail=20").json()
            assert [e["kind"] for e in pages] == ["sitemap"]  # the sitemap, and nothing else
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()


@pytest.mark.integration
def test_a_conditional_request_turns_a_page_into_a_304(fixture_server):
    """Without a sitemap there is no `lastmod`, so the page is asked for — with the ETag we were
    given last time, which the server answers with 304 and no body."""
    server, t, base = _serve()
    urls = [fixture_server.url(f"/etag/item/{i}") for i in (1, 2, 3)]
    try:
        with httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300
        ) as c:
            recipe = {
                **RECIPE,
                "seeds": [fixture_server.url("/etag/")],
                "source": {"kind": "urls", "urls": urls},
            }
            rid = c.post("/api/recipes", json=recipe).json()["id"]
            d1 = _wait(c, c.post("/api/runs", json={"recipe_id": rid}).json()["id"])
            assert d1["items"] == 3

            state = get_store().page_state(rid)
            assert all(s["etag"] for s in state.values()), state  # ETags were kept

            run2 = c.post("/api/runs", json={"recipe_id": rid}).json()
            d2 = _wait(c, run2["id"])
            assert d2["status"] == "finished"
            assert d2["stats"]["skipped"] == 3 and d2["items"] == 0
            kinds = [
                e["kind"] for e in c.get(f"/api/runs/{run2['id']}/events?types=page&tail=20").json()
            ]
            assert kinds == ["unchanged"] * 3  # asked, answered 304, not parsed
    finally:
        server.should_exit = True
        t.join(timeout=10)
        reset_store()
