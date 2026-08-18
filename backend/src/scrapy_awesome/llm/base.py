"""Provider-agnostic contracts for the in-app designer.

A *provider* runs one assistant **turn**: given a system prompt, the conversation so far (in the
provider's own native format, opaque to us), the user's new message and a set of tools, it
streams events (text deltas, tool calls/results, usage) and returns the updated native history
plus usage/cost. The session layer (llm/designer.py) owns budgets, persistence and the UI feed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# ---- events streamed to the UI ------------------------------------------------------------
# {"t": "text_delta", "text": str}
# {"t": "thinking_delta", "text": str}
# {"t": "tool_call", "id": str, "name": str, "input": dict}
# {"t": "tool_result", "id": str, "name": str, "ok": bool, "summary": str}
# {"t": "usage", ...Usage}
# {"t": "done", "text": str, "stop_reason": str}   /  {"t": "error", "message": str}
Event = dict[str, Any]
OnEvent = Callable[[Event], Awaitable[None] | None]


class LLMError(RuntimeError):
    """User-facing provider failure (bad key, model not found, quota, refusal, budget…)."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd
        self.calls += other.calls

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "calls": self.calls,
        }


@dataclass
class Budget:
    """USD ceiling for a session; providers check it between model calls."""

    limit_usd: float | None
    spent_usd: float = 0.0

    def charge(self, cost: float) -> None:
        self.spent_usd += cost
        if self.limit_usd is not None and self.spent_usd > self.limit_usd:
            raise BudgetExceeded(
                f"session budget exceeded (${self.spent_usd:.2f} > ${self.limit_usd:.2f}). "
                "Raise it in Settings → AI providers to continue."
            )

    @property
    def remaining_usd(self) -> float | None:
        return None if self.limit_usd is None else max(0.0, self.limit_usd - self.spent_usd)


@dataclass
class ToolSpec:
    """A callable tool + its JSON schema (shared shape for both providers)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Awaitable[Any]]


@dataclass
class TurnResult:
    text: str
    history: list[Any]  # provider-native messages after this turn (JSON-serialisable)
    usage: Usage
    stop_reason: str = "end_turn"
    tool_calls: int = 0


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "display_name": self.display_name or self.id, **self.extra}


class LLMProvider(Protocol):
    name: str

    async def run_turn(
        self,
        *,
        model: str,
        system: str,
        history: list[Any],
        user_message: str,
        tools: list[ToolSpec],
        effort: Effort,
        budget: Budget,
        on_event: OnEvent,
        max_iterations: int = 40,
    ) -> TurnResult: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def extract_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], budget: Budget
    ) -> tuple[Any, Usage]:
        """One-shot structured extraction (per-page fallback, Phase 6)."""
        ...


async def emit(on_event: OnEvent, ev: Event) -> None:
    r = on_event(ev)
    if r is not None:
        await r
