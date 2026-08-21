"""RunManager: owns worker subprocesses (crawls + snapshot jobs), ingests their events, persists
items in batches, publishes live events to the bus, and tracks per-domain tier memory."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scrapy_awesome.api.auth import AuthState
from scrapy_awesome.api.bus import EventBus
from scrapy_awesome.config import Paths, UserSettings
from scrapy_awesome.crawl.runner import common_worker_args, new_run_id, prepare_run_dir, worker_cmd
from scrapy_awesome.recipe.io import save_recipe
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store import RunRow, SampleRow, Store

logger = logging.getLogger(__name__)


def secrets_hex() -> str:
    import secrets

    return secrets.token_hex(3)


TERMINAL = {"finished", "failed", "stopped", "cancelled"}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunHandle:
    run_id: str
    run_dir: Path
    proc: asyncio.subprocess.Process
    kind: str
    task: asyncio.Task | None = None
    item_buffer: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    last_flush: float = field(default_factory=time.monotonic)
    counters: dict[str, Any] = field(default_factory=dict)
    last_counter_write: float = 0.0
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    healed: list[dict[str, Any]] = field(default_factory=list)  # `healed` events (self-heal)
    fills: dict[str, list[float]] = field(default_factory=dict)  # field → per-page fill rates
    fill_alerted: set[str] = field(default_factory=set)


class RunManager:
    def __init__(
        self,
        *,
        store: Store,
        bus: EventBus,
        auth: AuthState,
        paths: Paths,
        settings: UserSettings,
        base_url: str,
    ) -> None:
        self.store = store
        self.bus = bus
        self.auth = auth
        self.paths = paths
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        self.active: dict[str, RunHandle] = {}
        self._flusher: asyncio.Task | None = None
        # async callbacks (RunRow) → None, invoked after a crawl's final status is stored
        self.on_finished: list[Any] = []
        # sync callbacks (FailedPageRow) → None, invoked when a worker reports a page it couldn't extract
        self.on_page_failed: list[Any] = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._flusher = asyncio.create_task(self._flush_loop(), name="item-flusher")
        # runs left "running" by a previous server process are stale
        for row in self.store.list_runs(limit=500):
            if row.status in ("queued", "running", "stopping"):
                self.store.update_run(
                    row.id, status="failed", reason="server_restarted", finished_at=_now()
                )

    async def shutdown(self) -> None:
        if self._flusher:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
        for h in list(self.active.values()):
            with contextlib.suppress(ProcessLookupError):
                h.proc.terminate()
        for h in list(self.active.values()):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(h.proc.wait(), timeout=10)
            self._flush(h, force=True)

    @property
    def active_crawls(self) -> int:
        return sum(1 for h in self.active.values() if h.kind == "crawl")

    # ------------------------------------------------------------------ helpers
    def _storage_state_for(self, recipe: Recipe) -> str | None:
        sid = recipe.fetch.session
        if not sid:
            return None
        row = self.store.get_session(sid)
        if row and row.status == "ready" and Path(row.storage_state_path).exists():
            self.store.upsert_session(row.model_copy(update={"last_used_at": _now()}))
            return row.storage_state_path
        logger.warning("session %s not ready; running without storage_state", sid)
        return None

    async def _spawn(self, args: list[str], run_dir: Path) -> asyncio.subprocess.Process:
        run_dir.mkdir(parents=True, exist_ok=True)
        log = (run_dir / "worker.log").open("ab")
        try:
            return await asyncio.create_subprocess_exec(
                *worker_cmd(),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(run_dir),
            )
        finally:
            log.close()

    # ------------------------------------------------------------------ crawls
    async def start_crawl(
        self,
        recipe: Recipe,
        *,
        recipe_version: int | None,
        max_pages: int | None = None,
        max_items: int | None = None,
        tier: str | None = None,
        resume_run_id: str | None = None,
        headed: bool = False,
        httpcache: bool = False,
        schedule_id: str | None = None,
    ) -> RunRow:
        if self.active_crawls >= self.settings.max_concurrent_runs and not resume_run_id:
            raise RuntimeError(f"max concurrent runs ({self.settings.max_concurrent_runs}) reached")
        if resume_run_id:
            row = self.store.get_run(resume_run_id)
            if row is None:
                raise KeyError(resume_run_id)
            run_id, run_dir = row.id, Path(row.run_dir)
            token = self.auth.new_run_token(run_id)
            self.store.update_run(run_id, status="queued", token=token, reason=None, error=None)
            prepare_run_dir(recipe, run_dir)  # refresh recipe.json (resume-compatible edits)
            (run_dir / "control.json").unlink(missing_ok=True)
        else:
            run_id = new_run_id()
            run_dir = self.paths.runs / run_id
            token = self.auth.new_run_token(run_id)
            prepare_run_dir(recipe, run_dir)
            self.store.create_run(
                run_id=run_id,
                recipe=recipe,
                recipe_version=recipe_version,
                kind="crawl",
                run_dir=run_dir,
                token=token,
                limits={"max_pages": max_pages, "max_items": max_items, "tier": tier},
            )
            if schedule_id:
                self.store.update_run(run_id, schedule_id=schedule_id)
        args = [
            "crawl",
            "--recipe",
            str(run_dir / "recipe.json"),
            *common_worker_args(
                run_id=run_id,
                run_dir=run_dir,
                tier=tier,
                headed=headed,
                storage_state=self._storage_state_for(recipe),
                events_url=f"{self.base_url}/internal/runs/{run_id}/events",
                events_token=token,
                control_url=None,
                obey_robots=self.settings.crawl.obey_robots,
                httpcache=httpcache,
                chrome=self.settings.crawl.chrome_executable_path,
                proxies=self.settings.crawl.proxies,
                tier_memory=self.store.tier_memory(),
                log_level="INFO",
            ),
            "--resume",  # always JOBDIR-backed so any run can be stopped and resumed
        ]
        if max_pages is not None:
            args += ["--max-pages", str(max_pages)]
        if max_items is not None:
            args += ["--max-items", str(max_items)]
        if recipe.incremental.enabled and recipe.id:
            # Hand the worker what the last run learned; a file, not arguments — a sitemap crawl
            # remembers tens of thousands of URLs.
            state = self.store.page_state(recipe.id)
            (run_dir / "page_state.json").write_text(json.dumps(state), encoding="utf-8")
        proc = await self._spawn(args, run_dir)
        handle = RunHandle(run_id=run_id, run_dir=run_dir, proc=proc, kind="crawl")
        self.active[run_id] = handle
        row = self.store.update_run(run_id, status="running", pid=proc.pid, started_at=_now())
        handle.task = asyncio.create_task(self._watch(handle), name=f"run-{run_id}")
        self.bus.publish(run_id, {"t": "status", "run_id": run_id, "status": "running"})
        return row  # type: ignore[return-value]

    def _fold_dataset(self, handle: RunHandle, stats: dict[str, Any]) -> dict[str, int] | None:
        """Runs are episodes; the dataset is what the recipe knows. Fold one into the other."""
        row = self.store.get_run(handle.run_id)
        if row is None or not row.recipe_id:
            return None
        recipe = self.store.get_recipe(row.recipe_id)
        keys = recipe.dedupe_key if recipe else ["_url"]
        with contextlib.suppress(Exception):
            return self.store.fold_run_into_dataset(
                row.recipe_id, handle.run_id, keys, partial=bool(stats.get("skipped"))
            )
        return None

    def _ingest_page_state(self, handle: RunHandle) -> int:
        """Fold the worker's page-state lines into the store, so the next run can skip them."""
        path = handle.run_dir / "page_state.jsonl"
        if not path.exists():
            return 0
        row = self.store.get_run(handle.run_id)
        recipe_id = row.recipe_id if row else None
        if not recipe_id:
            return 0
        entries: list[dict[str, Any]] = []
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    entries.append(json.loads(line))
        return self.store.remember_page_state(recipe_id, entries)

    async def _watch(self, handle: RunHandle) -> None:
        code = await handle.proc.wait()
        self._flush(handle, force=True)
        stats: dict[str, Any] = {}
        sp = handle.run_dir / "stats.json"
        if sp.exists():
            with contextlib.suppress(json.JSONDecodeError):
                stats = json.loads(sp.read_text())
        learned = self._ingest_page_state(handle)
        if learned:
            stats["page_state"] = learned
        folded = self._fold_dataset(handle, stats)
        if folded:
            stats["dataset"] = folded
        if handle.healed:
            stats["healed"] = handle.healed
        if handle.fills:
            stats["fill_history"] = {k: v[-120:] for k, v in handle.fills.items()}
        row = self.store.get_run(handle.run_id)
        # keep server-side stats written while the run was active (LLM fallback counters, …)
        for k in ("llm",):
            if row and k in (row.stats or {}) and k not in stats:
                stats[k] = row.stats[k]
        reason = stats.get("reason")
        if row and row.status == "cancelled":
            status = "cancelled"
        elif reason in ("finished", "max_items", "closespider_itemcount"):
            status = "finished"
        elif reason == "stopped":
            status = "stopped"
        else:
            status = "failed"
        self.store.update_run(
            handle.run_id,
            status=status,
            reason=reason or (f"exit {code}" if code else None),
            finished_at=_now(),
            stats=stats,
            items=int(stats.get("items", handle.counters.get("items", 0)) or 0),
            pages=int(stats.get("pages", handle.counters.get("pages", 0)) or 0),
            blocked=sum((stats.get("blocked") or {}).values())
            if stats
            else handle.counters.get("blocked", 0),
            escalations=sum((stats.get("escalations") or {}).values()) if stats else 0,
            error=None if status != "failed" else f"worker exited {code}; see worker.log",
        )
        tm = stats.get("tier_memory") or {}
        if tm:
            self.store.remember_tiers(tm)
        self.active.pop(handle.run_id, None)
        self.bus.publish(
            handle.run_id,
            {"t": "status", "run_id": handle.run_id, "status": status, "reason": reason},
        )
        finished = self.store.get_run(handle.run_id)
        for hook in list(self.on_finished):
            try:
                await hook(finished)  # type: ignore[arg-type]
            except Exception:  # a hook must never break run bookkeeping
                logger.exception("run finish hook failed")
        handle.done_event.set()

    def stop(self, run_id: str) -> bool:
        h = self.active.get(run_id)
        if not h:
            return False
        (h.run_dir / "control.json").write_text(json.dumps({"cmd": "stop"}))
        self.store.update_run(run_id, status="stopping")
        self.bus.publish(run_id, {"t": "status", "run_id": run_id, "status": "stopping"})
        return True

    async def cancel(self, run_id: str) -> bool:
        h = self.active.get(run_id)
        if not h:
            return False
        self.store.update_run(run_id, status="cancelled")
        with contextlib.suppress(ProcessLookupError):
            h.proc.terminate()
        try:
            await asyncio.wait_for(h.proc.wait(), timeout=15)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                h.proc.kill()
        return True

    # ------------------------------------------------------------------ events from workers
    def ingest(self, run_id: str, events: list[dict[str, Any]]) -> None:
        h = self.active.get(run_id)
        for ev in events:
            t = ev.get("t")
            if t == "item" and h is not None:
                n = int(ev.get("n") or (len(h.item_buffer) + 1))
                h.item_buffer.append((n, ev.get("row") or {}))
                h.counters["items"] = max(h.counters.get("items", 0), n)
                # keep WS payload light: the row itself is what the grid needs
            elif t == "healed" and h is not None:
                h.healed.append({k: v for k, v in ev.items() if k not in ("t", "run_id")})
                self.bus.publish(
                    "agent",
                    {
                        "t": "notify",
                        "level": "info",
                        "title": f"Selector healed: {ev.get('field')}",
                        "body": f"{(ev.get('old') or {}).get('css') or (ev.get('old') or {}).get('xpath')} → "
                        f"{(ev.get('new') or {}).get('css')} (fill {ev.get('fill')})",
                        "run_id": run_id,
                        "route": f"/runs/{run_id}",
                    },
                )
            elif t == "fill" and h is not None:
                rates = ev.get("rates") or {}
                for name, rate in rates.items():
                    hist = h.fills.setdefault(name, [])
                    hist.append(float(rate))
                    if len(hist) > 300:
                        del hist[0]
                    # drift: a field that filled well before collapses (and no heal fixed it)
                    if (
                        len(hist) >= 3
                        and rate <= 0.25
                        and max(hist[:-1]) >= 0.8
                        and name not in h.fill_alerted
                    ):
                        h.fill_alerted.add(name)
                        self.bus.publish(
                            "agent",
                            {
                                "t": "notify",
                                "level": "warning",
                                "title": f"Field stopped filling: {name}",
                                "body": f"{int(rate * 100)}% on {ev.get('url')} — the site may have changed; open the run and use “Fix with AI” or re-validate the recipe.",
                                "run_id": run_id,
                                "route": f"/runs/{run_id}",
                            },
                        )
            elif t == "page_failed" and h is not None:
                self._on_page_failed(run_id, ev)
            elif t == "progress" and h is not None:
                ev = {**ev, "fills": {k: v[-1] for k, v in h.fills.items() if v}}
                h.counters.update(
                    {
                        "pages": ev.get("pages", 0),
                        "items": max(h.counters.get("items", 0), int(ev.get("items", 0) or 0)),
                        "blocked": ev.get("blocked", 0),
                        "escalations": ev.get("escalations", 0),
                    }
                )
                now = time.monotonic()
                if now - h.last_counter_write > 2.0:
                    h.last_counter_write = now
                    self.store.update_run(
                        run_id,
                        pages=int(h.counters.get("pages", 0)),
                        items=int(h.counters.get("items", 0)),
                        blocked=int(h.counters.get("blocked", 0)),
                        escalations=int(h.counters.get("escalations", 0)),
                    )
            self.bus.publish(run_id, ev)
        if h is not None and (len(h.item_buffer) >= 200):
            self._flush(h)

    def _on_page_failed(self, run_id: str, ev: dict[str, Any]) -> None:
        """A page the worker could not extract (saved as HTML). Persist it for the LLM/agent
        fallback; the FallbackRunner (if configured) is notified through `on_page_failed`."""
        from scrapy_awesome.store.models import FailedPageRow

        row = FailedPageRow(
            id=f"{run_id}-{int(time.time() * 1000) % 10**9}-{secrets_hex()}",
            run_id=run_id,
            url=str(ev.get("url") or ""),
            kind=str(ev.get("kind") or "list"),
            reason=str(ev.get("reason") or ""),
            html_path=str(ev.get("path") or ""),
            base_row=ev.get("base_row"),
            tier=ev.get("tier"),
        )
        try:
            self.store.add_failed_page(row)
        except Exception:  # pragma: no cover
            logger.exception("could not persist failed page")
            return
        for hook in list(self.on_page_failed):
            try:
                hook(row)
            except Exception:  # pragma: no cover
                logger.exception("page_failed hook failed")

    def _flush(self, h: RunHandle, *, force: bool = False) -> None:
        if not h.item_buffer:
            return
        if force or time.monotonic() - h.last_flush >= 0.5 or len(h.item_buffer) >= 200:
            buf, h.item_buffer = h.item_buffer, []
            h.last_flush = time.monotonic()
            try:
                self.store.add_items(h.run_id, buf)
            except Exception:  # pragma: no cover
                logger.exception("failed to persist %d items for %s", len(buf), h.run_id)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            for h in list(self.active.values()):
                self._flush(h)

    # ------------------------------------------------------------------ snapshots
    async def snapshot(
        self,
        urls: list[str],
        *,
        recipe: Recipe | None,
        kind: str = "list",
        tier: str | None = None,
        headed: bool = False,
        capture_xhr: bool = False,
        timeout: float = 240,
    ) -> list[SampleRow]:
        job_id = "snap-" + uuid.uuid4().hex[:8]
        run_dir = self.paths.cache / "snapshot-jobs" / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        args = ["snapshot", "--urls", json.dumps(urls), "--kind", kind]
        if capture_xhr:
            args.append("--capture-xhr")
            tier = "interactive"  # only a real browser can be watched
        if recipe is not None:
            args += ["--recipe", str(save_recipe(recipe, run_dir / "recipe.json"))]
        args += common_worker_args(
            run_id=job_id,
            run_dir=run_dir,
            tier=tier,
            headed=headed,
            storage_state=self._storage_state_for(recipe) if recipe else None,
            events_url=None,
            events_token=None,
            control_url=None,
            obey_robots=self.settings.crawl.obey_robots,
            httpcache=False,
            chrome=self.settings.crawl.chrome_executable_path,
            proxies=self.settings.crawl.proxies,
            tier_memory=self.store.tier_memory(),
            log_level="WARNING",
        )
        proc = await self._spawn(args, run_dir)
        handle = RunHandle(run_id=job_id, run_dir=run_dir, proc=proc, kind="snapshot")
        self.active[job_id] = handle
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        finally:
            self.active.pop(job_id, None)
        rows: list[SampleRow] = []
        snap_dir = run_dir / "snapshots"
        if snap_dir.exists():
            for p in sorted(snap_dir.glob("*.json")):
                rec = json.loads(p.read_text(encoding="utf-8"))
                from scrapy_awesome.fetch.blocks import page_title

                rows.append(
                    self.store.add_sample(
                        url=rec["url"],
                        html=rec.get("html") or "",
                        final_url=rec.get("final_url") or rec["url"],
                        status=int(rec.get("status") or 0),
                        tier=rec.get("tier"),
                        kind=kind,
                        recipe_id=recipe.id if recipe else None,
                        blobs=rec.get("blobs") or {},
                        verdict=rec.get("verdict"),
                        headers=rec.get("headers") or {},
                        title=page_title(rec.get("html") or ""),
                        xhr=rec.get("xhr") or [],
                    )
                )
                p.unlink(missing_ok=True)
        # persist any tier learned during the snapshot job
        stats_path = run_dir / "stats.json"
        if stats_path.exists():
            with contextlib.suppress(json.JSONDecodeError):
                tm = json.loads(stats_path.read_text()).get("tier_memory") or {}
                if tm:
                    self.store.remember_tiers(tm)
        return rows
