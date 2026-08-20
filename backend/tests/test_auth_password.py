"""Username + password sign-in for the UI, and what it must not break.

The token did not disappear — it moved to the machine clients (MCP server, CLI, crawl worker),
where there is nobody to type a password. These tests pin both halves.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from scrapy_awesome.api import credentials
from scrapy_awesome.api.app import create_app
from scrapy_awesome.api.auth import COOKIE_NAME, MAX_FAILURES
from scrapy_awesome.config import get_paths
from scrapy_awesome.store.db import reset_store

TOKEN = "machine-token-for-tests"
USER, PASSWORD = "scraper", "correct horse battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPY_AWESOME_HOME", str(tmp_path))  # get_paths reads it per call
    reset_store()
    paths = get_paths().ensure()
    with TestClient(create_app(token=TOKEN, paths=paths, base_url="http://test")) as c:
        yield c
    reset_store()


def test_a_fresh_machine_asks_you_to_create_a_login(client):
    status = client.get("/api/auth/status").json()
    assert status["configured"] is False and status["authenticated"] is False

    # ...and the app itself stays shut until you do
    assert client.get("/api/recipes").status_code == 401

    r = client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["username"] == USER
    assert client.cookies.get(COOKIE_NAME)  # signed in on the spot, no second round-trip
    assert client.get("/api/recipes").status_code == 200

    # setup is a first-run door, not a way to overwrite a login you cannot prove you own
    assert (
        client.post("/api/auth/setup", json={"username": "x", "password": "yyyyyyyy"}).status_code
        == 409
    )


def test_sign_in_out_and_wrong_passwords(client):
    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    client.post("/api/auth/logout")
    assert client.get("/api/recipes").status_code == 401

    assert (
        client.post("/api/auth/login", json={"username": USER, "password": "nope"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert client.get("/api/recipes").status_code == 401  # a near miss is still a miss

    r = client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200
    assert client.get("/api/recipes").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "configured": True,
        "authenticated": True,
        "username": USER,
        "locked_for": 0,
        "min_password": credentials.MIN_PASSWORD,
    }


def test_guessing_is_throttled(client):
    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    client.post("/api/auth/logout")
    for _ in range(MAX_FAILURES):
        client.post("/api/auth/login", json={"username": USER, "password": "wrong"})
    # the right password is refused too while locked — the lock is on the door, not the guess
    r = client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 429 and "try again" in r.json()["detail"]
    assert client.get("/api/auth/status").json()["locked_for"] > 0


def test_changing_the_password_needs_the_old_one_and_ends_other_sessions(client):
    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    session = dict(client.cookies)

    bad = client.post(
        "/api/auth/password", json={"current_password": "guess", "new_password": "brand new pass"}
    )
    assert bad.status_code == 403
    weak = client.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
    )
    assert weak.status_code == 422 and "at least" in weak.json()["detail"]

    ok = client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "brand new pass", "username": "owner"},
    )
    assert ok.status_code == 200 and ok.json()["username"] == "owner"
    assert client.get("/api/recipes").status_code == 200  # the browser that changed it stays in

    # a session opened before the change does not survive it
    stale = TestClient(client.app, cookies=session)
    assert stale.get("/api/recipes").status_code == 401
    assert credentials.verify("owner", "brand new pass")
    assert not credentials.verify(USER, PASSWORD)


def test_the_token_link_stops_working_once_a_password_exists(client):
    """The magic link is what people asked to stop chasing — and once there is a password, a URL
    that signs you in without it is a way around the password, not a convenience."""
    r = client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # no login set yet: still works
    client.cookies.clear()

    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    client.cookies.clear()
    r = client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert not client.cookies.get(COOKIE_NAME)
    assert client.get("/api/recipes").status_code == 401


def test_machine_clients_still_use_the_bearer_token(client):
    """MCP, the CLI and the crawl worker have no human to type a password."""
    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    client.cookies.clear()
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/recipes", headers=hdr).status_code == 200
    assert client.get("/api/recipes", headers={"Authorization": "Bearer wrong"}).status_code == 401

    # and that token can end browser sessions, which is how `scrapy-awesome passwd` does it
    assert client.post("/api/auth/revoke-sessions", headers=hdr).json()["revoked"] >= 0


def test_hashes_are_salted_scrypt_and_the_file_is_private(client, tmp_path):
    client.post("/api/auth/setup", json={"username": USER, "password": PASSWORD})
    path = tmp_path / credentials.FILENAME
    raw = path.read_text()
    assert PASSWORD not in raw and "scrypt" in raw
    assert oct(path.stat().st_mode)[-3:] == "600"

    first = credentials.load()
    time.sleep(0.01)
    credentials.save(USER, PASSWORD)
    second = credentials.load()
    assert first and second and first.salt != second.salt and first.hash != second.hash
