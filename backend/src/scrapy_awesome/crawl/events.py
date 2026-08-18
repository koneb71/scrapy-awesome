"""Worker → parent event channel.

Events are dicts `{"t": <type>, "ts": <iso>, ...}`. Types: started, progress, item, blocked, page,
log, fill, done, error, snapshot. Sinks:

* FileSink   — JSONL file inside the run dir (always on; the CLI reads it, tests assert on it)
* HttpSink   — batched POST to the local server `/internal/runs/{id}/events` (Phase 2)
* MultiSink  — fan out

stdout/stderr are for logs only (Scrapy, Playwright and Chrome all write there).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class NullSink:
    def emit(self, event: dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass


class FileSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            if self._fh.closed:
                return  # late events after close (signal-handler ordering) are dropped
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._fh.close()


class HttpSink:
    """Batched, threaded POSTs so the reactor never blocks on the parent."""

    def __init__(
        self, url: str, token: str | None, *, batch: int = 100, interval: float = 0.5
    ) -> None:
        self.url = url
        self.token = token
        self.batch = batch
        self.interval = interval
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="event-sink", daemon=True)
        self._thread.start()

    def emit(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        self._q.put(event)

    def _post(self, events: list[dict[str, Any]]) -> None:
        import httpx

        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        body = json.dumps({"events": events}, ensure_ascii=False, default=str)
        for attempt in range(3):
            try:
                r = httpx.post(self.url, content=body, headers=headers, timeout=10)
                if r.status_code < 400:
                    return
                logger.warning("event sink HTTP %s: %s", r.status_code, r.text[:200])
            except Exception as exc:  # pragma: no cover - network hiccup
                logger.warning("event sink post failed (%s): %s", attempt, exc)
            time.sleep(0.5 * (attempt + 1))

    def _loop(self) -> None:
        buf: list[dict[str, Any]] = []
        last = time.monotonic()
        while True:
            try:
                ev = self._q.get(timeout=self.interval)
            except queue.Empty:
                ev = ...  # type: ignore[assignment]
            if ev is None:
                if buf:
                    self._post(buf)
                return
            if ev is not ...:
                buf.append(ev)  # type: ignore[arg-type]
            if buf and (len(buf) >= self.batch or time.monotonic() - last >= self.interval):
                self._post(buf)
                buf = []
                last = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._thread.join(timeout=15)


class MultiSink:
    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = [s for s in sinks if s is not None]

    def emit(self, event: dict[str, Any]) -> None:
        for s in self.sinks:
            s.emit(event)

    def close(self) -> None:
        for s in self.sinks:
            s.close()


class Emitter:
    """Attached to the spider as `spider.emit(kind, **data)`. Adds ts/run_id, counts per type."""

    def __init__(self, sink: EventSink, run_id: str) -> None:
        self.sink = sink
        self.run_id = run_id
        self.counts: dict[str, int] = {}

    def __call__(self, event_type: str, /, **data: Any) -> None:
        self.counts[event_type] = self.counts.get(event_type, 0) + 1
        self.sink.emit({"t": event_type, "ts": _now(), "run_id": self.run_id, **data})

    def close(self) -> None:
        self.sink.close()


def make_sink(*, events_file: Path | None, events_url: str | None, token: str | None) -> EventSink:
    sinks: list[EventSink] = []
    if events_file:
        sinks.append(FileSink(events_file))
    if events_url:
        sinks.append(HttpSink(events_url, token))
    if not sinks:
        return NullSink()
    return sinks[0] if len(sinks) == 1 else MultiSink(*sinks)
