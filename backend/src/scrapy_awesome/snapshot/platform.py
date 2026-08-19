"""Which platform is this site, and does it publish a JSON API we can read instead of the HTML?

Two stages, deliberately:

1. **Passive scoring** over the page we already fetched — response headers, Set-Cookie, and body
   markers. Zero extra requests. A platform counts as detected at `THRESHOLD` points *and* at
   least one `STRONG` signal, so three weak markers (a Shopify buy-button embedded in a
   WordPress site, say) can never add up to a false positive.
2. **Confirmation**, one to three cheap GETs, and only for platforms that actually expose a
   catalogue endpoint: robots.txt first (probing a path you have been told not to fetch is
   itself the impolite act), then the endpoint itself. Nothing is offered to the user until a
   real response has been parsed.

The recipe an offer produces keeps the HTML selectors as *alternates*, so a single recipe reads
the API when it answers and falls back to parsing the page when it does not.

Signals and endpoint shapes were verified against live stores in Aug 2026; see docs/api-mode.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scrapy_awesome.recipe.models import JSON_PREFIX

THRESHOLD = 6  # points needed before a platform counts as detected
STRONG = 4  # ...at least one signal must be worth this much on its own


@dataclass(frozen=True)
class Signal:
    name: str
    weight: int
    detail: str = ""


@dataclass
class PlatformMatch:
    platform: str
    label: str
    signals: list[Signal] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(s.weight for s in self.signals)

    @property
    def detected(self) -> bool:
        return self.score >= THRESHOLD and any(s.weight >= STRONG for s in self.signals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label,
            "score": self.score,
            "detected": self.detected,
            "signals": [asdict(s) for s in self.signals],
            "extras": self.extras,
        }


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


def _header(headers: dict[str, Any], name: str) -> str:
    """Headers arrive from the store as a dict with unpredictable casing/list values."""
    for k, v in (headers or {}).items():
        if k.lower() == name:
            if isinstance(v, list | tuple):
                v = v[0] if v else ""
            return str(v)
    return ""


def _all_headers(headers: dict[str, Any]) -> str:
    return "\n".join(f"{k}: {_header(headers, k.lower())}" for k in (headers or {}))


# ---------------------------------------------------------------------------------- detectors
# `Shopify.shop = "x.myshopify.com"`, `window.Shopify = {shop: "..."}` and checkout links all
# carry the canonical origin; the domain is self-identifying, so match it anywhere in the body.
_SHOPIFY_SHOP = re.compile(r"([\w-]+\.myshopify\.com)")
_SHOPIFY_CURRENCY = re.compile(
    r"""currency\s*[:=]\s*\{[^}]*["']?active["']?\s*:\s*["'](\w{3})["']"""
)
_WP_JSON = re.compile(
    r"""<link[^>]+rel=["']https://api\.w\.org/["'][^>]+href=["']([^"']+)["']""", re.I
)
_WP_JSON_ALT = re.compile(
    r"""<link[^>]+href=["']([^"']+)["'][^>]+rel=["']https://api\.w\.org/["']""", re.I
)
_LINK_HEADER_WP = re.compile(r"<([^>]+)>;\s*rel=[\"']?https://api\.w\.org/", re.I)


def _shopify(url: str, html: str, headers: dict[str, Any]) -> PlatformMatch:
    m = PlatformMatch("shopify", "Shopify")
    powered = _header(headers, "powered-by")
    if "shopify" in powered.lower():
        m.signals.append(Signal("powered-by header", 6, powered))
    st = _header(headers, "server-timing")
    if "theme;desc" in st or "pageType;desc" in st:
        m.signals.append(Signal("server-timing theme/pageType", 4, st[:80]))
    if _header(headers, "shopify-complexity-score") or _header(
        headers, "shopify-complexity-score-v2"
    ):
        m.signals.append(Signal("shopify-complexity-score header", 4))
    cookies = _header(headers, "set-cookie")
    if any(c in cookies for c in ("_shopify_y", "_shopify_s", "_shopify_essential")):
        m.signals.append(Signal("_shopify_* cookies", 4))
    shop = _SHOPIFY_SHOP.search(html or "")
    if shop:
        m.signals.append(Signal("Shopify.shop global", 5, shop.group(1)))
        m.extras["myshopify_host"] = shop.group(1)
    cur = _SHOPIFY_CURRENCY.search(html or "")
    if cur:
        m.extras["currency"] = cur.group(1)
    if "ShopifyAnalytics" in (html or "") or "shopify-section-" in (html or ""):
        m.signals.append(Signal("theme markup (ShopifyAnalytics / shopify-section)", 3))
    if "shopify-checkout-api-token" in (html or "") or "shopify-digital-wallet" in (html or ""):
        m.signals.append(Signal("storefront meta tags", 3))
    if "cdn.shopify.com" in (html or "") or "/cdn/shop/" in (html or ""):
        # weak on purpose: any site can embed a Shopify buy button or hotlink its CDN
        m.signals.append(Signal("cdn.shopify.com assets", 2))
    return m


def _wordpress(url: str, html: str, headers: dict[str, Any]) -> PlatformMatch:
    m = PlatformMatch("wordpress", "WordPress")
    link = _LINK_HEADER_WP.search(_header(headers, "link"))
    if link:
        m.signals.append(Signal("Link rel=api.w.org header", 5, link.group(1)))
        m.extras["wp_json_base"] = link.group(1)
    tag = _WP_JSON.search(html or "") or _WP_JSON_ALT.search(html or "")
    if tag:
        m.signals.append(Signal("<link rel=api.w.org>", 5, tag.group(1)))
        m.extras.setdefault("wp_json_base", tag.group(1))
    if "/wp-content/" in (html or "") or "/wp-includes/" in (html or ""):
        m.signals.append(Signal("wp-content / wp-includes assets", 3))
    if re.search(
        r"""<meta[^>]+name=["']generator["'][^>]+content=["']WordPress""", html or "", re.I
    ):
        m.signals.append(Signal("generator meta", 3))
    return m


def _woocommerce(url: str, html: str, headers: dict[str, Any]) -> PlatformMatch:
    m = PlatformMatch("woocommerce", "WooCommerce")
    if re.search(r"""<body[^>]+class=["'][^"']*woocommerce""", html or "", re.I):
        m.signals.append(Signal("woocommerce body class", 4))
    if re.search(
        r"""<meta[^>]+name=["']generator["'][^>]+content=["']WooCommerce""", html or "", re.I
    ):
        m.signals.append(Signal("generator meta", 4))
    if "wc-cart-fragments" in (html or "") or "/plugins/woocommerce/" in (html or ""):
        m.signals.append(Signal("woocommerce assets", 3))
    if "?add-to-cart=" in (html or ""):
        m.signals.append(Signal("add-to-cart links", 2))
    return m


def _simple(platform: str, label: str, rules: list[tuple[str, int, str]]):
    """Label-only platforms: we can name them, but they publish no tokenless catalogue API."""

    def detect(url: str, html: str, headers: dict[str, Any]) -> PlatformMatch:
        m = PlatformMatch(platform, label)
        blob = f"{html or ''}\n{_all_headers(headers)}"
        for needle, weight, name in rules:
            if needle in blob:
                m.signals.append(Signal(name, weight, needle))
        return m

    return detect


_DETECTORS = [
    _shopify,
    _wordpress,
    _woocommerce,
    _simple(
        "bigcommerce",
        "BigCommerce",
        [
            ("mybigcommerce.com", 5, "mybigcommerce canonical"),
            ("stencil-utils", 4, "stencil theme runtime"),
            ("cdn11.bigcommerce.com", 5, "bigcommerce CDN"),
        ],
    ),
    _simple(
        "squarespace",
        "Squarespace",
        [
            ("SQUARESPACE_CONTEXT", 6, "Static.SQUARESPACE_CONTEXT"),
            ("static1.squarespace.com", 5, "squarespace CDN"),
            ("squarespace-cdn.com", 5, "squarespace CDN"),
        ],
    ),
    _simple(
        "wix",
        "Wix",
        [
            ("static.parastorage.com", 5, "parastorage assets"),
            ("wix-code", 4, "wix runtime"),
            ("X-Wix-Request-Id", 5, "wix header"),
        ],
    ),
    _simple(
        "magento",
        "Magento",
        [
            ("Magento_", 4, "Magento modules"),
            ("/static/frontend/", 3, "magento static paths"),
            ("mage/cookies", 4, "mage runtime"),
        ],
    ),
    _simple(
        "webflow",
        "Webflow",
        [
            ("webflow.js", 4, "webflow runtime"),
            ("data-wf-page", 5, "webflow page attributes"),
        ],
    ),
]


def detect(url: str, html: str, headers: dict[str, Any] | None = None) -> list[PlatformMatch]:
    """Score every platform against one already-fetched page. No requests. Best first."""
    headers = headers or {}
    matches = [d(url, html or "", headers) for d in _DETECTORS]
    matches = [m for m in matches if m.signals]
    # WooCommerce is a WordPress plugin: without WordPress underneath it is a false positive.
    wp = next((m for m in matches if m.platform == "wordpress" and m.detected), None)
    if wp is None:
        matches = [m for m in matches if m.platform != "woocommerce"]
    matches.sort(key=lambda m: (m.detected, m.score), reverse=True)
    return matches


def best(url: str, html: str, headers: dict[str, Any] | None = None) -> PlatformMatch | None:
    for m in detect(url, html, headers):
        if m.detected:
            return m
    return None


# ------------------------------------------------------------------------------- confirmation
@dataclass
class ProbeResult:
    url: str
    status: int = 0
    content_type: str = ""
    text: str = ""
    final_url: str = ""

    @property
    def json(self) -> Any:
        """Parsed body, or None. Status alone lies: blocked endpoints answer 404/403/429 with
        HTML app shells, and a frontend router can answer 200 with HTML."""
        if not self.text or "json" not in self.content_type.lower():
            return None
        try:
            return json.loads(self.text)
        except ValueError:
            return None


@dataclass
class ApiOffer:
    """A confirmed, robots-allowed JSON endpoint we can read instead of the page."""

    platform: str
    label: str
    endpoint: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    currency: str | None = None
    granularity: str = "product"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Path segments Shopify owns. Everything before the first one is the storefront root, which is
# usually "" but can be a sub-path ("/shop/") on multi-site setups.
SHOPIFY_ROUTES = frozenset(
    {
        "collections",
        "products",
        "pages",
        "blogs",
        "cart",
        "checkout",
        "search",
        "account",
        "policies",
        "apps",
        "tools",
        "discount",
        "challenge",
        "password",
        "recommendations",
        "services",
        "variants",
        "a",
        "wpm",
    }
)


def shopify_bases(url: str) -> list[str]:
    """Where `<base>/products.json` should be looked for, most specific first.

    Shopify serves the same endpoint at two depths: `/collections/<handle>/products.json` returns
    just that collection, `/products.json` the whole catalogue. Paste a collection page and the
    collection is what you meant — so it is probed first, with the storefront root behind it for
    stores that turn collection endpoints off (and for every other kind of page).
    """
    parts = urlsplit(url)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    segments = [s for s in parts.path.split("/") if s]
    root_len = next(
        (i for i, s in enumerate(segments) if s.lower() in SHOPIFY_ROUTES), len(segments)
    )
    # a trailing file ("index.html") is not part of the root either
    if root_len == len(segments) and segments and "." in segments[-1]:
        root_len -= 1
    bases: list[str] = []
    if "collections" in [s.lower() for s in segments]:
        i = [s.lower() for s in segments].index("collections")
        if i + 1 < len(segments):
            bases.append(origin + "/" + "/".join(segments[: i + 2]))
    root = origin + ("/" + "/".join(segments[:root_len]) if root_len else "")
    if root not in bases:
        bases.append(root)
    return bases


def shopify_store_root(base: str) -> str:
    """The storefront a collection base belongs to — where `/products/<handle>` pages live."""
    parts = urlsplit(base)
    segments = [s for s in parts.path.split("/") if s]
    lowered = [s.lower() for s in segments]
    if "collections" in lowered:
        segments = segments[: lowered.index("collections")]
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return origin + ("/" + "/".join(segments) if segments else "")


def shopify_collection(base: str) -> str | None:
    """The collection handle a base targets, or None for a whole-catalogue base."""
    segments = [s for s in urlsplit(base).path.split("/") if s]
    lowered = [s.lower() for s in segments]
    if "collections" in lowered and lowered.index("collections") + 1 < len(segments):
        return segments[lowered.index("collections") + 1]
    return None


def _short(base: str) -> str:
    """How a base reads in a message: its path ("/collections/sale"), else its host."""
    return urlsplit(base).path.rstrip("/") or urlsplit(base).netloc


def robots_url(origin: str) -> str:
    return f"{origin}/robots.txt"


def probe_urls(match: PlatformMatch, origin: str) -> list[str]:
    """Endpoints to confirm for this *page* (robots.txt is fetched separately, first).

    `origin` is the page URL: what to probe depends on where in the store it is.
    """
    if match.platform == "shopify":
        bases = shopify_bases(origin)
        return [
            f"{shopify_store_root(bases[-1])}/meta.json",
            *[f"{b}/products.json?limit=1" for b in bases],
        ]
    if match.platform in ("woocommerce", "wordpress"):
        base = (match.extras.get("wp_json_base") or f"{origin}/wp-json/").rstrip("/") + "/"
        if not base.startswith("http"):
            # the href is origin-relative ("/blog/wp-json/"), not relative to the page path
            base = origin_of(origin).rstrip("/") + "/" + base.lstrip("/")
        match.extras["wp_json_base"] = base
        path = (
            "wc/store/v1/products?per_page=1"
            if match.platform == "woocommerce"
            else "wp/v2/posts?per_page=1"
        )
        return [base + path]
    return []  # BigCommerce/Squarespace/Wix/Magento: named, but no tokenless catalogue API


def robots_allows(robots_txt: str, url: str, agent: str = "*") -> bool:
    """True when robots.txt permits `url`. Unreachable robots (empty text) means allow, per
    RFC 9309's 4xx rule; a 5xx is treated as a refusal by the caller, which passes no text."""
    if not robots_txt.strip():
        return True
    try:
        from protego import Protego

        return bool(Protego.parse(robots_txt).can_fetch(url, agent))
    except Exception:  # pragma: no cover - never let a parser bug block a user
        return True


def confirm(
    match: PlatformMatch, origin: str, probes: dict[str, ProbeResult], name: str = ""
) -> tuple[ApiOffer | None, str]:
    """(offer, reason) — reason explains a refusal so the UI can say *why* it stayed on HTML."""
    if match.platform == "shopify":
        bases = shopify_bases(origin)
        meta = probes.get(f"{shopify_store_root(bases[-1])}/meta.json")
        currency = match.extras.get("currency")
        evidence = []
        if meta is not None and isinstance(meta.json, dict) and "currency" in meta.json:
            currency = meta.json.get("currency") or currency
            evidence.append(f"/meta.json → shop id {meta.json.get('id')}, currency {currency}")
        why = "the products endpoint was not reachable"
        for base in bases:  # the collection you pasted first, the whole catalogue behind it
            listing = probes.get(f"{base}/products.json?limit=1")
            if listing is None:
                continue
            doc = listing.json
            if not isinstance(doc, dict) or not isinstance(doc.get("products"), list):
                detail = f"HTTP {listing.status}, {listing.content_type or 'no content-type'}"
                why = f"{_short(base)}/products.json did not return a product list ({detail})"
                continue
            if not listing.final_url.endswith("products.json?limit=1"):
                why = f"{_short(base)}/products.json redirected elsewhere"
                continue
            collection = shopify_collection(base)
            evidence.append(f"{_short(base)}/products.json?limit=1 → 1 product")
            reason = (
                f"the {collection!r} collection is published as JSON — complete fields, "
                "up to 250 products per request"
                if collection
                else "the store publishes its catalogue as JSON — complete fields, "
                "~250 products per request"
            )
            return (
                ApiOffer(
                    platform="shopify",
                    label="Shopify",
                    endpoint=f"{base}/products.json",
                    reason=reason,
                    evidence=evidence,
                    currency=currency,
                ),
                "",
            )
        return None, why
    if match.platform in ("woocommerce", "wordpress"):
        url = probe_urls(match, origin)[0]
        p = probes.get(url)
        if p is None:
            return None, "the REST endpoint was not reachable"
        doc = p.json
        if not isinstance(doc, list):
            return None, f"{url} did not return a list (HTTP {p.status})"
        kind = "products" if match.platform == "woocommerce" else "posts"
        return (
            ApiOffer(
                platform=match.platform,
                label="WooCommerce" if match.platform == "woocommerce" else "WordPress",
                endpoint=url.split("?")[0],
                reason=f"the site exposes its {kind} through the public REST API",
                evidence=[f"{url} → {len(doc)} {kind[:-1]}"],
            ),
            "",
        )
    return None, "this platform does not publish a public catalogue API"


# ------------------------------------------------------------------------------ recipe patches
def _f(name: str, path: str, **kw: Any) -> dict[str, Any]:
    extract: dict[str, Any] = {"json_path": path}
    for k in ("attr", "regex", "template", "all"):
        if k in kw:
            extract[k] = kw.pop(k)
    return {"name": name, "extract": extract, **kw}


def shopify_fields(store_root: str, granularity: str) -> list[dict[str, Any]]:
    """Field set for /products.json. Prices are string decimals ('91.00') — never the integer
    minor units the Ajax `.js` endpoints use — and the payload carries no product URL, so the
    canonical link is built from the handle against the storefront root (a collection endpoint
    still links to /products/<handle>, not /collections/x/products/<handle>)."""
    if granularity == "variant":
        return [
            _f("title", "$._parent.title", required=True),
            _f("variant", "$.title"),
            _f("price", "$.price", type="price"),
            _f("compare_at_price", "$.compare_at_price", type="price", sparse=True),
            _f("sku", "$.sku"),
            _f("available", "$.available", type="bool"),
            _f("vendor", "$._parent.vendor"),
            _f("product_type", "$._parent.product_type"),
            _f("tags", "$._parent.tags[*]", type="list", all=True),
            _f("image", "$._parent.images[0].src", type="image"),
            _f("url", "$._parent.handle", type="url", template=f"{store_root}/products/{{value}}"),
            _f("published_at", "$._parent.published_at", type="date"),
        ]
    return [
        _f("title", "$.title", required=True),
        _f("price", "$.variants[0].price", type="price"),
        _f("compare_at_price", "$.variants[0].compare_at_price", type="price", sparse=True),
        _f("sku", "$.variants[0].sku"),
        _f("available", "$.variants[0].available", type="bool"),
        _f("vendor", "$.vendor"),
        _f("product_type", "$.product_type"),
        _f("tags", "$.tags[*]", type="list", all=True),
        _f("image", "$.images[0].src", type="image"),
        _f("url", "$.handle", type="url", template=f"{store_root}/products/{{value}}"),
        _f("published_at", "$.published_at", type="date"),
    ]


def _wp_base(origin: str, extras: dict[str, str]) -> str:
    base = (extras.get("wp_json_base") or f"{origin}/wp-json/").rstrip("/") + "/"
    if not base.startswith("http"):
        base = origin_of(origin).rstrip("/") + "/" + base.lstrip("/")
    return base


def offer_patch(offer: ApiOffer, origin: str, extras: dict[str, str]) -> dict[str, Any]:
    """The recipe fragment that switches a recipe to this API."""
    if offer.platform == "shopify":
        base = origin.rstrip("/")
        collection = shopify_collection(base)
        note = (
            f"Shopify /collections/{collection}/products.json"
            if collection
            else "Shopify /products.json"
        ) + " — up to 250 products per request, paged until a page comes back empty"
        if offer.currency:
            note += f"; prices in {offer.currency}"
        return {
            "page_type": "list",
            "list": {"container": "json:body.products[*]"},
            "api": {
                "url_template": f"{base}/products.json?limit={{limit}}&page={{page}}",
                "paging": {"kind": "page", "start": 1, "step": 1, "page_size": 250},
                "explode": "variants" if offer.granularity == "variant" else None,
                "platform": "shopify",
                "note": note,
            },
            "fields": shopify_fields(shopify_store_root(base), offer.granularity),
        }
    if offer.platform == "woocommerce":
        base = _wp_base(origin, extras)
        return {
            "page_type": "list",
            "list": {"container": "json:body"},
            "api": {
                "url_template": f"{base}wc/store/v1/products?per_page=100&page={{page}}",
                "paging": {"kind": "page", "start": 1, "step": 1, "page_size": 100},
                "platform": "woocommerce",
                "note": "WooCommerce Store API — prices are in minor units (see currency_minor_unit)",
            },
            "fields": [
                _f("title", "$.name", required=True),
                _f("price", "$.prices.price"),
                _f("currency", "$.prices.currency_code"),
                _f("sku", "$.sku"),
                _f("in_stock", "$.is_in_stock", type="bool"),
                _f("image", "$.images[0].src", type="image"),
                _f("url", "$.permalink", type="url"),
            ],
        }
    if offer.platform == "wordpress":
        base = _wp_base(origin, extras)
        return {
            "page_type": "list",
            "list": {"container": "json:body"},
            "api": {
                "url_template": f"{base}wp/v2/posts?per_page=100&page={{page}}",
                "paging": {"kind": "page", "start": 1, "step": 1, "page_size": 100},
                "platform": "wordpress",
                "note": "WordPress REST API — rendered fields contain HTML",
            },
            "fields": [
                _f("title", "$.title.rendered", required=True),
                _f("url", "$.link", type="url"),
                _f("published_at", "$.date", type="date"),
                _f("excerpt", "$.excerpt.rendered"),
                _f("slug", "$.slug"),
            ],
        }
    raise ValueError(f"no patch for {offer.platform!r}")


# What a page budget written for HTML means once one request carries 250 rows. Only defaults are
# raised: a budget the user chose is a decision, not an accident.
HTML_DEFAULTS = {"max_pages": 20, "max_items": 1000}
API_DEFAULTS = {"max_pages": 100, "max_items": 25_000}


def _api_limits(limits: dict[str, Any], api: dict[str, Any]) -> dict[str, Any]:
    """Walking a catalogue "until a page comes back empty" needs a budget in API pages.

    20 pages x 250 products is 5,000, but `max_items` would still stop the walk at 1,000 — a cap
    written for pages of ten. Left alone, switching to the API would quietly truncate the very
    catalogue it was meant to read whole.
    """
    out = dict(limits)
    size = int((api.get("paging") or {}).get("page_size") or 0)
    if size <= 50:  # small pages: the HTML budget still means what it said
        return out
    for key, default in HTML_DEFAULTS.items():
        if out.get(key, default) == default:
            out[key] = API_DEFAULTS[key]
    return out


def apply_offer(recipe: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge an API patch into a recipe, keeping the HTML selectors as *alternates*.

    `select_containers` and `_run_field` both try the primary and then the alternates, so the
    merged recipe reads the API when it answers and parses the page when it does not — one
    recipe, two shapes, and the fallback needs no extra configuration.
    """
    out = json.loads(json.dumps(recipe))  # deep copy, JSON-safe by construction
    old_list = out.get("list") or {}
    old_container = (old_list.get("container") or "").strip()
    new_list = dict(patch["list"])
    alternates = list(old_list.get("alternates") or [])
    if (
        old_container
        and not old_container.startswith(JSON_PREFIX)
        and old_container not in alternates
    ):
        alternates.insert(0, old_container)  # HTML fallback
    if alternates:
        new_list["alternates"] = alternates[:3]
    out["list"] = new_list
    out["page_type"] = patch.get("page_type", "list")
    out["api"] = {k: v for k, v in patch["api"].items() if v is not None}

    by_name = {f.get("name"): f for f in out.get("fields") or []}
    merged: list[dict[str, Any]] = []
    for nf in patch["fields"]:
        old = by_name.pop(nf["name"], None)
        if old and old.get("extract") and old["extract"].get("json_path") is None:
            nf = {**nf, "alternates": [old["extract"], *(old.get("alternates") or [])][:3]}
            for keep in ("type", "required", "description", "enum"):
                if keep in old and keep not in nf:
                    nf[keep] = old[keep]
        merged.append(nf)
    merged += list(by_name.values())  # fields the user added stay, and still work on fallback
    out["fields"] = merged
    if not any(f.get("scope") == "detail" for f in merged):
        # the list payload is complete; a per-row page fetch would cost requests for nothing
        out.setdefault("detail", {})["enabled"] = False
    out["limits"] = _api_limits(out.get("limits") or {}, patch["api"])
    # dedupe on the item URL now that every row has its own
    if any(f.get("type") == "url" for f in merged):
        out["dedupe_key"] = ["_url"]
    return out
