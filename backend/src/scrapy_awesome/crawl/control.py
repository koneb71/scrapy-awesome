"""ControlExtension: graceful stop via a control file (or, in Phase 2, the server), plus periodic
progress events. Cross-platform — no signals involved (SIGTERM doesn't exist on Windows).

Control file: `<run_dir>/control.json` → `{"cmd": "stop"}`. Checked once per second.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from scrapy import Spider, signals
from scrapy.exceptions import NotConfigured
from twisted.internet import task

logger = logging.getLogger(__name__)


class ControlExtension:
    def __init__(self, crawler: Any) -> None:
        run_dir = crawler.settings.get("SA_RUN_DIR")
        if not run_dir:
            raise NotConfigured("SA_RUN_DIR not set")
        self.crawler = crawler
        self.control_file = Path(run_dir) / "control.json"
        self.control_url: str | None = crawler.settings.get("SA_CONTROL_URL")
        self.control_token: str | None = crawler.settings.get("SA_CONTROL_TOKEN")
        self.progress_every = float(crawler.settings.getfloat("SA_PROGRESS_INTERVAL", 2.0))
        self._loop: task.LoopingCall | None = None
        self._last_progress = 0.0
        self._stopping = False

    @classmethod
    def from_crawler(cls, crawler: Any) -> ControlExtension:
        ext = cls(crawler)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_opened(self, spider: Spider) -> None:
        self._loop = task.LoopingCall(self._tick, spider)
        self._loop.start(1.0, now=False)

    def spider_closed(self, spider: Spider, reason: str) -> None:
        if self._loop and self._loop.running:
            self._loop.stop()
        self._emit_progress(spider, final=True, reason=reason)

    # ---- tick -----------------------------------------------------------------------------
    def _tick(self, spider: Spider) -> None:
        try:
            cmd = self._read_command()
            if cmd == "stop" and not self._stopping:
                self._stopping = True
                logger.info("control: stop requested — closing spider gracefully")
                emit = getattr(spider, "emit", None)
                if callable(emit):
                    emit("log", level="info", msg="stop requested")
                self.crawler.engine.close_spider(spider, reason="stopped")
            now = time.monotonic()
            if now - self._last_progress >= self.progress_every:
                self._last_progress = now
                self._emit_progress(spider)
        except Exception:  # pragma: no cover - never let the tick kill the loop
            logger.debug("control tick failed", exc_info=True)

    def _read_command(self) -> str | None:
        if self.control_file.exists():
            try:
                data = json.loads(self.control_file.read_text() or "{}")
                return data.get("cmd")
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _emit_progress(
        self, spider: Spider, *, final: bool = False, reason: str | None = None
    ) -> None:
        emit = getattr(spider, "emit", None)
        if not callable(emit):
            return
        st = self.crawler.stats.get_stats() or {}
        emit(
            "progress",
            pages=int(st.get("response_received_count", 0)),
            items=int(st.get("item_scraped_count", 0)),
            requests=int(st.get("downloader/request_count", 0)),
            errors=int(st.get("log_count/ERROR", 0)),
            blocked=sum(v for k, v in st.items() if k.startswith("sa/blocked/")),
            escalations=sum(v for k, v in st.items() if k.startswith("sa/escalations/")),
            tiers={
                k.split("/")[2]: v
                for k, v in st.items()
                if k.startswith("sa/tier/") and k.endswith("/responses")
            },
            final=final,
            reason=reason,
        )
