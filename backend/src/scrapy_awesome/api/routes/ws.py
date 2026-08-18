"""WebSocket streams: per-run events and the firehose."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from scrapy_awesome.api.auth import is_authenticated

router = APIRouter()


async def _stream(ws: WebSocket, topic: str) -> None:
    app = ws.app
    if not is_authenticated(ws, app.state.auth):
        await ws.close(code=4401)
        return
    await ws.accept()
    bus = app.state.bus
    q = bus.subscribe(topic)
    try:
        # replay: current status so late joiners know where things stand
        if topic.startswith("chat:"):
            snap = app.state.chats.live_snapshot(topic.split(":", 1)[1])
            if snap:
                await ws.send_text(json.dumps(snap, default=str))
        elif topic != "*":
            row = app.state.store.get_run(topic)
            if row:
                await ws.send_text(
                    json.dumps(
                        {
                            "t": "status",
                            "run_id": row.id,
                            "status": row.status,
                            "items": row.items,
                            "pages": row.pages,
                        }
                    )
                )
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=20)
            except TimeoutError:
                await ws.send_text(json.dumps({"t": "ping"}))
                continue
            await ws.send_text(json.dumps(ev, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(topic, q)
        with contextlib.suppress(Exception):
            await ws.close()


@router.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str) -> None:
    await _stream(ws, run_id)


@router.websocket("/ws/events")
async def ws_all(ws: WebSocket) -> None:
    await _stream(ws, "*")


@router.websocket("/ws/chats/{chat_id}")
async def ws_chat(ws: WebSocket, chat_id: str) -> None:
    await _stream(ws, f"chat:{chat_id}")
