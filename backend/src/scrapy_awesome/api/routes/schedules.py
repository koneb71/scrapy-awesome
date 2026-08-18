"""Schedules: cron/interval crawls of a saved recipe, with diffs and notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from scrapy_awesome.scheduler.service import (
    Scheduler,
    compute_next,
    new_schedule_id,
    schedule_out,
    validate_schedule,
)
from scrapy_awesome.store import ScheduleRow, Store

router = APIRouter(tags=["schedules"])


class ScheduleIn(BaseModel):
    recipe_id: str
    name: str = ""
    kind: str = "cron"  # cron | interval
    cron: str | None = None
    every_seconds: int | None = None
    timezone: str | None = None
    enabled: bool = True
    max_pages: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)
    notify: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    cron: str | None = None
    every_seconds: int | None = None
    timezone: str | None = None
    enabled: bool | None = None
    max_pages: int | None = None
    max_items: int | None = None
    notify: bool | None = None


def _sched(request: Request) -> Scheduler:
    return request.app.state.scheduler  # type: ignore[no-any-return]


@router.get("/schedules")
def list_schedules(request: Request, recipe_id: str | None = None) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    return [schedule_out(r) for r in store.list_schedules(recipe_id)]


@router.post("/schedules", status_code=201)
def create_schedule(request: Request, body: ScheduleIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    if not store.get_recipe_row(body.recipe_id):
        raise HTTPException(404, "recipe not found")
    err = validate_schedule(body.kind, body.cron, body.every_seconds, body.timezone)
    if err:
        raise HTTPException(422, err)
    row = ScheduleRow(id=new_schedule_id(), **body.model_dump())
    row.next_run_at = compute_next(row) if row.enabled else None
    row = store.upsert_schedule(row)
    _sched(request).poke()
    return schedule_out(row)


@router.get("/schedules/{schedule_id}")
def get_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    row = request.app.state.store.get_schedule(schedule_id)
    if not row:
        raise HTTPException(404, "schedule not found")
    return schedule_out(row)


@router.patch("/schedules/{schedule_id}")
def patch_schedule(request: Request, schedule_id: str, body: SchedulePatch) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = store.get_schedule(schedule_id)
    if not row:
        raise HTTPException(404, "schedule not found")
    patch = body.model_dump(exclude_unset=True)
    merged = row.model_copy(update=patch)
    err = validate_schedule(merged.kind, merged.cron, merged.every_seconds, merged.timezone)
    if err:
        raise HTTPException(422, err)
    patch["next_run_at"] = compute_next(merged) if merged.enabled else None
    row = store.update_schedule(schedule_id, **patch)
    _sched(request).poke()
    return schedule_out(row)  # type: ignore[arg-type]


@router.delete("/schedules/{schedule_id}")
def delete_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    request.app.state.store.delete_schedule(schedule_id)
    return {"id": schedule_id, "deleted": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(request: Request, schedule_id: str) -> dict[str, Any]:
    from scrapy_awesome.api.routes.runs import run_out

    try:
        run = await _sched(request).run_now(schedule_id)
    except KeyError:
        raise HTTPException(404, "schedule or recipe not found") from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return run_out(run, True)


@router.post("/schedules/tick")
async def tick(request: Request) -> dict[str, Any]:
    """Run the due-check now (tests / debugging)."""
    started = await _sched(request).tick_once()
    return {"started": started}
