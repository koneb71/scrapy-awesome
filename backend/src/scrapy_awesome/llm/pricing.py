"""Approximate list prices (USD per 1M tokens) for cost *estimates* shown in the UI and used
for budgets. Unknown models fall back to a conservative default. Update as vendors change."""

from __future__ import annotations

# (input, output, cache_read, cache_write) per 1M tokens
_ANTHROPIC = {
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
}
_GEMINI = {
    "gemini-3.7-flash": (0.5, 3.0, 0.05, 0.5),
    "gemini-3.1-pro-preview": (2.0, 12.0, 0.2, 2.0),
    "gemini-2.5-pro": (1.25, 10.0, 0.125, 1.25),
    "gemini-2.5-flash": (0.30, 2.50, 0.03, 0.30),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01, 0.10),
}
_DEFAULT = (5.0, 25.0, 0.5, 6.25)


def _table(provider: str) -> dict[str, tuple[float, float, float, float]]:
    return _ANTHROPIC if provider == "anthropic" else _GEMINI


def rates(provider: str, model: str) -> tuple[float, float, float, float]:
    t = _table(provider)
    if model in t:
        return t[model]
    # prefix match: "claude-opus-5-20260601" → "claude-opus-5"
    for k, v in sorted(t.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(k):
            return v
    return _DEFAULT


def cost_usd(
    provider: str,
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    i, o, cr, cw = rates(provider, model)
    return (
        input_tokens * i + output_tokens * o + cache_read_tokens * cr + cache_write_tokens * cw
    ) / 1_000_000
