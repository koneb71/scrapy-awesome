"""Recipe → items, for one page. Pure functions; no Scrapy dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from parsel import Selector

from scrapy_awesome.extract import jsonpath
from scrapy_awesome.extract.coerce import coerce
from scrapy_awesome.extract.selectors import absolutize, extract_raw, select_nodes
from scrapy_awesome.recipe.models import JSON_PREFIX, Extractor, Field, Recipe, selector_kind


@dataclass
class ExtractedItem:
    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, list[Any]] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)  # field → primary | alt:<i> | missing
    detail_url: str | None = None
    index: int = 0

    def missing_fields(self) -> list[str]:
        return [k for k, p in self.provenance.items() if p == "missing"]


def html_selector(text: str, url: str | None = None) -> Selector:
    """A Selector for markup. A JSON body is not markup: parsel would build a `type="json"`
    selector (whose .css()/.xpath() raise), so an API response yields an empty document instead —
    CSS/XPath extractors simply find nothing, which is the honest answer.
    """
    body = text or ""
    if body.lstrip()[:1] in "{[":
        body = ""
    return Selector(text=body, base_url=url, type="html")


def api_blobs(doc: Any) -> dict[str, Any]:
    """JSON blobs for an API response body.

    The body is exposed as `body` (so containers read `json:body.products[*]`) and, for objects,
    its top-level keys are merged in as well, so page/detail fields can use the plain JSONPath
    root form (`$.product.title`). A literal top-level key named "body" wins over the alias.
    """
    blobs: dict[str, Any] = {"body": doc}
    if isinstance(doc, dict):
        blobs.update(doc)
    return blobs


def _json_container(blobs: dict[str, Any] | None, spec: str) -> list[Any]:
    """`json:<blob>.<path>[*]` → list of JSON items."""
    if not blobs:
        return []
    body = spec[len(JSON_PREFIX) :].strip()
    # blob name is up to the first '.' or '[' — blob names may contain '+' etc.
    cut = len(body)
    for ch in ".[":
        i = body.find(ch)
        if i != -1:
            cut = min(cut, i)
    blob_name, rest = body[:cut], body[cut:].lstrip(".")
    if blob_name not in blobs:
        return []
    hits = jsonpath.resolve(blobs[blob_name], rest) if rest else [blobs[blob_name]]
    # a single list hit means "the array" → its elements
    if len(hits) == 1 and isinstance(hits[0], list):
        return list(hits[0])
    return hits


def select_containers(
    sel: Selector, container: str, alternates: list[str], json_blobs: dict[str, Any] | None
) -> tuple[list[Any], str]:
    """Return (nodes, which) — which = 'primary' or 'alt:<i>'."""
    candidates = [container, *alternates]
    for i, c in enumerate(candidates):
        if c.startswith(JSON_PREFIX):
            nodes = _json_container(json_blobs, c)
        else:
            nodes = list(select_nodes(sel, c))
        if nodes:
            return nodes, "primary" if i == 0 else f"alt:{i}"
    return [], "missing"


def _run_field(
    node: Any,
    f: Field,
    *,
    base_url: str | None,
    page_sel: Selector | None,
    json_blobs: dict[str, Any] | None,
) -> tuple[list[Any], Any, str]:
    """Try primary then alternates. Returns (raw_values, value, provenance)."""
    extractors: list[Extractor] = [f.extract, *f.alternates]
    for i, ext in enumerate(extractors):
        ctx = node
        # json_path on an html node: look in the page's JSON blobs instead
        if ext.source == "json_path" and isinstance(node, Selector):
            ctx = json_blobs or {}
        raw = extract_raw(ctx, ext)
        if ext.template:  # build a value the payload only implies, e.g. a URL from a handle
            raw = [ext.template.replace("{value}", str(v)) for v in raw if v not in (None, "")]
        if f.type in ("url", "image"):
            raw = absolutize(raw, base_url)
        value = coerce(raw, f)
        if value not in (None, [], ""):
            return raw, value, "primary" if i == 0 else f"alt:{i}"
    return [], f.default, "missing" if f.extract.source != "llm" else "llm_pending"


def _resolve_detail_url(node: Any, link: Extractor | None, base_url: str) -> str | None:
    if link is None:
        return None
    ext = link
    if isinstance(node, Selector) and ext.source in ("css", "xpath") and not ext.attr:
        # bare element selector for a link → take href
        bare_css = ext.css and "::attr" not in ext.css and "::text" not in ext.css
        bare_xpath = ext.xpath and not ext.xpath.rstrip().endswith(("href", "text()"))
        if bare_css or bare_xpath:
            ext = ext.model_copy(update={"attr": "href"})
    raw = extract_raw(node, ext)
    if ext.template:
        raw = [ext.template.replace("{value}", str(v)) for v in raw if v not in (None, "")]
    raw = absolutize(raw, base_url)
    for v in raw:
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return None


def _row_url(recipe: Recipe, item: ExtractedItem) -> str | None:
    """First url-typed field value of a row, if it is an absolute http(s) URL."""
    for f in recipe.fields:
        if f.type != "url":
            continue
        v = item.values.get(f.name)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    return None


def item_url(recipe: Recipe, item: ExtractedItem, page_url: str) -> str:
    """The identity of one row — what `_url` holds and what the default dedupe key uses.

    An API row's identity is the item's own page, not the endpoint it arrived in: otherwise every
    row of a `/products.json` page shares one `_url` and dedupe collapses them into one.
    """
    own = item.detail_url
    if own is None and recipe.api is not None:
        own = _row_url(recipe, item)
    return own or f"{page_url}#item-{item.index}"


def extract_list_items(
    recipe: Recipe,
    html: str,
    url: str,
    *,
    json_blobs: dict[str, Any] | None = None,
    limit: int | None = None,
) -> tuple[list[ExtractedItem], str]:
    """Extract all list-scope items from one list page. Returns (items, container_provenance)."""
    sel = html_selector(html, url)
    if recipe.page_type == "single" or recipe.list_ is None:
        item = extract_page_fields(recipe, html, url, scope="page", json_blobs=json_blobs)
        return [item], "page"

    nodes, which = select_containers(
        sel, recipe.list_.container, recipe.list_.alternates, json_blobs
    )
    if recipe.api and recipe.api.explode and nodes:
        # one row per child (Shopify variants): the child becomes the node and the item stays
        # reachable as `_parent`, so product-level fields keep working (`$._parent.title`).
        # `variants` and `variants[*]` mean the same thing here.
        exploded: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for match in jsonpath.resolve(node, recipe.api.explode):
                children = match if isinstance(match, list) else [match]
                exploded += [{**c, "_parent": node} for c in children if isinstance(c, dict)]
        nodes = exploded
    page_fields = [f for f in recipe.fields if f.scope == "page"]
    page_values: dict[str, Any] = {}
    page_raw: dict[str, list[Any]] = {}
    page_prov: dict[str, str] = {}
    for f in page_fields:
        raw, value, prov = _run_field(sel, f, base_url=url, page_sel=sel, json_blobs=json_blobs)
        page_values[f.name], page_raw[f.name], page_prov[f.name] = value, raw, prov

    items: list[ExtractedItem] = []
    for idx, node in enumerate(nodes):
        if limit is not None and idx >= limit:
            break
        item = ExtractedItem(index=idx)
        item.values.update(page_values)
        item.raw.update(page_raw)
        item.provenance.update(page_prov)
        for f in recipe.list_fields:
            if f.scope != "list":
                continue
            raw, value, prov = _run_field(
                node, f, base_url=url, page_sel=sel, json_blobs=json_blobs
            )
            item.values[f.name], item.raw[f.name], item.provenance[f.name] = value, raw, prov
        if recipe.detail.enabled:
            item.detail_url = _resolve_detail_url(node, recipe.detail.link, url)
            if item.detail_url is None and recipe.api is not None:
                # API rows carry the page URL in a url-typed field; the HTML link
                # selector (kept for the fallback path) cannot match a JSON node.
                item.detail_url = _row_url(recipe, item)
        items.append(item)
    return items, which


def extract_page_fields(
    recipe: Recipe,
    html: str,
    url: str,
    *,
    scope: str = "detail",
    json_blobs: dict[str, Any] | None = None,
) -> ExtractedItem:
    """Extract fields of the given scope ('detail' or 'page') from a full page."""
    sel = html_selector(html, url)
    item = ExtractedItem()
    for f in recipe.fields:
        if f.scope != scope:
            continue
        raw, value, prov = _run_field(sel, f, base_url=url, page_sel=sel, json_blobs=json_blobs)
        item.values[f.name], item.raw[f.name], item.provenance[f.name] = value, raw, prov
    return item


def next_page_url(recipe: Recipe, html: str, url: str) -> str | None:
    """For pagination kind next_link: resolve the next page URL from this page (or None)."""
    pg = recipe.pagination
    if pg.kind != "next_link" or not pg.selector:
        return None
    sel = html_selector(html, url)
    ext = (
        Extractor(xpath=pg.selector)
        if selector_kind(pg.selector) == "xpath"
        else Extractor(css=pg.selector)
    )
    return _resolve_detail_url(sel, ext, url)


def count_matches(html: str, selector: str, url: str | None = None) -> int:
    sel = html_selector(html, url)
    try:
        return len(select_nodes(sel, selector))
    except Exception:
        return -1
