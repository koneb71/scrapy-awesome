"""Find the API a page reads itself from.

Platform detection (`platform.py`) recognises stores it already knows. This is the general case:
open the page in a real browser, watch every JSON response it fetches, and work out which one is
the list on the screen. What comes out is the same `api` recipe block a Shopify offer produces, so
everything downstream — paging, preview, HTML selectors kept as fallbacks — is unchanged.

Nothing here is trusted on its own: the winner is re-fetched directly (robots-checked) before it
is offered, because an endpoint that only answers inside the page's session is not one a crawl can
use.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scrapy_awesome.snapshot.analyze import find_json_lists

# Query parameters that walk a list, and the ones that size a page.
PAGE_PARAMS = ("page", "p", "pagenumber", "page_number", "pageno", "pg", "page[number]")
OFFSET_PARAMS = ("offset", "start", "skip", "from", "_start", "startindex", "page[offset]")
SIZE_PARAMS = (
    "limit",
    "per_page",
    "perpage",
    "pagesize",
    "page_size",
    "size",
    "count",
    "_limit",
    "first",
    "n",
    "rows",
    "page[size]",
)
CURSOR_PARAMS = ("cursor", "after", "next", "page[cursor]", "continuation")
CURSOR_KEYS = ("next_cursor", "nextcursor", "endcursor", "cursor", "next", "next_page_token")

# Endpoints that are never the data on the page.
NOISE = re.compile(
    r"(analytics|collect|beacon|telemetry|track(ing)?|metrics|pixel|gtm|gtag|segment\.|sentry"
    r"|datadog|newrelic|hotjar|clarity|recaptcha|consent|cookie|session/ping|heartbeat|log)",
    re.I,
)
APIISH = re.compile(r"(/api/|/v\d+/|\.json|/graphql|/rest/|/query|/search)", re.I)

MIN_ITEMS = 3


@dataclass
class Capture:
    """One JSON response the page fetched."""

    url: str
    method: str = "GET"
    status: int = 200
    content_type: str = ""
    bytes: int = 0
    body: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Capture:
        return cls(
            url=str(raw.get("url") or ""),
            method=str(raw.get("method") or "GET"),
            status=int(raw.get("status") or 0),
            content_type=str(raw.get("content_type") or ""),
            bytes=int(raw.get("bytes") or 0),
            body=str(raw.get("body") or ""),
        )


@dataclass
class XhrCandidate:
    """A captured endpoint that looks like the list on the page."""

    url: str
    container: str  # json:body.results[*]
    count: int
    keys: list[str]
    score: int
    why: list[str] = field(default_factory=list)
    url_template: str = ""
    paging: dict[str, Any] = field(default_factory=dict)
    sample: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _params(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def _find(params: list[tuple[str, str]], names: tuple[str, ...]) -> tuple[str, str] | None:
    for key, value in params:
        if key.lower() in names:
            return key, value
    return None


def _templated(url: str, page_key: str | None, size_key: str | None) -> str:
    """The URL with its paging parameters turned into `{page}` / `{limit}` placeholders."""
    parts = urlsplit(url)
    out = []
    for key, value in _params(url):
        if page_key and key == page_key:
            out.append((key, "{page}"))
        elif size_key and key == size_key:
            out.append((key, "{limit}"))
        else:
            out.append((key, value))
    query = urlencode(out, safe="{}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def paging_for(url: str, count: int, doc: Any) -> dict[str, Any]:
    """How to walk this endpoint, read off its own query string.

    `page=2` is a page number; `offset=40` is a row number and steps by the page size — both
    render through `{page}`, which is why ApiPaging only needs start and step.
    """
    params = _params(url)
    size = _find(params, SIZE_PARAMS)
    page_size = count
    if size and size[1].isdigit() and int(size[1]) > 0:
        page_size = int(size[1])

    hit = _find(params, PAGE_PARAMS)
    if hit:
        start = int(hit[1]) if hit[1].isdigit() else 1
        return {
            "kind": "page",
            "start": start,
            "step": 1,
            "page_size": page_size,
            "_page_key": hit[0],
            "_size_key": size[0] if size else None,
        }
    hit = _find(params, OFFSET_PARAMS)
    if hit:
        start = int(hit[1]) if hit[1].isdigit() else 0
        return {
            "kind": "page",  # {page} carries the offset; stepping by a page size walks it
            "start": start,
            "step": page_size,
            "page_size": page_size,
            "_page_key": hit[0],
            "_size_key": size[0] if size else None,
        }
    cursor_path = _cursor_path(doc)
    if _find(params, CURSOR_PARAMS) and cursor_path:
        return {
            "kind": "cursor",
            "cursor_path": cursor_path,
            "page_size": page_size,
            "_page_key": _find(params, CURSOR_PARAMS)[0],  # type: ignore[index]
            "_size_key": size[0] if size else None,
        }
    return {"kind": "none", "page_size": page_size, "_page_key": None, "_size_key": None}


def _cursor_path(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return None
    for key, value in doc.items():
        if key.lower() in CURSOR_KEYS and isinstance(value, str | int):
            return f"$.{key}"
    for outer in ("meta", "pagination", "page_info", "pageInfo", "links"):
        inner = doc.get(outer)
        if isinstance(inner, dict):
            for key, value in inner.items():
                if key.lower() in CURSOR_KEYS and isinstance(value, str | int):
                    return f"$.{outer}.{key}"
    return None


def _values(node: Any, out: list[str], budget: int = 60) -> None:
    if len(out) >= budget:
        return
    if isinstance(node, dict):
        for v in node.values():
            _values(v, out, budget)
    elif isinstance(node, list):
        for v in node[:5]:
            _values(v, out, budget)
    elif isinstance(node, str) and 3 < len(node) < 120:
        out.append(node)


def _overlap(doc: Any, container: str, page_text: str) -> float:
    """How much of this array is text you can see on the page — the strongest signal that this
    response is what the page rendered, rather than config, tracking or a recommendation rail."""
    if not page_text:
        return 0.0
    from scrapy_awesome.extract import jsonpath

    path = container.removeprefix("json:body").lstrip(".").removesuffix("[*]")
    nodes = jsonpath.resolve(doc, path) if path else [doc]
    items = nodes[0] if nodes and isinstance(nodes[0], list) else []
    strings: list[str] = []
    for item in items[:8]:
        _values(item, strings)
    if not strings:
        return 0.0
    hay = page_text.lower()
    hits = sum(1 for s in strings if s.lower() in hay)
    return hits / len(strings)


def candidates(
    captures: list[Capture] | list[dict[str, Any]], *, page_text: str = "", top: int = 5
) -> list[XhrCandidate]:
    """Rank the captured responses by how much they look like the list on the page."""
    out: list[XhrCandidate] = []
    for raw in captures:
        cap = raw if isinstance(raw, Capture) else Capture.from_dict(raw)
        if cap.method.upper() not in ("GET", "POST"):
            continue
        try:
            doc = json.loads(cap.body)
        except (ValueError, TypeError):
            continue
        for found in find_json_lists({"body": doc}, min_items=MIN_ITEMS, top=3):
            score, why = 0, []
            count, container = int(found["count"]), str(found["container"])

            depth = container.count(".")
            if depth <= 2:
                score += 3
                why.append("the list is what the response is about")
            share = _overlap(doc, container, page_text)
            if share >= 0.4:
                score += 5
                why.append(f"{share:.0%} of its values are text on the page")
            elif share > 0:
                score += 2
                why.append(f"{share:.0%} of its values are text on the page")
            if APIISH.search(cap.url):
                score += 2
                why.append("the URL looks like an API")
            paging = paging_for(cap.url, count, doc)
            if paging["kind"] != "none":
                score += 2
                why.append(f"it pages by {paging['_page_key']}")
            score += min(count, 30) // 10
            if NOISE.search(cap.url):
                score -= 6
                why.append("looks like analytics, not content")
            if cap.method.upper() == "POST":
                score -= 1  # replayable, but more likely a search or a GraphQL query

            if score <= 2:
                continue
            out.append(
                XhrCandidate(
                    url=cap.url,
                    container=container,
                    count=count,
                    keys=list(found["keys"]),
                    score=score,
                    why=why,
                    url_template=_templated(cap.url, paging["_page_key"], paging["_size_key"]),
                    paging={k: v for k, v in paging.items() if not k.startswith("_")},
                    sample=_first_item(doc, container),
                )
            )
    out.sort(key=lambda c: (-c.score, -c.count))
    deduped: list[XhrCandidate] = []
    seen: set[str] = set()
    for c in out:
        key = f"{c.url_template}|{c.container}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped[:top]


def _first_item(doc: Any, container: str) -> dict[str, Any]:
    from scrapy_awesome.extract import jsonpath

    path = container.removeprefix("json:body").lstrip(".").removesuffix("[*]")
    nodes = jsonpath.resolve(doc, path) if path else [doc]
    items = nodes[0] if nodes and isinstance(nodes[0], list) else []
    return items[0] if items and isinstance(items[0], dict) else {}


# ------------------------------------------------------------------------------ recipe patch
_IDENT = re.compile(r"[^a-z0-9]+")
_TYPE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(price|amount|cost|salary|fee|total)", re.I), "price"),
    (re.compile(r"(image|img|thumb|photo|picture|avatar|cover)", re.I), "image"),
    (re.compile(r"(url|link|href|permalink|slug|path)", re.I), "url"),
    (re.compile(r"(date|time|created|updated|published|_at$)", re.I), "date"),
)


def field_name(key: str) -> str:
    """A recipe-legal snake_case name for a JSON key (`productName` → `product_name`)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    name = _IDENT.sub("_", spaced.lower()).strip("_")
    if not name or not name[0].isalpha():
        name = f"f_{name}" if name else "field"
    return name[:64]


def _type_for(key: str, value: Any) -> str:
    for pattern, kind in _TYPE_HINTS:
        if pattern.search(key):
            if kind == "url" and isinstance(value, str) and not value.startswith(("http", "/")):
                continue  # a "slug" that is not a link is just text
            return kind
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "list"
    return "text"


def fields_for(sample: dict[str, Any], *, max_fields: int = 14) -> list[dict[str, Any]]:
    """A field per useful key of the first item. Nested objects are skipped rather than guessed
    at — the Fields tab is a better place to pick `$.brand.name` than a heuristic is."""
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for key, value in sample.items():
        if isinstance(value, dict) or value is None:
            continue
        if isinstance(value, list) and any(isinstance(x, dict) for x in value):
            continue
        name = field_name(key)
        if name in used:
            continue
        used.add(name)
        kind = _type_for(key, value)
        extract: dict[str, Any] = {"json_path": f"$.{key}"}
        if kind == "list":
            extract["all"] = True
        entry: dict[str, Any] = {"name": name, "type": kind, "extract": extract}
        if name in ("title", "name") and kind == "text":
            entry["required"] = True
        out.append(entry)
        if len(out) >= max_fields:
            break
    return out


def offer_patch(candidate: XhrCandidate) -> dict[str, Any]:
    """The recipe fragment that switches a recipe to this endpoint."""
    paging = dict(candidate.paging)
    note = f"{urlsplit(candidate.url).path} — the JSON this page reads itself from"
    if paging.get("kind") == "page":
        note += "; paged until a page comes back empty"
    return {
        "page_type": "list",
        "list": {"container": candidate.container},
        "api": {
            "url_template": candidate.url_template,
            "paging": paging,
            "platform": "xhr",
            "note": note,
        },
        "fields": fields_for(candidate.sample),
    }


__all__ = [
    "Capture",
    "XhrCandidate",
    "candidates",
    "field_name",
    "fields_for",
    "offer_patch",
    "paging_for",
]
