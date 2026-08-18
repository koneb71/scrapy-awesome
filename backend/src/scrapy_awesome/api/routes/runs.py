"""Runs: start / stop / resume / cancel / list / items / events / export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scrapy_awesome.export.writers import FORMATS, export_rows
from scrapy_awesome.recipe.compat import incompatible_changes
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store import RunRow, Store, iso

router = APIRouter(tags=["runs"])


class StartIn(BaseModel):
    recipe_id: str | None = None
    recipe: dict[str, Any] | None = None  # ad-hoc run of an unsaved recipe
    max_pages: int | None = None
    max_items: int | None = None
    tier: str | None = None
    headed: bool = False
    httpcache: bool = False


class ExportIn(BaseModel):
    fmt: str = "xlsx"
    include_meta: bool = True
    dest: str | None = None  # absolute path (or ~/…) to write to; default: <run_dir>/items.<fmt>


def run_out(row: RunRow, active: bool = False) -> dict[str, Any]:
    return {
        "id": row.id,
        "recipe_id": row.recipe_id,
        "recipe_version": row.recipe_version,
        "recipe_name": row.recipe_name,
        "kind": row.kind,
        "status": row.status,
        "reason": row.reason,
        "items": row.items,
        "pages": row.pages,
        "blocked": row.blocked,
        "escalations": row.escalations,
        "limits": row.limits,
        "stats": row.stats,
        "error": row.error,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "active": active,
        "run_dir": row.run_dir,
        "schedule_id": row.schedule_id,
    }


@router.get("/runs")
def list_runs(
    request: Request, recipe_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    manager = request.app.state.manager
    return [
        run_out(r, r.id in manager.active)
        for r in store.list_runs(recipe_id=recipe_id, limit=limit)
    ]


@router.post("/runs", status_code=201)
async def start_run(request: Request, body: StartIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    manager = request.app.state.manager
    version: int | None = None
    if body.recipe_id:
        row = store.get_recipe_row(body.recipe_id)
        if not row:
            raise HTTPException(404, "recipe not found")
        recipe = Recipe.model_validate(row.data)
        if row.fingerprints:  # design-time element fingerprints → self-heal in the worker
            recipe.fingerprints = dict(row.fingerprints)
        version = row.version
    elif body.recipe:
        recipe = Recipe.model_validate(body.recipe)
    else:
        raise HTTPException(422, "recipe_id or recipe required")
    if not recipe.ready:
        raise HTTPException(
            422, {"detail": "recipe is not ready to run", "errors": recipe.readiness_errors()}
        )
    try:
        run = await manager.start_crawl(
            recipe,
            recipe_version=version,
            max_pages=body.max_pages,
            max_items=body.max_items,
            tier=body.tier,
            headed=body.headed,
            httpcache=body.httpcache,
        )
    except RuntimeError as exc:
        raise HTTPException(429, str(exc)) from exc
    return run_out(run, True)


def _get(request: Request, run_id: str) -> RunRow:
    row = request.app.state.store.get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    return row


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    row = _get(request, run_id)
    return run_out(row, run_id in request.app.state.manager.active)


@router.post("/runs/{run_id}/stop")
def stop_run(request: Request, run_id: str) -> dict[str, Any]:
    _get(request, run_id)
    ok = request.app.state.manager.stop(run_id)
    if not ok:
        raise HTTPException(409, "run is not active")
    return {"id": run_id, "status": "stopping"}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    _get(request, run_id)
    ok = await request.app.state.manager.cancel(run_id)
    if not ok:
        raise HTTPException(409, "run is not active")
    return {"id": run_id, "status": "cancelled"}


@router.post("/runs/{run_id}/resume")
async def resume_run(request: Request, run_id: str) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = _get(request, run_id)
    if row.status not in ("stopped", "failed"):
        raise HTTPException(409, f"cannot resume a run in status {row.status}")
    if not row.recipe_id:
        raise HTTPException(409, "ad-hoc runs cannot be resumed")
    current = store.get_recipe(row.recipe_id)
    if current is None:
        raise HTTPException(404, "recipe no longer exists")
    ran = Recipe.model_validate(json.loads((Path(row.run_dir) / "recipe.json").read_text()))
    bad = incompatible_changes(ran, current)
    if bad:
        raise HTTPException(
            409, {"detail": "recipe changed incompatibly since this run", "paths": bad}
        )
    run = await request.app.state.manager.start_crawl(
        current,
        recipe_version=store.get_recipe_row(row.recipe_id).version,  # type: ignore[union-attr]
        max_pages=row.limits.get("max_pages"),
        max_items=row.limits.get("max_items"),
        tier=row.limits.get("tier"),
        resume_run_id=run_id,
    )
    return run_out(run, True)


@router.get("/runs/{run_id}/items")
def run_items(request: Request, run_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    store: Store = request.app.state.store
    _get(request, run_id)
    limit = max(1, min(limit, 1000))
    return {
        "total": store.count_items(run_id),
        "offset": offset,
        "items": store.list_items(run_id, offset=offset, limit=limit),
    }


@router.get("/runs/{run_id}/events")
def run_events(
    request: Request, run_id: str, tail: int = 200, types: str | None = None
) -> list[dict[str, Any]]:
    row = _get(request, run_id)
    p = Path(row.run_dir) / "events.jsonl"
    if not p.exists():
        return []
    wanted = set(types.split(",")) if types else None
    out: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wanted and ev.get("t") not in wanted:
                continue
            if ev.get("t") == "item":
                continue  # items come from /items
            out.append(ev)
    return out[-tail:]


@router.get("/runs/{run_id}/log")
def run_log(request: Request, run_id: str, tail: int = 200) -> dict[str, Any]:
    row = _get(request, run_id)
    p = Path(row.run_dir) / "worker.log"
    if not p.exists():
        return {"lines": []}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": lines[-tail:]}


@router.post("/runs/{run_id}/export")
def export_run(request: Request, run_id: str, body: ExportIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = _get(request, run_id)
    fmt = body.fmt.lower()
    if fmt not in FORMATS:
        raise HTTPException(422, f"fmt must be one of {FORMATS}")
    rows = list(store.iter_items(run_id))
    fields = None
    if row.recipe_id and row.recipe_version:
        rec = store.get_recipe_version(row.recipe_id, row.recipe_version) or store.get_recipe(
            row.recipe_id
        )
        if rec:
            fields = [f.name for f in rec.fields]
    dest = Path(row.run_dir) / f"items.{fmt}"
    if body.dest:
        dest = Path(body.dest).expanduser()
        if not dest.is_absolute():
            raise HTTPException(422, "dest must be an absolute path")
        if dest.is_dir():
            dest = dest / f"{row.recipe_name or run_id}.{fmt}"
        dest.parent.mkdir(parents=True, exist_ok=True)
    export_rows(rows, dest, fmt=fmt, fields=fields, include_meta=body.include_meta)
    return {"path": str(dest), "rows": len(rows), "download": f"/api/runs/{run_id}/download/{fmt}"}


@router.get("/runs/{run_id}/diff")
def run_diff(request: Request, run_id: str, against: str | None = None) -> dict[str, Any]:
    """Diff this run's rows against `against` (default: the previous finished run of the recipe)."""
    from scrapy_awesome.scheduler.diff import diff_rows

    store: Store = request.app.state.store
    row = _get(request, run_id)
    prev = (
        store.get_run(against)
        if against
        else store.previous_finished_run(row.recipe_id or "", run_id)
    )
    if not prev:
        return {"run_id": run_id, "against_run_id": None, "diff": None}
    rec = store.get_recipe(row.recipe_id or "") if row.recipe_id else None
    keys = rec.dedupe_key if rec else ["_url"]
    d = diff_rows(store.iter_items(prev.id), store.iter_items(run_id), keys)
    return {
        "run_id": run_id,
        "against_run_id": prev.id,
        "against_finished_at": iso(prev.finished_at),
        "diff": d,
    }


@router.get("/runs/{run_id}/download/{fmt}")
def download(request: Request, run_id: str, fmt: str) -> FileResponse:
    row = _get(request, run_id)
    p = Path(row.run_dir) / f"items.{fmt}"
    if not p.exists():
        raise HTTPException(404, "export not found; POST /export first")
    name = f"{(row.recipe_name or 'items').replace(' ', '_')}-{run_id}.{fmt}"
    return FileResponse(p, filename=name)
