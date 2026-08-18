"""Find where a piece of text lives in a cached page and hand back usable selectors.

Agents work best "by example": they see `£11.50` on the page and ask *where is that?* We answer
with the innermost element(s) whose own text contains the query, a short unique CSS path, and —
when a list container is known — the selector *relative to the container item* plus how many
items it fills. That is exactly what a recipe field needs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from lxml import html as lxml_html
from lxml.etree import _Element

from scrapy_awesome.snapshot.analyze import css_for

_WS = re.compile(r"\s+")
_SKIP = frozenset(["script", "style", "noscript", "svg", "template", "head"])


@dataclass
class TextMatch:
    css: str
    tag: str
    text: str
    attr: str | None = None  # when the match is in an attribute value (href/src/alt/title/content)
    in_container: bool = False
    relative_css: str | None = None
    container_fill: int | None = None
    container_items: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v is not False}


def _own_text(el: _Element) -> str:
    parts = [el.text or ""] + [c.tail or "" for c in el]
    return _WS.sub(" ", " ".join(parts)).strip()


def _full_text(el: _Element, limit: int = 160) -> str:
    return _WS.sub(" ", " ".join(el.itertext())).strip()[:limit]


def _relative(el: _Element, item: _Element) -> str:
    """Selector of `el` inside `item` (not necessarily unique — fields select the first match)."""
    if el is item:
        return ""
    return css_for(el, item, max_depth=5)


def _item_of(el: _Element, items: list[_Element]) -> _Element | None:
    cur: _Element | None = el
    while cur is not None:
        if cur in items:
            return cur
        cur = cur.getparent()
    return None


def _count_fill(items: list[_Element], rel: str) -> int:
    n = 0
    for it in items:
        try:
            hit = it.cssselect(rel) if rel else [it]
        except Exception:
            return 0
        if hit and _full_text(hit[0]):
            n += 1
    return n


def search_text(
    html: str,
    query: str,
    *,
    container: str | None = None,
    limit: int = 10,
    include_attrs: bool = True,
) -> list[dict[str, Any]]:
    q = _WS.sub(" ", query or "").strip().lower()
    if not q:
        return []
    try:
        root = lxml_html.fromstring(html or "<html></html>")
    except Exception:
        return []
    items: list[_Element] = []
    if container:
        try:
            items = list(root.cssselect(container))
        except Exception:
            items = []
    out: list[TextMatch] = []
    seen: set[str] = set()
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag.lower() in _SKIP:
            continue
        hit_attr: str | None = None
        if q in _own_text(el).lower():
            pass
        elif include_attrs:
            for a in ("href", "src", "alt", "title", "content", "value", "datetime", "aria-label"):
                v = el.get(a)
                if v and q in v.lower():
                    hit_attr = a
                    break
            if hit_attr is None:
                continue
        else:
            continue
        css = css_for(el, root)
        key = f"{css}|{hit_attr}"
        if key in seen:
            continue
        seen.add(key)
        m = TextMatch(
            css=css,
            tag=el.tag.lower(),
            text=(el.get(hit_attr) or "")[:160] if hit_attr else _full_text(el),
            attr=hit_attr,
        )
        item = _item_of(el, items) if items else None
        if item is not None:
            rel = _relative(el, item)
            m.in_container = True
            m.relative_css = rel or "."
            m.container_items = len(items)
            m.container_fill = _count_fill(items, rel)
        out.append(m)
        if len(out) >= limit:
            break
    # innermost first: longer css paths tend to be more specific; keep container hits on top
    out.sort(key=lambda m: (not m.in_container, -(m.container_fill or 0), len(m.css)))
    return [m.to_dict() for m in out]
