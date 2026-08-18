"""FetchPolicy: one description of *how* to fetch, compiled to engine-specific request meta.

Tiers (see docs/spike-engine-coexistence.md):

  http         scrapy-stealth HTTP driver (wreq/curl_cffi TLS impersonation)      meta["stealth"] = {...}
  browser      scrapy-stealth browser driver (real Chrome via CDP, settle-based)  meta["stealth"] = {"driver": "browser"}
  interactive  scrapy-playwright + Patchright (waits, scroll, click, sessions)    meta["playwright"] = True

Routing rule: a `stealth` dict → stealth engines; `stealth: False` + `playwright: True` → Patchright.
The policy also stamps `meta["sa"]` with our own bookkeeping (tier, attempt) for the escalation
middleware and the item provenance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from scrapy_awesome.recipe.models import Action, FetchConfig, Recipe, Tier

ConcreteTier = Literal["http", "browser", "interactive"]
TIER_ORDER: tuple[ConcreteTier, ...] = ("http", "browser", "interactive")
META_KEY = "sa"

# JS helper used by scroll_until_stable: scroll to the bottom until the document stops growing.
SCROLL_UNTIL_STABLE_JS = """
async ({maxRounds, delayMs}) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let stable = 0, last = -1, rounds = 0;
  while (rounds < maxRounds && stable < 3) {
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(delayMs);
    const h = document.body.scrollHeight;
    if (h === last) stable++; else stable = 0;
    last = h; rounds++;
  }
  return {rounds, height: last};
}
"""


def next_tier(tier: ConcreteTier) -> ConcreteTier | None:
    i = TIER_ORDER.index(tier)
    return TIER_ORDER[i + 1] if i + 1 < len(TIER_ORDER) else None


@dataclass
class FetchPolicy:
    tier: Tier = "auto"
    profile: str = "chrome"
    proxy: str | None = None
    settle_seconds: float | None = None
    timeout_seconds: int = 30
    headers: dict[str, str] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    wait_for: str | None = None
    session_id: str | None = None
    storage_state_path: str | None = None  # resolved by the caller from session_id
    block_static_assets: bool = True
    headless: bool = True

    # ---- construction --------------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        cfg: FetchConfig,
        *,
        tier_override: str | None = None,
        storage_state_path: str | None = None,
        headless: bool = True,
    ) -> FetchPolicy:
        return cls(
            tier=tier_override or cfg.tier,  # type: ignore[arg-type]
            profile=cfg.profile,
            proxy=cfg.proxy,
            settle_seconds=cfg.settle_seconds,
            timeout_seconds=cfg.timeout_seconds,
            headers=dict(cfg.headers),
            actions=list(cfg.actions),
            wait_for=cfg.wait_for,
            session_id=cfg.session,
            storage_state_path=storage_state_path,
            block_static_assets=cfg.block_static_assets,
            headless=headless,
        )

    @classmethod
    def from_recipe(
        cls,
        recipe: Recipe,
        *,
        for_detail: bool = False,
        tier_override: str | None = None,
        storage_state_path: str | None = None,
        headless: bool = True,
    ) -> FetchPolicy:
        cfg = recipe.fetch
        if for_detail and recipe.detail.fetch is not None:
            cfg = recipe.detail.fetch
        return cls.from_config(
            cfg,
            tier_override=tier_override,
            storage_state_path=storage_state_path,
            headless=headless,
        )

    # ---- (de)serialization — the policy travels inside request meta ------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["actions"] = [a.model_dump(mode="json", exclude_none=True) for a in self.actions]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FetchPolicy:
        d = dict(d)
        d["actions"] = [Action.model_validate(a) for a in d.get("actions", [])]
        return cls(**d)

    # ---- tier resolution -----------------------------------------------------------------
    @property
    def needs_interactive(self) -> bool:
        return bool(self.actions or self.wait_for or self.session_id)

    def initial_tier(self, remembered: ConcreteTier | None = None) -> ConcreteTier:
        """Concrete tier for the first attempt. `remembered` = per-domain tier that worked before."""
        if self.tier != "auto":
            return self.tier  # explicit
        if self.needs_interactive:
            return "interactive"
        return remembered or "http"

    def escalation_allowed(self) -> bool:
        return self.tier == "auto"

    # ---- meta compilation ----------------------------------------------------------------
    def to_meta(
        self, tier: ConcreteTier, *, attempt: int = 0, extra: dict | None = None
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            META_KEY: {
                "tier": tier,
                "attempt": attempt,
                "policy_tier": self.tier,
                "policy": self.to_dict(),
            },
            "download_timeout": self.timeout_seconds,
            "handle_httpstatus_all": True,  # spider/middleware decide what a 4xx/5xx means
        }
        if tier in ("http", "browser"):
            # http tier = scrapy-stealth's "turbo" HTTP driver; we own escalation, so never "auto"
            # (the library's auto-fallback forces a *headed* Chrome window).
            stealth: dict[str, Any] = {
                "driver": "browser" if tier == "browser" else "turbo",
                "profile": self.profile,
                "stealth_timeout": self.timeout_seconds,
                "fallback": False,
            }
            if self.proxy:
                stealth["proxy"] = self.proxy
            if tier == "browser":
                stealth["headless"] = self.headless
                if self.settle_seconds is not None:
                    stealth["settle"] = self.settle_seconds
                stealth["static_assets_block"] = self.block_static_assets
            meta["stealth"] = stealth
        else:  # interactive
            meta["stealth"] = False
            meta["playwright"] = True
            meta["playwright_page_methods"] = self.page_methods()
            if self.block_static_assets:
                meta["playwright_abort_static"] = (
                    True  # consumed by our PLAYWRIGHT_ABORT_REQUEST hook
                )
            if self.session_id:
                meta["playwright_context"] = f"session-{self.session_id}"
                ctx_kwargs: dict[str, Any] = {}
                if self.storage_state_path:
                    ctx_kwargs["storage_state"] = self.storage_state_path
                if self.proxy:
                    ctx_kwargs["proxy"] = {"server": self.proxy}
                if ctx_kwargs:
                    meta["playwright_context_kwargs"] = ctx_kwargs
            elif self.proxy:
                meta["playwright_context"] = f"proxy-{abs(hash(self.proxy)) % 10_000}"
                meta["playwright_context_kwargs"] = {"proxy": {"server": self.proxy}}
        if self.headers:
            meta.setdefault("sa_headers", dict(self.headers))
        if extra:
            meta.update(extra)
        return meta

    def page_methods(self) -> list[Any]:
        """Compile actions (+ wait_for shortcut) to scrapy-playwright PageMethods."""
        from scrapy_playwright.page import PageMethod

        methods: list[Any] = []
        if self.wait_for:
            methods.append(
                PageMethod("wait_for_selector", self.wait_for, timeout=self.timeout_seconds * 1000)
            )
        for a in self.actions:
            methods.extend(_compile_action(a, self.timeout_seconds))
        return methods


def _compile_action(a: Action, timeout_s: int) -> list[Any]:
    from scrapy_playwright.page import PageMethod

    t = timeout_s * 1000
    if a.kind == "wait_for":
        return [PageMethod("wait_for_selector", a.selector, timeout=t, state="attached")]
    if a.kind == "wait_ms":
        return [PageMethod("wait_for_timeout", a.ms)]
    if a.kind == "scroll_until_stable":
        return [
            PageMethod(
                "evaluate",
                SCROLL_UNTIL_STABLE_JS,
                {"maxRounds": a.max_rounds or 40, "delayMs": a.ms or 250},
            )
        ]
    if a.kind == "scroll":
        n = a.times or 1
        out: list[Any] = []
        for _ in range(n):
            out.append(PageMethod("evaluate", "window.scrollTo(0, document.body.scrollHeight)"))
            out.append(PageMethod("wait_for_timeout", a.ms or 400))
        return out
    if a.kind == "click":
        n = a.times or 1
        out = []
        for _ in range(n):
            # `optional` clicks use a JS click that no-ops when the element is missing
            if a.optional:
                out.append(
                    PageMethod(
                        "evaluate",
                        "(sel) => { const el = document.querySelector(sel); if (el) el.click(); return !!el; }",
                        a.selector,
                    )
                )
            else:
                out.append(PageMethod("click", a.selector, timeout=t))
            out.append(PageMethod("wait_for_timeout", a.ms or 500))
        return out
    if a.kind == "fill":
        return [PageMethod("fill", a.selector, a.value, timeout=t)]
    if a.kind == "press":
        return [PageMethod("press", a.selector, a.value, timeout=t)]
    if a.kind == "evaluate":
        return [PageMethod("evaluate", a.js)]
    raise ValueError(f"unknown action kind {a.kind!r}")


def tier_of(request_meta: dict) -> ConcreteTier | None:
    sa = request_meta.get(META_KEY)
    return sa.get("tier") if isinstance(sa, dict) else None
