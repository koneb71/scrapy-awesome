"""Worker → server endpoints (run-token authenticated)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from scrapy_awesome.api.auth import require_run_token

router = APIRouter(tags=["internal"])


class EventsIn(BaseModel):
    events: list[dict[str, Any]]


@router.post("/runs/{run_id}/events")
def post_events(request: Request, run_id: str, body: EventsIn) -> dict[str, Any]:
    require_run_token(request, run_id)
    request.app.state.manager.ingest(run_id, body.events)
    return {"ok": True, "n": len(body.events)}


@router.get("/runs/{run_id}/control")
def get_control(request: Request, run_id: str) -> dict[str, Any]:
    require_run_token(request, run_id)
    row = request.app.state.store.get_run(run_id)
    if row and row.status == "stopping":
        return {"cmd": "stop"}
    return {"cmd": None}
