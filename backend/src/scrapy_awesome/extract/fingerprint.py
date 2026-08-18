"""Element fingerprints for self-healing selectors.

At design time (validation) we remember *what the matched element looked like* for every list
field — tag, stable classes, semantic attributes, its path inside the item, the shape of its
text. At run time, when a field that used to fill suddenly doesn't (a redesign renamed a class,
moved a wrapper…), the worker looks for the most similar element inside the item container,
derives a new relative selector, checks that it fills across the page, and swaps it in for the
rest of the run — reporting the heal so the person can apply it to the recipe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lxml.etree import _Element
from parsel import Selector

from scrapy_awesome.recipe.models import Extractor, Field, Recipe

_UNSTABLE = re.compile(r"\d{2,}|^(css|sc|jsx|_)-|[A-Za-z0-9]{6,}[-_][A-Za-z0-9]{4,}$|__|--")
_CURRENCY = re.compile(r"[$€£¥₹]|\b(usd|eur|gbp)\b", re.I)
_SEM_ATTRS = ("itemprop", "data-testid", "data-test", "name", "role", "rel", "type", "property")
MAX_ELEMENTS_PER_ITEM = 400
PROBE_ITEMS = 6


def _classes(el: _Element) -> list[str]:
    return sorted(
        c for c in (el.get("class") or "").split() if 3 <= len(c) <= 40 and not _UNSTABLE.search(c)
    )[:4]


def _text(el: _Element) -> str:
    return re.sub(r"\s+", " ", " ".join(el.itertext())).strip()


def _text_shape(t: str) -> dict[str, Any]:
    n = len(t)
    return {
        "len": 0 if n == 0 else 1 if n < 12 else 2 if n < 40 else 3 if n < 120 else 4,
        "digits": bool(re.search(r"\d", t)),
        "currency": bool(_CURRENCY.search(t)),
        "words": min(len(t.split()), 20),
    }


def value_shape(t: str) -> str:
    """Coarse shape of a value: digit runs → 0, letter runs → a, whitespace collapsed.
    `£11.50` → `£0.0`, `In stock` → `a a`, `2026-08-18` → `0-0-0`."""
    t = re.sub(r"\s+", " ", t.strip())[:60]
    t = re.sub(r"\d+", "0", t)
    t = re.sub(r"[^\W\d_]+", "a", t)
    return t


def _path(el: _Element, root: _Element, max_depth: int = 6) -> list[str]:
    tags: list[str] = []
    cur: _Element | None = el
    while cur is not None and cur is not root and len(tags) < max_depth:
        if isinstance(cur.tag, str):
            tags.append(cur.tag.lower())
        cur = cur.getparent()
    return list(reversed(tags))


def element_fingerprint(el: _Element, root: _Element) -> dict[str, Any]:
    tag = el.tag.lower() if isinstance(el.tag, str) else "?"
    attrs = {a: el.get(a) for a in _SEM_ATTRS if el.get(a)}
    for k, v in el.attrib.items():
        if k.startswith("data-") and len(attrs) < 6 and k not in attrs:
            attrs[k] = v[:40]
    parent = el.getparent()
    same = (
        [c for c in parent if isinstance(c.tag, str) and c.tag == el.tag]
        if parent is not None
        else [el]
    )
    text = _text(el)
    return {
        "tag": tag,
        "id": el.get("id")
        if el.get("id") and not re.search(r"\d{3,}", el.get("id") or "")
        else None,
        "classes": _classes(el),
        "attrs": attrs,
        "path": _path(el, root),
        "text": _text_shape(text),
        "shape": value_shape(text),
        "leaf": not any(isinstance(c.tag, str) for c in el),
        "nth": same.index(el) if el in same else 0,
        "href": bool(el.get("href")),
        "src": bool(el.get("src")),
    }


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """0..1 — weighted agreement of the fingerprint facets. Structure (tag/classes/attrs/path)
    carries most weight; the *value shape* lets us re-find a price/date/URL-like element after
    a redesign that renamed everything."""
    score = 0.0
    score += 0.12 if a.get("tag") == b.get("tag") else 0.0
    ca, cb = set(a.get("classes") or []), set(b.get("classes") or [])
    if ca or cb:
        score += 0.22 * (len(ca & cb) / len(ca | cb))
    else:
        score += 0.08  # both classless: weak neutral evidence
    aa, ab = a.get("attrs") or {}, b.get("attrs") or {}
    keys = set(aa) | set(ab)
    if keys:
        score += 0.10 * (sum(1 for k in keys if aa.get(k) == ab.get(k)) / len(keys))
    else:
        score += 0.05  # both without semantic attrs: weak agreement
    if a.get("id") and a.get("id") == b.get("id"):
        score += 0.05
    pa, pb = a.get("path") or [], b.get("path") or []
    if pa or pb:
        common = 0
        for x, y in zip(reversed(pa), reversed(pb), strict=False):
            if x != y:
                break
            common += 1
        score += 0.08 * (common / max(len(pa), len(pb)))
    ta, tb = a.get("text") or {}, b.get("text") or {}
    if ta and tb:
        t = 0.0
        t += (
            0.4
            if ta.get("len") == tb.get("len")
            else 0.2
            if abs((ta.get("len") or 0) - (tb.get("len") or 0)) == 1
            else 0.0
        )
        t += 0.3 if ta.get("digits") == tb.get("digits") else 0.0
        t += 0.3 if ta.get("currency") == tb.get("currency") else 0.0
        score += 0.10 * t
    if a.get("shape") and a.get("shape") == b.get("shape"):
        score += 0.28
    if a.get("href") == b.get("href") and a.get("src") == b.get("src"):
        score += 0.03
    if a.get("leaf") is not None and a.get("leaf") == b.get("leaf"):
        score += 0.02
    return round(min(score, 1.0), 4)


# ---------------------------------------------------------------------- design time
def _element_css(ext: Extractor) -> str | None:
    """The element part of a css extractor (drop ::text / ::attr(...))."""
    if not ext.css:
        return None
    return re.sub(r"::(text|attr\([^)]*\))\s*$", "", ext.css).strip() or None


def _matched_element(node: Selector, ext: Extractor) -> _Element | None:
    try:
        if ext.css:
            css = _element_css(ext)
            hits = node.css(css) if css else []
        elif ext.xpath:
            xp = re.sub(r"/(text\(\)|@[\w:-]+)\s*$", "", ext.xpath)
            hits = node.xpath(xp)
        else:
            return None
    except Exception:
        return None
    for h in hits:
        root = getattr(h, "root", None)
        if isinstance(root, _Element):
            return root
    return None


def compute_fingerprints(recipe: Recipe, html: str, url: str) -> dict[str, Any]:
    """Fingerprint every list-scope css/xpath field on this page (first item that fills)."""
    from scrapy_awesome.extract.engine import select_containers

    if recipe.list_ is None:
        return {}
    sel = Selector(text=html, base_url=url)
    nodes, _ = select_containers(sel, recipe.list_.container, recipe.list_.alternates, None)
    out: dict[str, Any] = {}
    for f in recipe.list_fields:
        if f.scope != "list" or f.extract.source not in ("css", "xpath"):
            continue
        for node in nodes[:PROBE_ITEMS]:
            item_root = getattr(node, "root", None)
            el = _matched_element(node, f.extract)
            if el is not None and isinstance(item_root, _Element):
                out[f.name] = element_fingerprint(el, item_root)
                break
    return out


# ---------------------------------------------------------------------- run time
@dataclass
class Candidate:
    selector: str
    score: float
    fill: float
    examples: list[str]
    attr: str | None = None  # attribute the fill was validated with (None → text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "attr": self.attr,
            "score": self.score,
            "fill": self.fill,
            "examples": self.examples[:3],
        }


def _step(el: _Element) -> str:
    tag = el.tag.lower() if isinstance(el.tag, str) else "*"
    cls = _classes(el)
    if cls:
        return tag + "".join(f".{c}" for c in cls[:2])
    for a in _SEM_ATTRS:
        v = el.get(a)
        if v and re.match(r"^[\w.-]+$", v):
            return f'{tag}[{a}="{v}"]'
    return tag


def relative_selector(el: _Element, root: _Element, max_depth: int = 4) -> str:
    """Short css of `el` relative to the item root; unique inside this item when possible."""
    parts: list[str] = []
    cur: _Element | None = el
    depth = 0
    while cur is not None and cur is not root and depth < max_depth:
        parts.insert(0, _step(cur))
        css = " ".join(parts)
        try:
            if len(root.cssselect(css)) == 1:
                return css
        except Exception:
            break
        cur = cur.getparent()
        depth += 1
    return " ".join(parts) or (el.tag.lower() if isinstance(el.tag, str) else "*")


def _value(el: _Element, attr: str | None) -> str:
    return (el.get(attr) or "").strip() if attr else _text(el)


def relocate(
    item_nodes: list[Selector],
    fp: dict[str, Any],
    *,
    attr: str | None = None,
    min_score: float = 0.42,
    min_fill: float = 0.6,
    top: int = 5,
) -> list[Candidate]:
    """Find selectors (relative to the item) whose elements look like `fp` and fill across items."""
    roots = [n.root for n in item_nodes if isinstance(getattr(n, "root", None), _Element)]
    if not roots:
        return []
    scored: dict[str, list[float]] = {}
    for root in roots[:PROBE_ITEMS]:
        count = 0
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            count += 1
            if count > MAX_ELEMENTS_PER_ITEM:
                break
            if el.tag.lower() in ("script", "style", "svg", "path"):
                continue
            s = similarity(fp, element_fingerprint(el, root))
            if s < min_score:
                continue
            css = relative_selector(el, root)
            scored.setdefault(css, []).append(s)
    out: list[Candidate] = []
    for css, scores in scored.items():
        filled = 0
        examples: list[str] = []
        for root in roots:
            try:
                hit = root.cssselect(css)
            except Exception:
                hit = []
            v = _value(hit[0], attr) if hit else ""
            if v:
                filled += 1
                if len(examples) < 3:
                    examples.append(v[:60])
        fill = filled / len(roots)
        if fill >= min_fill:
            out.append(
                Candidate(css, round(sum(scores) / len(scores), 4), round(fill, 3), examples, attr)
            )
    out.sort(key=lambda c: (c.score * 0.6 + c.fill * 0.4, -len(c.selector)), reverse=True)
    return out[:top]


def healed_extractor(old: Extractor, css: str, attr: str | None) -> Extractor:
    """New primary extractor: new element css + validated attr, keeping regex/all semantics."""
    return Extractor(css=css, attr=attr, regex=old.regex, all=old.all)


def heal_field(field: Field, css: str, attr: str | None) -> Field:
    """Copy of `field` using `css`/`attr` as primary and the old primary as first alternate."""
    new = healed_extractor(field.extract, css, attr)
    return field.model_copy(
        update={"extract": new, "alternates": [field.extract, *field.alternates][:3]}
    )


def find_heal(item_nodes: list[Selector], field: Field, fp: dict[str, Any]) -> Candidate | None:
    """Best relocation for `field`: try the original attribute first, then element text."""
    attrs: list[str | None] = [field.extract.attr] if field.extract.attr else [None]
    if field.extract.attr:
        attrs.append(None)
    for attr in attrs:
        cands = relocate(item_nodes, fp, attr=attr)
        if cands:
            return cands[0]
    return None
