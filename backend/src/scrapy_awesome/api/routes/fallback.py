"""Failed pages of a run: LLM fallback status + agent hand-off (get markdown, submit rows)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from scrapy_awesome.llm.fallback import (
    FallbackRunner,
    field_spec,
    json_schema,
    page_markdown,
    submit_agent_rows,
)
from scrapy_awesome.store import FailedPageRow, Store
from scrapy_awesome.store.models import iso

router = APIRouter(tags=["fallback"])


def page_out(row: FailedPageRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "url": row.url,
        "kind": row.kind,
        "reason": row.reason,
        "tier": row.tier,
        "status": row.status,
        "rows_added": row.rows_added,
        "provider": row.provider,
        "cost_usd": row.cost_usd,
        "error": row.error,
        "created_at": iso(row.created_at),
    }


def _runner(request: Request) -> FallbackRunner:
    return request.app.state.fallback  # type: ignore[no-any-return]


@router.get("/runs/{run_id}/failed")
def list_failed(request: Request, run_id: str, status: str | None = None) -> dict[str, Any]:
    store: Store = request.app.state.store
    rows = store.list_failed_pages(run_id, status=status)
    counts: dict[str, int] = {}
    for r in store.list_failed_pages(run_id):
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"pages": [page_out(r) for r in rows], "counts": counts}


@router.get("/runs/{run_id}/failed/{page_id}")
def get_failed(
    request: Request, run_id: str, page_id: str, markdown: bool = True
) -> dict[str, Any]:
    """One failed page with its markdown and the field spec — what an agent needs to extract."""
    store: Store = request.app.state.store
    row = store.get_failed_page(page_id)
    if not row or row.run_id != run_id:
        raise HTTPException(404, "failed page not found")
    recipe = _runner(request)._recipe_for(run_id)
    out = page_out(row)
    if recipe:
        out["fields"] = field_spec(recipe, row.kind)
        out["schema"] = json_schema(recipe, row.kind)
        out["base_row"] = row.base_row
    if markdown:
        out["markdown"] = page_markdown(row.html_path, row.url)
    return out


class RowsIn(BaseModel):
    rows: list[dict[str, Any]] = Field(min_length=0, max_length=2000)


@router.post("/runs/{run_id}/failed/{page_id}/rows")
def submit_rows(request: Request, run_id: str, page_id: str, body: RowsIn) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = store.get_failed_page(page_id)
    if not row or row.run_id != run_id:
        raise HTTPException(404, "failed page not found")
    if row.status not in ("pending", "failed", "skipped"):
        raise HTTPException(409, f"page already {row.status}")
    try:
        added, dropped = submit_agent_rows(
            store, request.app.state.bus, _runner(request), row, body.rows
        )
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"page_id": page_id, "rows_added": added, "dropped_fields": dropped}


@router.post("/runs/{run_id}/failed/{page_id}/skip")
def skip_page(request: Request, run_id: str, page_id: str) -> dict[str, Any]:
    store: Store = request.app.state.store
    row = store.get_failed_page(page_id)
    if not row or row.run_id != run_id:
        raise HTTPException(404, "failed page not found")
    return page_out(store.update_failed_page(page_id, status="skipped", error="skipped by user"))  # type: ignore[arg-type]


@router.post("/runs/{run_id}/fallback")
async def run_fallback(request: Request, run_id: str) -> dict[str, Any]:
    """Process this run's pending failed pages with the LLM fallback now (e.g. after adding a key)."""
    return await _runner(request).process_pending(run_id)


@router.post("/runs/{run_id}/ai-fields")
async def compute_ai_fields_now(request: Request, run_id: str) -> dict[str, Any]:
    """(Re)compute the recipe's AI fields (`extract.llm`) for this run's rows."""
    from scrapy_awesome.llm.ai_fields import run_ai_fields_for
    from scrapy_awesome.llm.base import LLMError

    st = request.app.state
    try:
        return await run_ai_fields_for(
            store=st.store,
            bus=st.bus,
            paths=st.paths,
            settings=st.settings,
            run_id=run_id,
            provider_factory=st.fallback._factory,
        )
    except KeyError:
        raise HTTPException(404, "run not found") from None
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
