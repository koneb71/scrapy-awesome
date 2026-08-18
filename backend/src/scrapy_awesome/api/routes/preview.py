"""Preview / validation: run a recipe in-process against cached samples (sub-second), optionally
fetching the standard sample set first (page 1, page 2 via next link, two detail pages)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from scrapy_awesome.api.routes.pages import sample_out
from scrapy_awesome.extract.engine import extract_list_items, next_page_url
from scrapy_awesome.extract.fingerprint import compute_fingerprints
from scrapy_awesome.extract.validate import Sample, validate_on_samples
from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot.analyze import analyze_html
from scrapy_awesome.store import SampleRow, Store

router = APIRouter(tags=["preview"])


class PreviewIn(BaseModel):
    recipe: dict[str, Any]
    sample_ids: list[str] | None = None  # None → all samples stored for this recipe id
    max_rows: int = 50


class SamplesIn(BaseModel):
    recipe: dict[str, Any]
    with_page2: bool = True
    detail_pages: int = 2
    tier: str | None = None


def _remember_fingerprints(store: Store, recipe: Recipe, rows: list[SampleRow]) -> dict[str, Any]:
    """Fingerprint list fields on the first list page and keep them on the saved recipe row
    (derived data, not a recipe version) so runs can self-heal."""
    first = next((r for r in rows if r.kind == "list"), None)
    if first is None:
        return {}
    try:
        fps = compute_fingerprints(recipe, store.sample_html(first), first.final_url or first.url)
    except Exception:  # never fail validation because of fingerprints
        return {}
    if fps and store.get_recipe_row(recipe.id):
        store.set_fingerprints(recipe.id, fps)
    return fps


def _to_sample(store: Store, row: SampleRow) -> Sample:
    return Sample(
        row.final_url or row.url,
        store.sample_html(row),
        row.kind if row.kind in ("list", "detail") else "list",
        row.blobs or None,
    )  # type: ignore[arg-type]


@router.post("/preview")
def preview(request: Request, body: PreviewIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    try:
        recipe = Recipe.model_validate(body.recipe)
    except ValidationError as exc:
        raise HTTPException(422, {"errors": exc.errors()}) from exc
    if not recipe.ready:
        raise HTTPException(
            422, {"detail": "recipe is not ready", "errors": recipe.readiness_errors()}
        )
    if body.sample_ids:
        rows = [r for r in (store.get_sample(s) for s in body.sample_ids) if r]
    else:
        rows = store.list_samples(recipe_id=recipe.id, limit=20)
    if not rows:
        raise HTTPException(
            404, "no samples for this recipe — call POST /api/preview/samples first"
        )
    samples = [_to_sample(store, r) for r in rows]
    report = validate_on_samples(recipe, samples, max_rows=body.max_rows)
    fps = _remember_fingerprints(store, recipe, rows)
    return {
        "report": report.to_dict(),
        "samples": [sample_out(r) for r in rows],
        "fingerprints": fps,
    }


@router.post("/preview/samples")
async def fetch_samples(request: Request, body: SamplesIn) -> dict[str, Any]:
    """Fetch the standard validation set through the engine and store it for this recipe."""
    store: Store = request.app.state.store
    manager = request.app.state.manager
    try:
        recipe = Recipe.model_validate(body.recipe)
    except ValidationError as exc:
        raise HTTPException(422, {"errors": exc.errors()}) from exc

    if not recipe.ready:
        raise HTTPException(
            422, {"detail": "recipe is not ready", "errors": recipe.readiness_errors()}
        )
    # replace previous samples for this recipe
    for old in store.list_samples(recipe_id=recipe.id, limit=100):
        store.delete_sample(old.id)

    first_rows = await manager.snapshot(
        [recipe.seeds[0]], recipe=recipe, kind="list", tier=body.tier
    )
    if not first_rows:
        raise HTTPException(502, "could not fetch the seed page")
    first = first_rows[0]
    html = store.sample_html(first)
    # keep the Analyze tab meaningful: page 1 carries the heuristic analysis
    first = (
        store.update_sample(
            first.id,
            analysis=analyze_html(
                html, first.final_url or first.url, blobs=first.blobs or None
            ).to_dict(),
        )
        or first
    )
    first_rows[0] = first
    more: list[tuple[str, str]] = []
    if body.with_page2:
        nxt = next_page_url(recipe, html, first.final_url or first.url)
        if nxt:
            more.append((nxt, "list"))
        elif recipe.pagination.kind == "url_template" and recipe.pagination.url_template:
            more.append(
                (
                    recipe.pagination.url_template.format(
                        page=recipe.pagination.start + recipe.pagination.step
                    ),
                    "list",
                )
            )
    if recipe.detail.enabled and body.detail_pages > 0:
        items, _ = extract_list_items(
            recipe, html, first.final_url or first.url, json_blobs=first.blobs or None
        )
        links = [it.detail_url for it in items if it.detail_url]
        if links:
            more.append((links[0], "detail"))
            if body.detail_pages > 1 and len(links) > 2:
                more.append((links[len(links) // 2], "detail"))
    rows = list(first_rows)
    for url, kind in more:
        rows += await manager.snapshot([url], recipe=recipe, kind=kind, tier=body.tier)
    samples = [_to_sample(store, r) for r in rows]
    report = validate_on_samples(recipe, samples)
    fps = _remember_fingerprints(store, recipe, rows)
    return {
        "report": report.to_dict(),
        "samples": [sample_out(r) for r in rows],
        "fingerprints": fps,
    }
