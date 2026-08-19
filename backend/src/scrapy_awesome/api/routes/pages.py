"""Pages: snapshot jobs (design-time fetches through the real engine stack), sample metadata,
raw HTML, the same-origin *rendered DOM* for the element picker, heuristic analysis, and ad-hoc
selector testing."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from lxml import html as lxml_html
from pydantic import BaseModel, Field, ValidationError

from scrapy_awesome.api.platform_probe import detect_for_sample, switch_to_api
from scrapy_awesome.extract.engine import html_selector
from scrapy_awesome.extract.selectors import raw_values, select_nodes
from scrapy_awesome.recipe.models import Extractor, Recipe, selector_kind
from scrapy_awesome.snapshot.analyze import analyze_html
from scrapy_awesome.snapshot.fold import fold_html
from scrapy_awesome.snapshot.markdown import to_markdown
from scrapy_awesome.snapshot.search import search_text
from scrapy_awesome.store import SampleRow, Store, iso

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pages"])


class SnapshotIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
    recipe: dict[str, Any] | None = None
    recipe_id: str | None = None
    kind: str = "list"
    tier: str | None = None
    headed: bool = False
    detect_platform: bool = True  # auto-check for Shopify & co and confirm their JSON API


class SelectorIn(BaseModel):
    selector: str
    attr: str | None = None
    regex: str | None = None
    container: str | None = None  # evaluate relative to each container item


def sample_out(row: SampleRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "recipe_id": row.recipe_id,
        "url": row.url,
        "final_url": row.final_url,
        "status": row.status,
        "tier": row.tier,
        "kind": row.kind,
        "bytes": row.bytes,
        "title": row.title,
        "blobs": list((row.blobs or {}).keys()),
        "verdict": row.verdict,
        "analysis": row.analysis,
        "created_at": iso(row.created_at),
    }


def _recipe_from(body: SnapshotIn, store: Store) -> Recipe | None:
    if body.recipe:
        return Recipe.model_validate(body.recipe)
    if body.recipe_id:
        return store.get_recipe(body.recipe_id)
    return None


@router.post("/pages/snapshot")
async def snapshot(request: Request, body: SnapshotIn) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    manager = request.app.state.manager
    recipe = _recipe_from(body, store)
    rows = await manager.snapshot(
        body.urls, recipe=recipe, kind=body.kind, tier=body.tier, headed=body.headed
    )
    if not rows:
        raise HTTPException(502, "snapshot produced no pages (see snapshot-jobs/*/worker.log)")
    out = []
    for i, row in enumerate(rows):
        html = store.sample_html(row)
        analysis = analyze_html(html, row.final_url or row.url, blobs=row.blobs or None).to_dict()
        # "Is this a Shopify store?" — free on the page we already have, then one confirming
        # fetch. Only for the first list page: detail pages share the platform.
        if body.detect_platform and i == 0 and row.kind in ("list", "page"):
            try:
                analysis["platform"] = await detect_for_sample(
                    store=store, manager=manager, row=row, recipe=recipe
                )
            except Exception:  # detection must never break a snapshot
                logger.exception("platform detection failed for %s", row.url)
        row = store.update_sample(row.id, analysis=analysis) or row
        out.append(sample_out(row))
    return out


@router.get("/pages")
def list_pages(
    request: Request, recipe_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    store: Store = request.app.state.store
    return [sample_out(r) for r in store.list_samples(recipe_id=recipe_id, limit=limit)]


def _get(request: Request, sample_id: str) -> tuple[Store, SampleRow]:
    store: Store = request.app.state.store
    row = store.get_sample(sample_id)
    if not row:
        raise HTTPException(404, "page not found")
    return store, row


@router.get("/pages/{sample_id}")
def get_page(request: Request, sample_id: str) -> dict[str, Any]:
    _, row = _get(request, sample_id)
    return sample_out(row)


@router.delete("/pages/{sample_id}")
def delete_page(request: Request, sample_id: str) -> dict[str, Any]:
    store, _ = _get(request, sample_id)
    store.delete_sample(sample_id)
    return {"id": sample_id, "deleted": True}


@router.get("/pages/{sample_id}/html", response_class=PlainTextResponse)
def page_html(request: Request, sample_id: str) -> str:
    store, row = _get(request, sample_id)
    return store.sample_html(row)


@router.get("/pages/{sample_id}/blobs")
def page_blobs(request: Request, sample_id: str) -> dict[str, Any]:
    _, row = _get(request, sample_id)
    return row.blobs or {}


@router.get("/pages/{sample_id}/outline", response_class=PlainTextResponse)
def page_outline(
    request: Request,
    sample_id: str,
    max_chars: int = 12_000,
    keep_siblings: int = 2,
    text_limit: int = 80,
) -> str:
    """Folded DOM outline: noise removed, long text truncated, similar siblings collapsed."""
    store, row = _get(request, sample_id)
    return fold_html(
        store.sample_html(row),
        max_chars=max(1000, min(max_chars, 200_000)),
        keep_siblings=max(1, min(keep_siblings, 10)),
        text_limit=max(20, min(text_limit, 400)),
    )


@router.get("/pages/{sample_id}/markdown", response_class=PlainTextResponse)
def page_markdown(
    request: Request, sample_id: str, fit: bool = True, max_chars: int = 20_000
) -> str:
    """Main-content markdown (fit=1, trafilatura) or the whole body (fit=0)."""
    store, row = _get(request, sample_id)
    return to_markdown(
        store.sample_html(row),
        row.final_url or row.url,
        fit=fit,
        max_chars=max(1000, min(max_chars, 400_000)),
    )


class SearchIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    container: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


@router.post("/pages/{sample_id}/search")
def page_search(request: Request, sample_id: str, body: SearchIn) -> dict[str, Any]:
    """Where does this text appear? → elements + CSS paths (+ container-relative selectors)."""
    store, row = _get(request, sample_id)
    container = body.container
    if container is None and row.analysis:
        cands = (row.analysis or {}).get("containers") or []
        if cands:
            container = cands[0].get("selector")
    matches = search_text(store.sample_html(row), body.text, container=container, limit=body.limit)
    return {"query": body.text, "container": container, "matches": matches}


_ON_ATTR = re.compile(r"^on[a-z]+$", re.I)


def render_for_picker(html: str, base_url: str) -> str:
    """Rendered DOM, made inert: scripts/iframes/CSP removed, event handlers stripped, <base> injected."""
    root = lxml_html.fromstring(html or "<html><body></body></html>")
    for bad in root.xpath("//script|//noscript|//iframe|//frame|//object|//embed|//applet"):
        bad.getparent().remove(bad)
    for meta in root.xpath("//meta[@http-equiv]"):
        if (meta.get("http-equiv") or "").lower() in ("content-security-policy", "refresh"):
            meta.getparent().remove(meta)
    for link in root.xpath("//link[@rel]"):
        if (link.get("rel") or "").lower() in ("preload", "prefetch", "modulepreload"):
            link.getparent().remove(link)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if _ON_ATTR.match(attr) or (
                attr in ("href", "src")
                and (el.get(attr) or "").strip().lower().startswith("javascript:")
            ):
                del el.attrib[attr]
        if el.tag == "a":
            el.set("target", "_self")
    head = root.find("head")
    if head is None:
        head = lxml_html.Element("head")
        root.insert(0, head)
    for old in head.findall("base"):
        head.remove(old)
    base = lxml_html.Element("base", href=base_url)
    head.insert(0, base)
    style = lxml_html.Element("style")
    style.text = (
        "*{cursor:crosshair !important} "
        ".sa-hover{outline:2px solid #2563eb !important; outline-offset:-2px; background:rgba(37,99,235,.08) !important} "
        ".sa-picked{outline:2px solid #16a34a !important; outline-offset:-2px} "
        ".sa-sibling{outline:1px dashed #16a34a !important; outline-offset:-1px}"
    )
    head.append(style)
    return lxml_html.tostring(root, encoding="unicode", doctype="<!DOCTYPE html>")


@router.get("/pages/{sample_id}/render", response_class=HTMLResponse)
def page_render(request: Request, sample_id: str) -> HTMLResponse:
    store, row = _get(request, sample_id)
    html = render_for_picker(store.sample_html(row), row.final_url or row.url)
    return HTMLResponse(
        html, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    )


class SwitchIn(BaseModel):
    recipe: dict[str, Any]
    granularity: Literal["product", "variant"] = "product"


@router.post("/pages/{sample_id}/detect")
async def detect_platform(request: Request, sample_id: str, probe: bool = True) -> dict[str, Any]:
    """Re-run platform detection for a cached page (probing unless `probe=0`)."""
    store, row = _get(request, sample_id)
    block = await detect_for_sample(
        store=store, manager=request.app.state.manager, row=row, probe=probe
    )
    analysis = dict(row.analysis or {})
    analysis["platform"] = block
    store.update_sample(sample_id, analysis=analysis)
    return block


@router.post("/pages/{sample_id}/use-api")
def use_api(request: Request, sample_id: str, body: SwitchIn) -> dict[str, Any]:
    """Rewrite a recipe to read the confirmed JSON API, keeping its HTML selectors as fallbacks."""
    _store, row = _get(request, sample_id)
    block = (row.analysis or {}).get("platform") or {}
    if not block.get("api"):
        raise HTTPException(409, block.get("reason") or "no confirmed API for this page")
    try:
        merged = switch_to_api(body.recipe, block, granularity=body.granularity)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        recipe = Recipe.model_validate(merged)
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()
        ]
        raise HTTPException(422, {"errors": errors}) from exc
    return {
        "recipe": recipe.to_dict(),
        "platform": block.get("platform"),
        "endpoint": block["api"]["endpoint"],
        "granularity": body.granularity,
        "ready": recipe.ready,
    }


@router.post("/pages/{sample_id}/analyze")
def page_analyze(request: Request, sample_id: str) -> dict[str, Any]:
    store, row = _get(request, sample_id)
    analysis = analyze_html(
        store.sample_html(row), row.final_url or row.url, blobs=row.blobs or None
    ).to_dict()
    store.update_sample(sample_id, analysis=analysis)
    return analysis


@router.post("/pages/{sample_id}/selector")
def page_selector(request: Request, sample_id: str, body: SelectorIn) -> dict[str, Any]:
    """Test a selector against a cached page: match count, first values, and outer-HTML snippets."""
    store, row = _get(request, sample_id)
    html = store.sample_html(row)
    sel = html_selector(html, row.final_url or row.url)
    try:
        ext = (
            Extractor(xpath=body.selector, attr=body.attr, regex=body.regex)
            if selector_kind(body.selector) == "xpath"
            else Extractor(css=body.selector, attr=body.attr, regex=body.regex)
        )
    except Exception as exc:
        raise HTTPException(422, f"bad selector: {exc}") from exc
    try:
        if body.container:
            items = list(select_nodes(sel, body.container))
            per_item = [raw_values(it, ext) for it in items]
            filled = sum(1 for v in per_item if v)
            values = [v[0] for v in per_item if v][:8]
            return {
                "container_matches": len(items),
                "filled": filled,
                "fill_rate": round(filled / len(items), 3) if items else 0.0,
                "values": values,
            }
        nodes = select_nodes(sel, body.selector)
        values = raw_values(sel, ext)[:8]
        snippets = [n.get()[:240] for n in nodes[:5]] if hasattr(nodes, "__iter__") else []
        return {"matches": len(nodes), "values": values, "snippets": snippets}
    except Exception as exc:
        raise HTTPException(422, f"selector error: {exc}") from exc
