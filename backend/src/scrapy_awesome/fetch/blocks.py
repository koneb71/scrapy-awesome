"""Decide whether a response is blocked / a JS challenge / an empty JS app shell.

Pure functions over (status, headers, body) so the same logic runs in the Scrapy escalation
middleware, the design-time analyzer and unit tests. Signals (documented in
docs/spike-engine-coexistence.md and Cloudflare's own docs):

* status 403 / 429 / 503 → blocked (rate limit or WAF)
* `cf-mitigated: challenge` header → Cloudflare challenge (any status)
* challenge markers in a *short* body ("just a moment", `__cf_chl`, `px-captcha`, `datadome`, ...)
* WAF cookies/markers: `_Incapsula_Resource`, `ak-challenge`, `sec-if-cpt-container`
* needs_js: `<noscript>…enable JavaScript…`, an empty app-shell root (`#root/#app/#__next/#__nuxt`
  with no text), or an expected selector matching nothing on a page dominated by scripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

BLOCK_CODES = frozenset({403, 429, 430, 503})  # 430: Shopify Security Rejection

# Specific challenge markers (avoid bare vendor names — real product pages embed those in scripts).
CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "cf-browser-verification",
    "cf-chl",
    "__cf_chl",
    "challenge-platform",
    "challenges.cloudflare.com",
    "attention required",
    "px-captcha",
    "_px-captcha",
    "geo.captcha-delivery.com",  # DataDome
    "datadome",
    "_incapsula_resource",
    "incapsula incident",
    "sec-if-cpt-container",  # Akamai
    "ak-challenge",
    "behavioral-content",
    "access denied",
    "are you a human",
    "one more step",
    "security check",
    "unusual traffic",
    "recaptcha",
    "hcaptcha",
    "turnstile",
)
SHORT_BODY = 60_000  # bytes; challenge/stub pages are small, real pages with those words are big
_APP_SHELL_IDS = ("root", "app", "__next", "__nuxt", "___gatsby", "svelte", "q-app")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.I | re.S)
_NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>(.*?)</noscript>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_WS_RE = re.compile(r"\s+")

Reason = Literal[
    "status",
    "cf_mitigated",
    "challenge_marker",
    "noscript",
    "app_shell",
    "empty_body",
    "selector_missing",
]


@dataclass(frozen=True)
class BlockVerdict:
    blocked: bool  # anti-bot / rate limit / challenge — retry on a higher tier (or back off)
    needs_js: bool  # not blocked, but content is client-rendered — browser tier will help
    reason: Reason | None = None
    detail: str = ""

    @property
    def escalate(self) -> bool:
        return self.blocked or self.needs_js


def visible_text(html: str) -> str:
    """Cheap visible-text approximation without building a DOM."""
    s = _SCRIPT_RE.sub(" ", html)
    s = _STYLE_RE.sub(" ", s)
    s = _NOSCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def page_title(html: str) -> str:
    m = _TITLE_RE.search(html)
    return _WS_RE.sub(" ", m.group(1)).strip() if m else ""


def _header(headers: dict[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v if isinstance(v, str) else str(v)
    return ""


def classify_response(
    status: int,
    headers: dict[str, str] | None,
    body: str,
    *,
    expected_selector_matched: int | None = None,
) -> BlockVerdict:
    """Classify a fetched page.

    `expected_selector_matched` is the number of matches of the recipe's list container (or a key
    field) on this page, when the caller already ran it — 0 matches on a script-heavy page is the
    strongest "needs JS" signal we have.
    """
    body = body or ""
    lower = body[:SHORT_BODY].lower()
    title = page_title(body).lower()

    if _header(headers, "cf-mitigated").lower() == "challenge":
        return BlockVerdict(True, False, "cf_mitigated", "cf-mitigated: challenge header")

    marker = next((m for m in CHALLENGE_MARKERS if m in lower), None)
    if status in BLOCK_CODES:
        return BlockVerdict(
            True, False, "status", f"HTTP {status}" + (f" + '{marker}'" if marker else "")
        )
    if marker and (len(body) < SHORT_BODY or marker in title):
        return BlockVerdict(
            True,
            False,
            "challenge_marker",
            f"'{marker}' in {'title' if marker in title else 'body'}",
        )

    if expected_selector_matched is not None and expected_selector_matched > 0:
        return BlockVerdict(False, False)  # the content we want is there — rendered or not

    text = visible_text(body)
    for m in _NOSCRIPT_RE.finditer(body):
        ns = m.group(1).lower()
        asks_js = "javascript" in ns and ("enable" in ns or "requires" in ns or "turn on" in ns)
        if asks_js and len(text) < 400:
            return BlockVerdict(False, True, "noscript", "noscript asks to enable JavaScript")

    if not text and status < 400:
        return BlockVerdict(False, True, "empty_body", "no visible text")

    if len(text) < 200:
        for root_id in _APP_SHELL_IDS:
            if re.search(rf'<div[^>]+id=["\']{re.escape(root_id)}["\'][^>]*>\s*</div>', body, re.I):
                return BlockVerdict(False, True, "app_shell", f"empty #{root_id} app shell")

    if expected_selector_matched == 0:
        # The list container matched nothing. If the page runs any JavaScript, one browser attempt
        # is cheap insurance (a genuinely empty result page just stays empty).
        scripts = len(_SCRIPT_RE.findall(body))
        if scripts >= 1:
            return BlockVerdict(
                False,
                True,
                "selector_missing",
                f"selector matched 0; {scripts} scripts, {len(text)} chars text",
            )

    return BlockVerdict(False, False)
