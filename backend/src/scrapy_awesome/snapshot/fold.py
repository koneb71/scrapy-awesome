"""Folded DOM — a compact, LLM-friendly outline of a page.

Compared to raw HTML it drops noise (scripts, styles, svg, tracking attributes), truncates long
text, and *collapses runs of similar siblings* (list items, cards, table rows) so a 200-item
listing reads like "here is the shape of one item, +199 more". Selectors an agent reads off the
outline (tag / #id / .class / [attr]) are exactly the ones our engine accepts.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lxml_html
from lxml.etree import _Comment, _Element

DROP_TAGS = frozenset(
    [
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "frame",
        "object",
        "embed",
        "link",
        "meta",
        "path",
        "symbol",
        "defs",
        "use",
        "picture",
        "source",
        "track",
        "map",
        "area",
    ]
)
INLINE_SKIP = frozenset(["br", "wbr", "hr"])
KEEP_ATTRS = (
    "id",
    "class",
    "href",
    "src",
    "alt",
    "title",
    "name",
    "type",
    "value",
    "role",
    "itemprop",
    "itemtype",
    "datetime",
    "aria-label",
    "placeholder",
    "action",
    "method",
    "for",
    "rel",
    "content",
    "property",
    "colspan",
    "rowspan",
)
_UNSTABLE_CLASS = re.compile(r"\d{2,}|^(css|sc|jsx|_)-|[A-Za-z0-9]{6,}[-_][A-Za-z0-9]{4,}$|__|--")
_WS = re.compile(r"\s+")


def _clean_text(s: str | None, limit: int) -> str:
    if not s:
        return ""
    s = _WS.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _classes(el: _Element, max_classes: int) -> list[str]:
    out = []
    for c in (el.get("class") or "").split():
        if 3 <= len(c) <= 40 and not _UNSTABLE_CLASS.search(c):
            out.append(c)
        if len(out) >= max_classes:
            break
    return out


def _sig(el: _Element, max_classes: int) -> str:
    """Similarity key for sibling collapsing: tag + stable classes *without digits*
    (`item-1`/`item-2`, `col-3` are positional, not structural)."""
    tag = el.tag.lower() if isinstance(el.tag, str) else "?"
    cls = [c for c in _classes(el, max_classes + 2) if not any(ch.isdigit() for ch in c)]
    return tag + "".join(f".{c}" for c in cls[:max_classes])


def _open_tag(el: _Element, max_classes: int, attr_limit: int) -> str:
    tag = el.tag.lower()
    parts = [tag]
    eid = el.get("id")
    if eid:
        parts[0] += f"#{eid}"
    cls = _classes(el, max_classes)
    if cls:
        parts[0] += "".join(f".{c}" for c in cls)
    for a in KEEP_ATTRS:
        if a in ("id", "class"):
            continue
        v = el.get(a)
        if v is None:
            continue
        v = _clean_text(v, attr_limit)
        if a in ("content", "property") and tag != "meta":
            continue
        parts.append(f'{a}="{v}"')
    for a, v in el.attrib.items():
        if a.startswith("data-") and len(parts) < 8:
            parts.append(f'{a}="{_clean_text(v, attr_limit)}"')
    return "<" + " ".join(parts) + ">"


def fold_html(
    html: str,
    *,
    max_chars: int = 12_000,
    text_limit: int = 80,
    keep_siblings: int = 2,
    max_depth: int = 40,
    max_classes: int = 2,
) -> str:
    """Return the folded outline (plain text, indented pseudo-HTML)."""
    try:
        root = lxml_html.fromstring(html or "<html><body></body></html>")
    except Exception:
        return ""
    lines: list[str] = []
    budget = {"chars": 0, "truncated": False}

    def emit(depth: int, s: str) -> None:
        if budget["truncated"]:
            return
        line = "  " * min(depth, 30) + s
        budget["chars"] += len(line) + 1
        if budget["chars"] > max_chars:
            budget["truncated"] = True
            lines.append("  " * min(depth, 30) + "<!-- … outline truncated (max_chars) -->")
            return
        lines.append(line)

    def walk(el: _Element, depth: int) -> None:
        if budget["truncated"] or depth > max_depth:
            return
        if isinstance(el, _Comment) or not isinstance(el.tag, str):
            return
        tag = el.tag.lower()
        if tag in DROP_TAGS or tag in INLINE_SKIP:
            return
        if tag == "input" and (el.get("type") or "").lower() == "hidden":
            return
        own_text = _clean_text(el.text, text_limit)
        children = [c for c in el if isinstance(c.tag, str) and c.tag.lower() not in DROP_TAGS]
        head = _open_tag(el, max_classes, 60)
        if not children:
            body = own_text
            # empty structural element — still show it if it carries an id/class/attr hook
            if (
                not body
                and tag not in ("img", "input", "a", "button", "select", "textarea", "td", "th")
                and "#" not in head
                and "." not in head
                and "=" not in head
            ):
                return
            emit(depth, f"{head} {body}".rstrip())
            return
        emit(depth, f"{head} {own_text}".rstrip())
        # collapse runs of similar siblings
        i = 0
        n = len(children)
        while i < n and not budget["truncated"]:
            sig = _sig(children[i], max_classes)
            j = i
            while j < n and _sig(children[j], max_classes) == sig:
                j += 1
            run = children[i:j]
            shown = run[:keep_siblings] if len(run) > keep_siblings + 1 else run
            for c in shown:
                walk(c, depth + 1)
                tail = _clean_text(c.tail, text_limit)
                if tail:
                    emit(depth + 1, tail)
            if len(shown) < len(run):
                emit(depth + 1, f"<!-- +{len(run) - len(shown)} more <{sig}> siblings -->")
            i = j

    body = root.find("body")
    head_el = root.find("head")
    if head_el is not None:
        title = head_el.findtext("title")
        if title:
            emit(0, f"<title> {_clean_text(title, 160)}")
        for m in head_el.findall("meta"):
            key = m.get("property") or m.get("name") or ""
            if key in ("description", "og:title", "og:description", "og:type", "og:url") and m.get(
                "content"
            ):
                emit(0, f'<meta {key}="{_clean_text(m.get("content"), 160)}">')
        ld = len(head_el.xpath('.//script[@type="application/ld+json"]')) + len(
            root.xpath('//body//script[@type="application/ld+json"]')
        )
        if ld:
            emit(0, f"<!-- {ld} ld+json block(s): see list_json_blobs -->")
    walk(body if body is not None else root, 0)
    return "\n".join(lines)


def outline_stats(outline: str) -> dict[str, Any]:
    return {"chars": len(outline), "lines": outline.count("\n") + 1 if outline else 0}
