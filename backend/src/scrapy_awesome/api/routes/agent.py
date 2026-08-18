"""Agent ↔ human hand-offs.

* **Pick requests** — an agent (MCP tool `request_pick`) asks the person to click an element in
  the app's picker: `POST /api/picks` → the UI shows the request and opens the picker; the person's
  choice lands in `POST /api/picks/{id}/answer`; the agent long-polls `GET /api/picks/{id}?wait=…`.
  Requests are ephemeral (in-memory) — they die with the server, which is what you want.
* **open_ui** — bring the app to the front on a given route (same machine; loopback only).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
import webbrowser
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["agent"])

PICK_KINDS = ("field", "container", "link", "pagination", "any")


class PickIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="field")
    recipe_id: str | None = None
    sample_id: str | None = None
    field_name: str | None = None
    hint: str | None = None  # e.g. an example value the agent expects to see


class PickAnswerIn(BaseModel):
    selector: str | None = None  # absolute (page-level) selector
    relative_selector: str | None = None  # relative to the container item (fields)
    container: str | None = None
    attr: str | None = None
    examples: list[str] = Field(default_factory=list)
    matches: int | None = None
    cancelled: bool = False
    note: str | None = None


class PickState:
    def __init__(self, req: PickIn) -> None:
        self.id = secrets.token_urlsafe(8)
        self.req = req
        self.status = "pending"  # pending | answered | cancelled | expired
        self.answer: dict[str, Any] | None = None
        self.created_at = time.time()
        self.answered_at: float | None = None
        self.event = asyncio.Event()

    def out(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "prompt": self.req.prompt,
            "kind": self.req.kind,
            "recipe_id": self.req.recipe_id,
            "sample_id": self.req.sample_id,
            "field_name": self.req.field_name,
            "hint": self.req.hint,
            "created_at": self.created_at,
        }
        if self.answer is not None:
            d["answer"] = self.answer
        return d


def _picks(request: Request) -> dict[str, PickState]:
    st = request.app.state
    if not hasattr(st, "picks"):
        st.picks = {}
    return st.picks  # type: ignore[no-any-return]


def _publish(request: Request, t: str, pick: PickState) -> None:
    request.app.state.bus.publish("agent", {"t": t, **pick.out()})


@router.post("/picks", status_code=201)
def create_pick(request: Request, body: PickIn) -> dict[str, Any]:
    if body.kind not in PICK_KINDS:
        raise HTTPException(422, f"kind must be one of {PICK_KINDS}")
    picks = _picks(request)
    # only one pending request at a time keeps the UI unambiguous
    for p in picks.values():
        if p.status == "pending":
            p.status = "cancelled"
            p.event.set()
            _publish(request, "pick_cancelled", p)
    pick = PickState(body)
    picks[pick.id] = pick
    _publish(request, "pick_request", pick)
    return pick.out()


@router.get("/picks")
def list_picks(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    picks = _picks(request)
    return [p.out() for p in picks.values() if status is None or p.status == status]


@router.get("/picks/{pick_id}")
async def get_pick(request: Request, pick_id: str, wait: float = 0) -> dict[str, Any]:
    """Long-poll: with `wait=N` (≤30 s) the response is delayed until answered or the wait ends."""
    pick = _picks(request).get(pick_id)
    if not pick:
        raise HTTPException(404, "pick not found")
    if pick.status == "pending" and wait > 0:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pick.event.wait(), timeout=min(wait, 30))
    return pick.out()


@router.post("/picks/{pick_id}/answer")
def answer_pick(request: Request, pick_id: str, body: PickAnswerIn) -> dict[str, Any]:
    pick = _picks(request).get(pick_id)
    if not pick:
        raise HTTPException(404, "pick not found")
    if pick.status != "pending":
        raise HTTPException(409, f"pick already {pick.status}")
    if body.cancelled:
        pick.status = "cancelled"
    else:
        if not (body.selector or body.relative_selector):
            raise HTTPException(422, "selector or relative_selector required")
        pick.status = "answered"
        pick.answer = body.model_dump(exclude={"cancelled"})
    pick.answered_at = time.time()
    pick.event.set()
    _publish(request, "pick_answered" if pick.status == "answered" else "pick_cancelled", pick)
    return pick.out()


class OpenIn(BaseModel):
    route: str = "/"


@router.post("/ui/open")
def open_ui(request: Request, body: OpenIn) -> dict[str, Any]:
    """Open (or focus) the app in the default browser at `route`, pre-authenticated."""
    route = body.route if body.route.startswith("/") else "/" + body.route
    base = request.app.state.base_url.rstrip("/")
    token = request.app.state.auth.token
    url = f"{base}/auth?token={token}&next={route}"
    bus = request.app.state.bus
    # If the UI is already open (a firehose WebSocket is connected) just navigate it in place;
    # otherwise open a fresh, pre-authenticated tab.
    ui_connected = bus.subscriber_count("*") > 0
    bus.publish("agent", {"t": "navigate", "route": route})
    ok = ui_connected
    if not ui_connected and not os.environ.get("SA_NO_BROWSER"):  # tests / headless CI
        try:
            ok = bool(webbrowser.open(url))
        except Exception:
            ok = False
    return {"opened": ok, "route": route, "url": f"{base}{route}", "in_app": ui_connected}
