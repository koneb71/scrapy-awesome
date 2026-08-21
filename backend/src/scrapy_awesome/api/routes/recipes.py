"""Recipe CRUD + versions + validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from scrapy_awesome.recipe.compat import incompatible_changes
from scrapy_awesome.recipe.io import dump_recipe
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.store import RecipeRow, Store, iso

router = APIRouter(tags=["recipes"])


def _row_out(row: RecipeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "recipe": row.data,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "last_run_id": row.last_run_id,
        "archived": row.archived,
    }


def _announce(request: Request, row: RecipeRow) -> None:
    """Tell open UIs (and anyone on the firehose) that a recipe changed — agents save too."""
    request.app.state.bus.publish(
        "agent", {"t": "recipe_saved", "id": row.id, "version": row.version, "name": row.name}
    )


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()]


@router.get("/recipes")
def list_recipes(request: Request, include_archived: bool = False) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    return [_row_out(r) for r in store.list_recipes(include_archived=include_archived)]


@router.post("/recipes", status_code=201)
def create_recipe(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    store: Store = request.app.state.store
    try:
        recipe = Recipe.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, {"errors": _errors(exc)}) from exc
    if store.get_recipe_row(recipe.id):
        raise HTTPException(409, "recipe id exists; use PUT")
    row = store.save_recipe(recipe, note="created")
    _announce(request, row)
    return _row_out(row)


@router.post("/recipes/validate")
def validate_recipe(body: dict[str, Any]) -> dict[str, Any]:
    try:
        recipe = Recipe.model_validate(body)
    except ValidationError as exc:
        return {"ok": False, "errors": _errors(exc)}
    return {"ok": True, "errors": [], "recipe": recipe.to_dict()}


@router.get("/recipes/{recipe_id}")
def get_recipe(request: Request, recipe_id: str) -> dict[str, Any]:
    row = request.app.state.store.get_recipe_row(recipe_id)
    if not row:
        raise HTTPException(404, "recipe not found")
    return _row_out(row)


@router.put("/recipes/{recipe_id}")
def update_recipe(
    request: Request, recipe_id: str, body: dict[str, Any], note: str = ""
) -> dict[str, Any]:
    store: Store = request.app.state.store
    old_row = store.get_recipe_row(recipe_id)
    if not old_row:
        raise HTTPException(404, "recipe not found")
    body = dict(body)
    body["id"] = recipe_id
    try:
        recipe = Recipe.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, {"errors": _errors(exc)}) from exc
    old = Recipe.model_validate(old_row.data)
    row = store.save_recipe(recipe, note=note)
    _announce(request, row)
    return {**_row_out(row), "incompatible_with_resume": incompatible_changes(old, recipe)}


@router.delete("/recipes/{recipe_id}")
def delete_recipe(request: Request, recipe_id: str) -> dict[str, Any]:
    ok = request.app.state.store.delete_recipe(recipe_id)
    if not ok:
        raise HTTPException(404, "recipe not found")
    return {"id": recipe_id, "archived": True}


@router.get("/recipes/{recipe_id}/dataset")
def dataset(
    request: Request,
    recipe_id: str,
    limit: int = 100,
    offset: int = 0,
    include_gone: bool = True,
    changed_days: int | None = None,
) -> dict[str, Any]:
    """Everything this recipe has ever seen, one row per item rather than per run."""
    store: Store = request.app.state.store
    since = datetime.now(UTC) - timedelta(days=changed_days) if changed_days is not None else None
    return store.dataset(
        recipe_id,
        limit=max(1, min(limit, 1000)),
        offset=max(0, offset),
        include_gone=include_gone,
        changed_since=since,
    )


@router.get("/recipes/{recipe_id}/dataset/history")
def dataset_history(request: Request, recipe_id: str, key: str) -> dict[str, Any]:
    """How one row's values have changed, newest last."""
    store: Store = request.app.state.store
    return {"key": key, "history": store.dataset_history(recipe_id, key)}


@router.delete("/recipes/{recipe_id}/dataset")
def forget_dataset(request: Request, recipe_id: str) -> dict[str, int]:
    """Start the dataset over (the runs themselves are untouched)."""
    store: Store = request.app.state.store
    return {"forgotten": store.forget_dataset(recipe_id)}


@router.get("/recipes/{recipe_id}/versions")
def versions(request: Request, recipe_id: str) -> list[dict[str, Any]]:
    return [
        {
            "version": v.version,
            "note": v.note,
            "created_at": iso(v.created_at),
            "recipe": v.data,
        }
        for v in request.app.state.store.list_recipe_versions(recipe_id)
    ]


@router.post("/recipes/{recipe_id}/rollback/{version}")
def rollback(request: Request, recipe_id: str, version: int) -> dict[str, Any]:
    store: Store = request.app.state.store
    recipe = store.get_recipe_version(recipe_id, version)
    if not recipe:
        raise HTTPException(404, "version not found")
    row = store.save_recipe(recipe, note=f"rollback to v{version}")
    _announce(request, row)
    return _row_out(row)


@router.get("/recipes/{recipe_id}/export")
def export_recipe(request: Request, recipe_id: str, fmt: str = "yaml") -> Any:
    """`fmt=yaml|json` → the recipe file; `fmt=scrapy` → a standalone Scrapy project (zip)."""
    recipe = request.app.state.store.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(404, "recipe not found")
    if fmt == "scrapy":
        from fastapi.responses import Response

        from scrapy_awesome.export.scrapy_project import build_zip

        data = build_zip(recipe, obey_robots=request.app.state.settings.crawl.obey_robots)
        name = (
            "".join(ch if ch.isalnum() else "_" for ch in recipe.name.lower()).strip("_")
            or "recipe"
        )
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}_scrapy.zip"'},
        )
    return PlainTextResponse(dump_recipe(recipe, fmt="json" if fmt == "json" else "yaml"))
