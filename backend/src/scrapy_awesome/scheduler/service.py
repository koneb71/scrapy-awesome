"""Scheduler: runs recipes on cron/interval schedules, diffs results, notifies.

Design: schedules live in SQLite (`ScheduleRow.next_run_at`); a lightweight asyncio loop in the
server checks for due schedules every `tick` seconds. APScheduler 3.x *triggers* compute the next
fire time (cron/interval, timezone-aware) — we don't run its executor/jobstore, so there is one
source of truth (our DB) and the loop survives restarts:

* **catch-up / coalesce** — a schedule that was due while the server was down runs once at
  start-up, and `next_run_at` is computed from *now*, so missed slots collapse into one run;
* **per-recipe serialization** — a schedule whose recipe already has an active run is skipped
  this tick and picked up on the next one; global `max_concurrent_runs` is respected too;
* **diff + notify** — when a scheduled run finishes, it is diffed against the previous finished
  run of the same recipe (dedupe key) and a `notify` event is broadcast (toast / OS notification).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.scheduler.diff import diff_rows, summary_line
from scrapy_awesome.store import RunRow, ScheduleRow, Store
from scrapy_awesome.store.models import iso

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _tz(name: str | None) -> Any:
    from tzlocal import get_localzone

    if not name:
        return get_localzone()
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def compute_next(sch: ScheduleRow, after: datetime | None = None) -> datetime | None:
    """Next fire time (UTC) strictly after `after` (default now)."""
    from datetime import timedelta

    from apscheduler.triggers.cron import CronTrigger

    after = _aware(after) or _now()
    tz = _tz(sch.timezone)
    if sch.kind == "cron":
        if not sch.cron:
            return None
        trig = CronTrigger.from_crontab(sch.cron, timezone=tz)
        nxt = trig.get_next_fire_time(None, after.astimezone(tz))
        return nxt.astimezone(UTC) if nxt else None
    if sch.kind == "interval":
        if not sch.every_seconds or sch.every_seconds < 60:
            return None
        # "every N" means N after the last start (or after now for a fresh/edited schedule)
        return after.astimezone(UTC) + timedelta(seconds=sch.every_seconds)
    return None


def validate_schedule(
    kind: str, cron: str | None, every_seconds: int | None, timezone: str | None
) -> str | None:
    """Return an error message or None."""
    try:
        _tz(timezone)
    except Exception:
        return f"unknown timezone {timezone!r}"
    if kind == "cron":
        if not cron:
            return "cron expression required"
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(cron)
        except Exception as exc:
            return f"invalid cron expression: {exc}"
        return None
    if kind == "interval":
        if not every_seconds or every_seconds < 60:
            return "interval must be at least 60 seconds"
        return None
    return f"unknown kind {kind!r}"


def describe(sch: ScheduleRow) -> str:
    if sch.kind == "interval" and sch.every_seconds:
        s = sch.every_seconds
        if s % 86400 == 0:
            return f"every {s // 86400} day(s)"
        if s % 3600 == 0:
            return f"every {s // 3600} hour(s)"
        return f"every {s // 60} min"
    return f"cron {sch.cron}" + (f" ({sch.timezone})" if sch.timezone else "")


class Scheduler:
    def __init__(
        self, *, store: Store, manager: Any, bus: Any, settings: Any, tick: float = 30.0
    ) -> None:
        self.store = store
        self.manager = manager
        self.bus = bus
        self.settings = settings
        self.tick = tick
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        # (re)compute next_run_at for enabled schedules that lack one (fresh, or edited offline)
        for sch in self.store.list_schedules():
            if sch.enabled and sch.next_run_at is None:
                self.store.update_schedule(sch.id, next_run_at=compute_next(sch))
        self.manager.on_finished.append(self.on_run_finished)
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with contextlib.suppress(ValueError):
            self.manager.on_finished.remove(self.on_run_finished)

    def poke(self) -> None:
        """Re-check soon (after a schedule was created/edited)."""
        self._wake.set()

    async def _loop(self) -> None:
        last_prune = 0.0
        while True:
            try:
                await self.tick_once()
                if asyncio.get_running_loop().time() - last_prune > 86_400 or last_prune == 0.0:
                    self.prune()
                    last_prune = asyncio.get_running_loop().time()
            except Exception:  # keep the loop alive
                log.exception("scheduler tick failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick)

    def prune(self) -> dict[str, int]:
        r = self.settings.retention
        out = self.store.prune(
            keep_runs_per_recipe=r.keep_runs_per_recipe,
            keep_samples_per_recipe=r.keep_samples_per_recipe,
            keep_days=r.keep_days,
            active_run_ids=set(self.manager.active),
        )
        if out["runs"] or out["samples"]:
            log.info("retention: pruned %s runs, %s samples", out["runs"], out["samples"])
        return out

    # ------------------------------------------------------------------ core
    def _recipe_running(self, recipe_id: str) -> bool:
        for h in self.manager.active.values():
            row = self.store.get_run(h.run_id)
            if row and row.recipe_id == recipe_id and h.kind == "crawl":
                return True
        return False

    async def tick_once(self, now: datetime | None = None) -> list[str]:
        """Start every due schedule that can run; returns started run ids."""
        now = now or _now()
        started: list[str] = []
        for sch in self.store.due_schedules(now.replace(tzinfo=None) if now.tzinfo else now):
            row = self.store.get_recipe_row(sch.recipe_id)
            if row is None or row.archived:
                self.store.update_schedule(
                    sch.id, enabled=False, next_run_at=None, last_status="recipe missing"
                )
                continue
            if self._recipe_running(sch.recipe_id):
                log.info("schedule %s: recipe %s still running, will retry", sch.id, sch.recipe_id)
                continue
            if self.manager.active_crawls >= self.settings.max_concurrent_runs:
                log.info("schedule %s: max concurrent runs reached, will retry", sch.id)
                continue
            run_id = await self._start(sch, row.version, row.data)
            if run_id:
                started.append(run_id)
        return started

    async def _start(self, sch: ScheduleRow, version: int, data: dict[str, Any]) -> str | None:
        recipe = self.store.get_recipe(sch.recipe_id) or Recipe.model_validate(
            data
        )  # + fingerprints
        try:
            run = await self.manager.start_crawl(
                recipe,
                recipe_version=version,
                max_pages=sch.max_pages,
                max_items=sch.max_items,
                schedule_id=sch.id,
            )
        except Exception as exc:
            log.warning("schedule %s failed to start: %s", sch.id, exc)
            self.store.update_schedule(
                sch.id, last_status=f"start failed: {exc}"[:200], next_run_at=compute_next(sch)
            )
            return None
        self.store.update_schedule(
            sch.id,
            last_run_at=_now(),
            last_run_id=run.id,
            last_status="running",
            next_run_at=compute_next(sch),  # from now → coalesces missed slots
        )
        self.bus.publish(
            "agent",
            {
                "t": "schedule_started",
                "schedule_id": sch.id,
                "run_id": run.id,
                "recipe_id": sch.recipe_id,
            },
        )
        return run.id

    async def run_now(self, schedule_id: str) -> RunRow:
        sch = self.store.get_schedule(schedule_id)
        if not sch:
            raise KeyError(schedule_id)
        row = self.store.get_recipe_row(sch.recipe_id)
        if not row:
            raise KeyError(sch.recipe_id)
        run_id = await self._start(sch, row.version, row.data)
        if not run_id:
            raise RuntimeError("could not start run (see server log)")
        return self.store.get_run(run_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ finish → diff + notify
    async def on_run_finished(self, run: RunRow | None) -> None:
        if run is None or run.kind != "crawl":
            return
        diff = None
        if run.status == "finished" and run.recipe_id:
            diff = self.diff_against_previous(run)
            if diff is not None:
                stats = dict(run.stats or {})
                stats["diff"] = {k: v for k, v in diff.items() if k != "samples"}
                self.store.update_run(run.id, stats=stats)
        if run.schedule_id:
            self.store.update_schedule(
                run.schedule_id,
                last_status=run.status,
                last_diff={k: v for k, v in (diff or {}).items() if k != "samples"} or None,
            )
        sch = self.store.get_schedule(run.schedule_id) if run.schedule_id else None
        if sch is None or sch.notify:
            self._notify(run, diff, scheduled=sch is not None)

    def diff_against_previous(self, run: RunRow) -> dict[str, Any] | None:
        prev = self.store.previous_finished_run(run.recipe_id or "", run.id)
        if not prev:
            return None
        rec = self.store.get_recipe(run.recipe_id or "")
        keys = rec.dedupe_key if rec else ["_url"]
        d = diff_rows(
            self.store.iter_items(prev.id),
            self.store.iter_items(run.id),
            keys,
            partial=bool((run.stats or {}).get("skipped")),
        )
        d["against_run_id"] = prev.id
        d["against_finished_at"] = iso(prev.finished_at)
        return d

    def _notify(self, run: RunRow, diff: dict[str, Any] | None, *, scheduled: bool) -> None:
        name = run.recipe_name or "Run"
        if run.status == "finished":
            body = f"{run.items} rows" + (f" · {summary_line(diff)}" if diff else "")
            level = "info"
            title = f"{'Scheduled run' if scheduled else 'Run'} finished: {name}"
        elif run.status in ("failed",):
            body = run.error or run.reason or "failed"
            level = "error"
            title = f"Run failed: {name}"
        else:
            body = f"{run.status} after {run.items} rows"
            level = "warning"
            title = f"Run {run.status}: {name}"
        self.bus.publish(
            "agent",
            {
                "t": "notify",
                "level": level,
                "title": title,
                "body": body,
                "run_id": run.id,
                "recipe_id": run.recipe_id,
                "schedule_id": run.schedule_id,
                "route": f"/runs/{run.id}",
            },
        )


def schedule_out(row: ScheduleRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "recipe_id": row.recipe_id,
        "name": row.name,
        "kind": row.kind,
        "cron": row.cron,
        "every_seconds": row.every_seconds,
        "timezone": row.timezone,
        "describe": describe(row),
        "enabled": row.enabled,
        "max_pages": row.max_pages,
        "max_items": row.max_items,
        "notify": row.notify,
        "next_run_at": iso(row.next_run_at),
        "last_run_at": iso(row.last_run_at),
        "last_run_id": row.last_run_id,
        "last_status": row.last_status,
        "last_diff": row.last_diff,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def new_schedule_id() -> str:
    return uuid.uuid4().hex[:10]
