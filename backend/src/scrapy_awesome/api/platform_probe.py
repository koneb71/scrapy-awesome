"""Server-side glue: detect a page's platform, confirm its API, remember the verdict.

Probes go through the crawl engine (the same snapshot worker the rest of design time uses), so
robots.txt, the stealth tiers, delays and the page cache all apply automatically — a disallowed
probe URL is dropped by Scrapy before it is ever requested. We fetch robots.txt alongside only to
*explain* a refusal in the UI.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from scrapy_awesome.recipe.models import Recipe
from scrapy_awesome.snapshot.platform import (
    ApiOffer,
    PlatformMatch,
    ProbeResult,
    apply_offer,
    best,
    confirm,
    detect,
    offer_patch,
    origin_of,
    probe_urls,
    robots_allows,
    robots_url,
)
from scrapy_awesome.store import SampleRow, Store

log = logging.getLogger(__name__)

MEMORY_TTL = 7 * 24 * 3600  # re-confirm a host at most weekly (research: verdicts are stable)


def _note_key(origin: str) -> str:
    return f"platform:{origin}"


def remembered(store: Store, origin: str) -> dict[str, Any] | None:
    raw = store.get_note(_note_key(origin))
    if not raw:
        return None
    with contextlib.suppress(ValueError):
        data = json.loads(raw)
        if time.time() - float(data.get("at", 0)) < MEMORY_TTL:
            return data
    return None


def remember(store: Store, origin: str, verdict: dict[str, Any]) -> None:
    store.set_note(_note_key(origin), json.dumps({**verdict, "at": time.time()}))


def _sample_headers(row: SampleRow) -> dict[str, Any]:
    return dict(row.headers or {})


async def detect_for_sample(
    *,
    store: Store,
    manager: Any,
    row: SampleRow,
    recipe: Recipe | None = None,
    probe: bool = True,
) -> dict[str, Any]:
    """Passive detection on an already-fetched page, then (optionally) one confirmation job.

    Returns the `platform` block that rides along with the page analysis:
        {detected, platform, label, score, signals, api: {...} | None, reason, probed}
    """
    url = row.final_url or row.url
    origin = origin_of(url)
    html = store.sample_html(row)
    headers = _sample_headers(row)
    matches = detect(url, html, headers)
    match = best(url, html, headers)
    out: dict[str, Any] = {
        "detected": bool(match),
        "platform": match.platform if match else None,
        "label": match.label if match else None,
        "score": match.score if match else 0,
        "candidates": [m.to_dict() for m in matches[:3]],
        "signals": [s.name for s in (match.signals if match else [])],
        "extras": dict(match.extras) if match else {},
        "api": None,
        "reason": "",
        "probed": False,
    }
    if match is None:
        out["reason"] = "no known platform"
        return out

    # The page path matters, not just the origin: /shop/ and / can be different storefronts, and
    # /collections/<handle> has an endpoint of its own. Each distinct page gets its own verdict.
    base = url.split("?")[0].rstrip("/") or url
    cached = remembered(store, base)
    if cached and cached.get("platform") == match.platform:
        out.update({k: cached[k] for k in ("api", "reason") if k in cached})
        out["probed"] = True
        out["cached"] = True
        return out

    urls = probe_urls(match, url)
    out["extras"] = dict(match.extras)  # probe_urls resolves relative bases (wp-json) — keep them
    if not urls:
        out["reason"] = f"{match.label} does not publish a public catalogue API"
        return out
    if not probe:
        return out

    rows = await manager.snapshot([robots_url(origin), *urls], recipe=None, kind="page")
    fetched = {r.url: r for r in rows}
    robots_row = fetched.get(robots_url(origin))
    robots_txt = store.sample_html(robots_row) if robots_row else ""
    # A blocked candidate is dropped, not fatal: a store may disallow /collections/*/products*
    # while publishing /products.json, and then the catalogue is still a legitimate answer.
    blocked = [u for u in urls if not robots_allows(robots_txt, u)]
    urls = [u for u in urls if u not in blocked]
    probes: dict[str, ProbeResult] = {}
    for u in urls:
        r = fetched.get(u)
        if r is None:
            continue
        probes[u] = ProbeResult(
            url=u,
            status=r.status,
            content_type=str(
                (r.headers or {}).get("Content-Type") or (r.headers or {}).get("content-type") or ""
            ),
            text=store.sample_html(r),
            final_url=r.final_url or u,
        )
    out["probed"] = True
    listings = [u for u in urls if "products.json" in u or "wp-json" in u or "/wc/" in u]
    if blocked and not listings:
        first = blocked[0]
        out["reason"] = f"robots.txt disallows {first.removeprefix(origin) or first}"
        remember(store, base, {"platform": match.platform, "api": None, "reason": out["reason"]})
        return out

    offer, why = confirm(match, url, probes)
    if offer is None:
        out["reason"] = why
        if blocked:
            out["reason"] += f" (robots.txt disallows {blocked[0].removeprefix(origin)})"
        remember(store, base, {"platform": match.platform, "api": None, "reason": why})
        return out
    api = offer.to_dict()
    # Shopify patches against the base that answered (a collection endpoint stays a collection
    # endpoint); WordPress/WooCommerce patch against the origin, since their base is wp_json_base.
    api["patch_origin"] = (
        offer.endpoint.removesuffix("/products.json") if match.platform == "shopify" else base
    )
    if blocked:
        api["robots_note"] = f"robots.txt disallows {blocked[0].removeprefix(origin).split('?')[0]}"
    out["api"] = api
    out["reason"] = offer.reason
    remember(store, base, {"platform": match.platform, "api": api, "reason": offer.reason})
    return out


def switch_to_api(
    recipe: dict[str, Any], platform_block: dict[str, Any], *, granularity: str = "product"
) -> dict[str, Any]:
    """Merge a confirmed offer into a recipe dict (HTML selectors kept as alternates)."""
    api = platform_block.get("api")
    if not api:
        raise ValueError("no confirmed API for this page")
    offer = ApiOffer(
        platform=api["platform"],
        label=api["label"],
        endpoint=api["endpoint"],
        reason=api["reason"],
        evidence=list(api.get("evidence") or []),
        currency=api.get("currency"),
        granularity=granularity,
    )
    extras = dict(platform_block.get("extras") or {})
    patch = offer_patch(offer, api.get("patch_origin") or origin_of(recipe["seeds"][0]), extras)
    merged = apply_offer(recipe, patch)
    note = f"Reading the {offer.label} API: {offer.endpoint}."
    if offer.currency:
        note += f" Prices are in {offer.currency}."
    merged["notes"] = (
        (merged.get("notes") or "" + "\n" + note).strip() if merged.get("notes") else note
    )
    return merged


__all__ = ["MEMORY_TTL", "PlatformMatch", "detect_for_sample", "remembered", "switch_to_api"]
