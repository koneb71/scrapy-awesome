from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.server import FixtureServer


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[FixtureServer]:
    with FixtureServer() as srv:
        yield srv


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep tests away from the user's real data dir/keychain."""
    home = tmp_path_factory.mktemp("sa-home")
    monkeypatch.setenv("SCRAPY_AWESOME_HOME", str(home))
    yield


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    return d


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests unless explicitly selected with `-m integration` (or SA_INTEGRATION=1)."""
    if config.getoption("-m") and "integration" in str(config.getoption("-m")):
        return
    if os.environ.get("SA_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test — run with `-m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
