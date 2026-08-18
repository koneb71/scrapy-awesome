"""Provider factory (keys from the SecretStore) and cached live model lists."""

from __future__ import annotations

import os
import time
from typing import Any

from scrapy_awesome.config import Paths, SecretStore
from scrapy_awesome.llm.base import LLMError, LLMProvider, ModelInfo

# Shown when the key is missing or the list call fails (kept in sync with docs/providers.md).
FALLBACK_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8"],
    "gemini": ["gemini-3.7-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash"],
}
_KEY_NAME = {"anthropic": "anthropic_api_key", "gemini": "gemini_api_key"}
FALLBACK_MODELS["claude_code"] = ["claude-opus-5", "claude-sonnet-5"]
_CACHE_TTL = 600.0
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def api_key_for(provider: str, paths: Paths) -> str | None:
    name = _KEY_NAME.get(provider)
    if not name:
        return None
    value, _source = SecretStore(paths).get(name)  # type: ignore[arg-type]
    return value


def make_provider(provider: str, paths: Paths) -> LLMProvider:
    if os.environ.get("SA_FAKE_LLM"):  # dev/tests: offline scripted designer, no key needed
        from scrapy_awesome.llm.fake_provider import FakeDesignerProvider

        return FakeDesignerProvider()
    key = api_key_for(provider, paths)
    if provider == "anthropic":
        from scrapy_awesome.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(key or "")
    if provider == "gemini":
        from scrapy_awesome.llm.gemini_provider import GeminiProvider

        return GeminiProvider(key or "")
    if provider == "claude_code":
        from scrapy_awesome.config import UserSettings
        from scrapy_awesome.llm.claude_code_provider import ClaudeCodeProvider

        enabled = UserSettings.load(paths).llm.cli_login_enabled
        return ClaudeCodeProvider(enabled=enabled)
    raise LLMError(f"unknown provider {provider!r}")


async def list_models(provider: str, paths: Paths, *, refresh: bool = False) -> dict[str, Any]:
    """`{provider, models: [{id, display_name}], source: live|fallback, error?}` (cached 10 min)."""
    now = time.monotonic()
    hit = _cache.get(provider)
    if hit and not refresh and now - hit[0] < _CACHE_TTL:
        return {"provider": provider, "models": hit[1], "source": "live", "cached": True}
    fallback = [ModelInfo(id=m).to_dict() for m in FALLBACK_MODELS.get(provider, [])]
    if provider == "claude_code":
        return {"provider": provider, "models": fallback, "source": "fallback"}
    if not api_key_for(provider, paths):
        return {
            "provider": provider,
            "models": fallback,
            "source": "fallback",
            "error": "no API key",
        }
    try:
        p = make_provider(provider, paths)
        models = [m.to_dict() for m in await p.list_models()]
        if models:
            _cache[provider] = (now, models)
            return {"provider": provider, "models": models, "source": "live"}
        return {
            "provider": provider,
            "models": fallback,
            "source": "fallback",
            "error": "empty list",
        }
    except LLMError as exc:
        return {"provider": provider, "models": fallback, "source": "fallback", "error": str(exc)}
