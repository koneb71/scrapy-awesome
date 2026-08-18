"""In-process pub/sub for live events (worker → server → WebSocket clients). Thread-safe publish."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import defaultdict
from typing import Any

MAX_QUEUE = 5000


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            self._subs[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs[topic].discard(q)
            if not self._subs[topic]:
                self._subs.pop(topic, None)

    def _deliver(self, topic: str, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subs.get(topic, ())) + list(self._subs.get("*", ()))
        for q in targets:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop oldest
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        """Safe from any thread."""
        loop = self._loop
        if loop is None:
            self._deliver(topic, event)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(topic, event)
        else:
            loop.call_soon_threadsafe(self._deliver, topic, event)

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, ()))
