"""In-app designer chats (Claude / Gemini via API keys) + live model lists."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from scrapy_awesome.llm.base import LLMError
from scrapy_awesome.llm.designer import ChatManager, chat_out
from scrapy_awesome.llm.registry import list_models

router = APIRouter(tags=["chat"])


class ChatIn(BaseModel):
    recipe_id: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    title: str = ""


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


def _mgr(request: Request) -> ChatManager:
    return request.app.state.chats  # type: ignore[no-any-return]


@router.get("/llm/models")
async def llm_models(
    request: Request, provider: str = "anthropic", refresh: bool = False
) -> dict[str, Any]:
    if provider not in ("anthropic", "gemini", "claude_code"):
        raise HTTPException(422, "provider must be anthropic, gemini or claude_code")
    return await list_models(provider, request.app.state.paths, refresh=refresh)


@router.post("/chats", status_code=201)
def create_chat(request: Request, body: ChatIn) -> dict[str, Any]:
    if body.provider and body.provider not in ("anthropic", "gemini", "claude_code"):
        raise HTTPException(422, "provider must be anthropic, gemini or claude_code")
    row = _mgr(request).create(
        recipe_id=body.recipe_id,
        provider=body.provider,
        model=body.model,
        effort=body.effort,
        title=body.title,
    )
    return chat_out(row)


@router.get("/chats")
def list_chats(request: Request, recipe_id: str | None = None) -> list[dict[str, Any]]:
    mgr = _mgr(request)
    return [
        chat_out(r, mgr.is_running(r.id)) for r in request.app.state.store.list_chats(recipe_id)
    ]


@router.get("/chats/{chat_id}")
def get_chat(request: Request, chat_id: str) -> dict[str, Any]:
    row = request.app.state.store.get_chat(chat_id)
    if not row:
        raise HTTPException(404, "chat not found")
    return chat_out(row, _mgr(request).is_running(chat_id))


@router.post("/chats/{chat_id}/messages", status_code=202)
async def send_message(request: Request, chat_id: str, body: MessageIn) -> dict[str, Any]:
    mgr = _mgr(request)
    try:
        row = await mgr.send(chat_id, body.content)
    except KeyError:
        raise HTTPException(404, "chat not found") from None
    except LLMError as exc:  # missing key / unknown provider (LLMError is a RuntimeError)
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return chat_out(row, True)


@router.post("/chats/{chat_id}/cancel")
async def cancel_chat(request: Request, chat_id: str) -> dict[str, Any]:
    cancelled = await _mgr(request).cancel(chat_id)
    return {"id": chat_id, "cancelled": cancelled}


@router.delete("/chats/{chat_id}")
async def delete_chat(request: Request, chat_id: str) -> dict[str, Any]:
    await _mgr(request).cancel(chat_id)
    request.app.state.store.delete_chat(chat_id)
    return {"id": chat_id, "deleted": True}
