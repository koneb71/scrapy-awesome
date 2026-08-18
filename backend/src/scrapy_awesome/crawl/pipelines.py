"""ItemsPipeline: dedupe by recipe.dedupe_key, append to <run_dir>/items.jsonl, emit `item` events,
and enforce limits.max_items (graceful close)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from scrapy import Spider
from scrapy.exceptions import DropItem, NotConfigured

logger = logging.getLogger(__name__)


class ItemsPipeline:
    def __init__(self, run_dir: Path, crawler: Any) -> None:
        self.crawler = crawler
        self.run_dir = run_dir
        self.path = run_dir / "items.jsonl"
        self._fh: Any = None
        self._seen: set[str] = set()
        self.count = 0
        self.dupes = 0

    @classmethod
    def from_crawler(cls, crawler: Any) -> ItemsPipeline:
        run_dir = crawler.settings.get("SA_RUN_DIR")
        if not run_dir:
            raise NotConfigured("SA_RUN_DIR not set")
        return cls(Path(run_dir), crawler)

    @property
    def spider(self) -> Spider:
        return self.crawler.spider

    def open_spider(self) -> None:
        spider = self.spider
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # resume-friendly: reload keys already written
        if self.path.exists():
            recipe = getattr(spider, "recipe", None)
            keys = recipe.dedupe_key if recipe else ["_url"]
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._seen.add(self._key(row, keys))
                    self.count += 1
        self._fh = self.path.open("a", encoding="utf-8")

    def close_spider(self) -> None:
        if self._fh:
            self._fh.close()
        self.crawler.stats.set_value("sa/items_written", self.count)
        self.crawler.stats.set_value("sa/items_duplicate", self.dupes)

    @staticmethod
    def _key(row: dict[str, Any], keys: list[str]) -> str:
        return json.dumps([row.get(k) for k in keys], sort_keys=True, default=str)

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        spider = self.spider
        recipe = getattr(spider, "recipe", None)
        keys = recipe.dedupe_key if recipe else ["_url"]
        k = self._key(item, keys)
        if k in self._seen and any(item.get(x) not in (None, "") for x in keys):
            self.dupes += 1
            raise DropItem(f"duplicate {k}")
        self._seen.add(k)
        self.count += 1
        self._fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        emit = getattr(spider, "emit", None)
        if callable(emit):
            emit("item", row=item, n=self.count)
        max_items = getattr(spider, "max_items", None)
        if max_items and self.count >= max_items:
            self.crawler.engine.close_spider(spider, reason="max_items")
        return item
