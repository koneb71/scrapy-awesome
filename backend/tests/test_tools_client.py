"""Fast checks for the tool client's server discovery / start lock (no processes spawned)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scrapy_awesome.config import get_paths
from scrapy_awesome.tools.client import ToolError, _Lock, ensure_server, running_server


def test_lock_reclaims_stale_and_blocks_live(tmp_path: Path):
    p = tmp_path / "server.lock"
    p.write_text("999999999")  # dead pid → stale → reclaimable
    lock = _Lock(p)
    assert lock.acquire(timeout=1) is True and p.read_text() == str(os.getpid())
    other = _Lock(p)
    assert other.acquire(timeout=0.3) is False  # live owner (us) → cannot take it
    lock.release()
    assert not p.exists()
    assert other.acquire(timeout=0.3) is True
    other.release()


def test_ensure_server_without_autostart_explains(monkeypatch: pytest.MonkeyPatch):
    paths = get_paths().ensure()
    assert running_server(paths) is None
    with pytest.raises(ToolError, match="not running"):
        ensure_server(paths, auto_start=False)
