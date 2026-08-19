"""Run a single Extractor against an HTML element / page or a JSON node → raw string values."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from parsel import Selector

from scrapy_awesome.extract import jsonpath
from scrapy_awesome.recipe.models import Extractor, selector_kind

_WS = re.compile(r"\s+")
_CSS_PSEUDO = re.compile(r"::(text|attr\([^)]*\))\s*$")


def norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _apply_regex(values: list[str], pattern: str | None) -> list[str]:
    if not pattern:
        return values
    rx = re.compile(pattern, re.S)
    out: list[str] = []
    for v in values:
        m = rx.search(v)
        if m:
            out.append(m.group(1) if m.groups() else m.group(0))
    return out


def select_nodes(sel: Selector, selector: str) -> Any:
    """CSS or XPath → SelectorList (elements or strings)."""
    return sel.xpath(selector) if selector_kind(selector) == "xpath" else sel.css(selector)


def _has_pseudo(selector: str) -> bool:
    return bool(_CSS_PSEUDO.search(selector))


def raw_values(sel: Selector, ext: Extractor) -> list[str]:
    """All raw string values for an Extractor on an HTML node (before type coercion)."""
    if ext.source == "css":
        css = ext.css or ""
        if ext.attr and not _has_pseudo(css):
            css = f"{css}::attr({ext.attr})"
        nodes = sel.css(css)
        if _has_pseudo(css):
            values = [norm(v) for v in nodes.getall()]
        else:
            values = [norm(v) for v in nodes.xpath("string(.)").getall()]
    elif ext.source == "xpath":
        xp = ext.xpath or ""
        if ext.attr and not re.search(r"/@[\w:-]+$|/text\(\)$|^string\(", xp.strip()):
            xp = f"{xp}/@{ext.attr}"
        nodes = sel.xpath(xp)
        got = nodes.getall()
        # element results come back as serialized HTML; use string(.) for those
        if (
            got
            and any(g.lstrip().startswith("<") for g in got)
            and not re.search(r"/@[\w:-]+$|/text\(\)$|^string\(|/@\*$", xp.strip())
        ):
            values = [norm(v) for v in nodes.xpath("string(.)").getall()]
        else:
            values = [norm(v) for v in got]
    else:
        raise ValueError(f"raw_values only handles css/xpath, got {ext.source}")
    values = [v for v in values if v != ""]
    return _apply_regex(values, ext.regex)


def json_values(node: Any, ext: Extractor) -> list[Any]:
    if ext.source != "json_path":
        raise ValueError("json_values needs a json_path extractor")
    hits = jsonpath.resolve(node, ext.json_path or "")
    if ext.regex:
        return _apply_regex([str(h) for h in hits], ext.regex)
    return hits


def extract_raw(context: Any, ext: Extractor) -> list[Any]:
    """Dispatch on context type: parsel Selector (html) or dict/list (json)."""
    if ext.source == "llm":
        return []  # filled by the LLM fallback layer, never here
    if isinstance(context, Selector):
        if ext.source == "json_path":
            return []  # json extractor on an html node → nothing (caller may supply blobs)
        return raw_values(context, ext)
    if ext.source != "json_path":
        # a CSS/XPath extractor against a JSON node (an API item, or an HTML alternate on an
        # API-mode row): no match, so the next alternate gets its turn
        return []
    return json_values(context, ext)


def absolutize(values: list[Any], base_url: str | None) -> list[Any]:
    if not base_url:
        return values
    return [urljoin(base_url, v) if isinstance(v, str) else v for v in values]
